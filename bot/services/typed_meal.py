"""Resolve parsed meal segments against saved foods, recipes, and the catalog.

The rules come straight from the plan, and each one is a refusal to guess:

* **Private names win.** A user's own "oats" is what they meant, even if the
  shared catalog also has one.
* **Exact before fuzzy.** An exact name match resolves. Otherwise the catalog is
  searched, and a search is only accepted when it returns exactly one candidate.
* **Ambiguity is never broken by ranking.** Two plausible matches means the
  segment stays unresolved and the user chooses. Picking the "best" one silently
  is how a log ends up containing food nobody ate.
* **Nutrition is never invented.** Every resolved segment goes through
  :mod:`bot.nutrition_resolution` against a stored definition. A segment with no
  quantity, or a quantity the shared parser rejects, is reported unresolved.

The output is a plan, not a write. The caller shows it, the user confirms, and
the existing atomic meal path performs the single write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Sequence

from ..meal_text import ParsedSegment
from ..nutrition import (
    NutritionError,
    normalize_catalog_name,
    require_complete_nutrients,
)
from ..nutrition_resolution import (
    ResolvedCatalogDietEntry,
    resolve_catalog_food_entry,
    resolve_food_diet_entry,
    resolve_recipe_diet_entry,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TypedMealPlan",
    "UnresolvedSegment",
    "augment_plan_with_parser",
    "plan_typed_meal",
    "REASON_TEXT",
]

#: Why a segment could not become a log item. Each maps to a distinct user
#: message, because the fix differs: add a food, give an amount, or disambiguate.
REASON_TEXT = {
    "unknown": "not in your foods or the catalog",
    "no_quantity": "needs an amount",
    "bad_quantity": "that amount or unit isn't supported",
    "ambiguous": "matches more than one item",
    "incomplete_nutrition": "its saved nutrition is incomplete",
}


@dataclass(frozen=True)
class UnresolvedSegment:
    """A segment that deliberately did not become a log item."""

    raw: str
    name: str
    reason: str
    candidates: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def explanation(self) -> str:
        return REASON_TEXT.get(self.reason, self.reason)


@dataclass(frozen=True)
class TypedMealPlan:
    """Everything the confirm screen needs, and nothing written yet."""

    resolved: tuple[ResolvedCatalogDietEntry, ...] = ()
    unresolved: tuple[UnresolvedSegment, ...] = ()
    #: True when an external parser contributed at least one resolved item, so
    #: the confirm screen can say so. Assistance is never invisible.
    model_assisted: bool = False

    @property
    def has_items(self) -> bool:
        return bool(self.resolved)

    @property
    def total_calories(self) -> int | None:
        """Sum of the resolved items' calories, or ``None`` when there are none.

        No item can reach ``resolved`` with a missing value — the planner refuses
        an incomplete definition outright — so this total is never partial.
        """
        return self._total("calories")

    @property
    def total_protein_g(self) -> float | None:
        return self._total("protein_g")

    @property
    def total_carbs_g(self) -> float | None:
        return self._total("carbs_g")

    @property
    def total_fat_g(self) -> float | None:
        return self._total("fat_g")

    def _total(self, field: str):
        values = [getattr(entry, field) for entry in self.resolved]
        if not values or any(value is None for value in values):
            return None
        total = sum(values)
        return total if field == "calories" else round(float(total), 1)

    @property
    def has_unknown_calories(self) -> bool:
        """Retained for callers; always ``False`` now that gaps are refused."""
        return any(e.calories is None for e in self.resolved)


def _name_key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _exact_matches(rows: Sequence[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    key = _name_key(name)
    return [row for row in rows if _name_key(row.get("name")) == key]


async def plan_typed_meal(
    db: Any,
    user_id: int,
    segments: Sequence[ParsedSegment],
) -> TypedMealPlan:
    """Resolve each segment, in order, without writing anything.

    Reads the user's foods and recipes once for the whole line rather than per
    segment, so a five-item meal is two queries plus at most one catalog search
    per unmatched segment.
    """
    resolved: list[ResolvedCatalogDietEntry] = []
    unresolved: list[UnresolvedSegment] = []

    foods = await db.list_foods(user_id)
    recipes = await db.list_recipes(user_id)

    for segment in segments:
        outcome = await _resolve_one(db, user_id, segment, foods, recipes)
        if isinstance(outcome, UnresolvedSegment):
            unresolved.append(outcome)
        else:
            resolved.append(outcome)

    return TypedMealPlan(resolved=tuple(resolved), unresolved=tuple(unresolved))


async def augment_plan_with_parser(
    db: Any,
    user_id: int,
    plan: TypedMealPlan,
    parser: Any,
) -> TypedMealPlan:
    """Re-attempt only the *unresolved* segments through an external parser.

    Deterministic results are never revisited: anything already resolved locally
    keeps its local resolution, so turning the model on cannot change how an
    already-working meal is logged. The model only sees text the local parser
    could not use, and its output goes through the same resolver — so it can
    contribute a better *segmentation*, never a nutrient value.

    Any failure leaves ``plan`` untouched, because :meth:`MealParser.parse`
    returns an empty list rather than raising.
    """
    if parser is None or not plan.unresolved:
        return plan

    # Only the raw text of what could not be understood leaves the host, and
    # only for segments the user just typed.
    leftover = ", ".join(item.raw for item in plan.unresolved)
    try:
        proposed = await parser.parse(leftover)
    except Exception:  # A provider must not be able to break logging.
        logger.warning("Meal parser raised; keeping local result", exc_info=False)
        return plan

    if not proposed:
        return plan

    segments = [
        ParsedSegment(
            raw=item.food, name=item.food, quantity_tokens=tuple(item.as_tokens())
        )
        for item in proposed
        if item.food
    ]
    if not segments:
        return plan

    retry = await plan_typed_meal(db, user_id, segments)
    if not retry.resolved:
        # The model re-segmented but nothing resolved. Keep the original
        # unresolved list, whose reasons describe what the user actually typed.
        return plan

    # The model is under no obligation to answer about everything it was sent,
    # and a partial answer used to erase the rest: the leftovers went in as one
    # joined string, so anything the model did not mention simply vanished from
    # "Not logged" and the user was never told an item had been dropped.
    #
    # An original is therefore surrendered to the model's account of it only when
    # the model visibly spoke about it. Anything unclaimed is carried through
    # unchanged, still carrying the reason that describes what the user typed.
    claimed = _claimed_originals(plan.unresolved, segments)
    carried = tuple(item for item in plan.unresolved if item not in claimed)

    return TypedMealPlan(
        resolved=(*plan.resolved, *retry.resolved),
        unresolved=(*carried, *retry.unresolved),
        model_assisted=True,
    )


def _significant_tokens(value: str) -> frozenset[str]:
    """Word tokens worth matching on, ignoring amounts and one-letter noise."""
    return frozenset(
        token
        for token in _name_key(value).replace("-", " ").split()
        if len(token) > 1 and not token.replace(".", "").isdigit()
    )


def _claimed_originals(
    originals: Sequence[UnresolvedSegment],
    proposed: Sequence[ParsedSegment],
) -> set[UnresolvedSegment]:
    """Which original segments the model's re-segmentation actually addressed.

    The model returns names drawn from the very text it was given, so a shared
    word is good evidence it is talking about that item. Matching is deliberately
    biased towards *not* claiming: an unclaimed original is merely listed again,
    while a wrongly claimed one disappears without trace.
    """
    proposed_tokens = [_significant_tokens(segment.name) for segment in proposed]
    claimed: set[UnresolvedSegment] = set()
    for original in originals:
        tokens = _significant_tokens(original.name) | _significant_tokens(original.raw)
        if any(tokens & candidate for candidate in proposed_tokens):
            claimed.add(original)
    return claimed


def _singular_forms(name: str) -> tuple[str, ...]:
    """Naive singular readings of a plural name, best first.

    People type what they eat — "2 eggs" — while a catalog stores the food
    itself, "Egg". The lookup is substring-based, so the singular finds the
    plural entry but never the reverse, and the advertised example could not
    resolve. Rules are deliberately crude because they are only ever a *second*
    attempt, used when the name as typed matched nothing at all.
    """
    stripped = name.strip()
    lowered = stripped.casefold()
    if len(stripped) < 4 or not lowered.endswith("s") or lowered.endswith("ss"):
        return ()
    forms = [stripped[:-1]]
    if lowered.endswith(("oes", "ches", "shes", "xes", "zes")):
        forms.insert(0, stripped[:-2])
    return tuple(form for form in forms if form)


async def _resolve_one(
    db: Any,
    user_id: int,
    segment: ParsedSegment,
    foods: Sequence[dict[str, Any]],
    recipes: Sequence[dict[str, Any]],
) -> ResolvedCatalogDietEntry | UnresolvedSegment:
    """Resolve one segment as typed, then — only if nothing matched — singular.

    The retry is confined to ``unknown``: an ambiguous match or a rejected
    quantity is a real answer about a food that *was* found, and re-running the
    lookup under a different name could only replace it with a worse one.
    """
    outcome = await _resolve_as_named(db, user_id, segment, foods, recipes)
    if not isinstance(outcome, UnresolvedSegment) or outcome.reason != "unknown":
        return outcome

    for candidate in _singular_forms(segment.name):
        retried = await _resolve_as_named(
            db, user_id, replace(segment, name=candidate), foods, recipes
        )
        if not isinstance(retried, UnresolvedSegment):
            return retried
        if retried.reason != "unknown":
            # The singular found the food but could not use it (no amount, or an
            # unsupported one). That reason is about a real match, so it is more
            # useful than "not in your foods or the catalog" — but it is reported
            # against the name the user actually typed.
            return replace(retried, raw=segment.raw, name=segment.name)

    return outcome


def _reject_incomplete(
    segment: ParsedSegment, entry: ResolvedCatalogDietEntry
) -> ResolvedCatalogDietEntry | UnresolvedSegment:
    """Turn a resolved-but-nutritionally-incomplete entry into a refusal.

    Resolving proves the *food* was found, not that its numbers are usable. A
    definition saved before nutrition was mandatory can still be missing macros,
    and logging it would put a hole straight into the day's totals. Refusing here
    means the preview says so, with the food named, instead of the save failing
    later at the write path.
    """
    try:
        require_complete_nutrients(entry.as_item(), what=entry.display_text)
    except NutritionError as exc:
        return UnresolvedSegment(
            raw=segment.raw,
            name=segment.name,
            reason="incomplete_nutrition",
            detail=str(exc),
        )
    return entry


async def _resolve_as_named(
    db: Any,
    user_id: int,
    segment: ParsedSegment,
    foods: Sequence[dict[str, Any]],
    recipes: Sequence[dict[str, Any]],
) -> ResolvedCatalogDietEntry | UnresolvedSegment:
    """Resolve one segment: private exact, then recipe exact, then catalog."""
    food_hits = _exact_matches(foods, segment.name)
    recipe_hits = _exact_matches(recipes, segment.name)

    if len(food_hits) + len(recipe_hits) > 1:
        # A private food and a private recipe sharing a name is rare but real.
        # Refuse rather than rank.
        names = [str(row.get("name")) for row in (*food_hits, *recipe_hits)]
        return UnresolvedSegment(
            raw=segment.raw,
            name=segment.name,
            reason="ambiguous",
            candidates=tuple(names[:4]),
        )

    if food_hits:
        return await _resolve_food(db, user_id, segment, food_hits[0])
    if recipe_hits:
        return await _resolve_recipe(db, user_id, segment, recipe_hits[0])

    return await _resolve_from_catalog(db, segment)


def _missing_quantity(segment: ParsedSegment) -> UnresolvedSegment | None:
    if segment.has_quantity:
        return None
    return UnresolvedSegment(
        raw=segment.raw, name=segment.name, reason="no_quantity"
    )


def _bad_quantity(segment: ParsedSegment, exc: Exception) -> UnresolvedSegment:
    return UnresolvedSegment(
        raw=segment.raw,
        name=segment.name,
        reason="bad_quantity",
        detail=str(exc),
    )


async def _resolve_food(db, user_id, segment, food):
    missing = _missing_quantity(segment)
    if missing is not None:
        return missing
    portions = await db.get_food_portions(user_id, food["id"])
    try:
        entry = resolve_food_diet_entry(food, portions, list(segment.quantity_tokens))
    except NutritionError as exc:
        return _bad_quantity(segment, exc)
    return _reject_incomplete(segment, entry)


async def _resolve_recipe(db, user_id, segment, recipe):
    missing = _missing_quantity(segment)
    if missing is not None:
        return missing
    ingredients = await db.get_recipe_ingredients(user_id, recipe["id"])
    try:
        entry = resolve_recipe_diet_entry(
            recipe, ingredients, list(segment.quantity_tokens)
        )
    except NutritionError as exc:
        return _bad_quantity(segment, exc)
    return _reject_incomplete(segment, entry)


async def _resolve_from_catalog(db, segment):
    """Search the shared catalog, accepting only an unambiguous single match."""
    try:
        normalize_catalog_name(segment.name, "Search")
    except NutritionError:
        # Not a searchable name (empty, or unsupported characters).
        return UnresolvedSegment(
            raw=segment.raw, name=segment.name, reason="unknown"
        )

    try:
        matches = await db.search_catalog(segment.name)
    except NutritionError:
        return UnresolvedSegment(
            raw=segment.raw, name=segment.name, reason="unknown"
        )

    if not matches:
        return UnresolvedSegment(
            raw=segment.raw, name=segment.name, reason="unknown"
        )

    exact = _exact_matches(matches, segment.name)
    if len(exact) == 1:
        chosen = exact[0]
    elif len(matches) == 1:
        chosen = matches[0]
    else:
        # Several plausible catalog entries: the user picks, we do not.
        return UnresolvedSegment(
            raw=segment.raw,
            name=segment.name,
            reason="ambiguous",
            candidates=tuple(str(row.get("name")) for row in matches[:4]),
        )

    missing = _missing_quantity(segment)
    if missing is not None:
        return missing

    portions = await db.get_catalog_portions(chosen["id"])
    try:
        entry = resolve_catalog_food_entry(
            chosen, portions, list(segment.quantity_tokens)
        )
    except NutritionError as exc:
        return _bad_quantity(segment, exc)
    return _reject_incomplete(segment, entry)
