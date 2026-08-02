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

from dataclasses import dataclass
from typing import Any, Sequence

from ..meal_text import ParsedSegment
from ..nutrition import NutritionError, normalize_catalog_name
from ..nutrition_resolution import (
    ResolvedCatalogDietEntry,
    resolve_catalog_food_entry,
    resolve_food_diet_entry,
    resolve_recipe_diet_entry,
)

__all__ = ["TypedMealPlan", "UnresolvedSegment", "plan_typed_meal", "REASON_TEXT"]

#: Why a segment could not become a log item. Each maps to a distinct user
#: message, because the fix differs: add a food, give an amount, or disambiguate.
REASON_TEXT = {
    "unknown": "not in your foods or the catalog",
    "no_quantity": "needs an amount",
    "bad_quantity": "that amount or unit isn't supported",
    "ambiguous": "matches more than one item",
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

    @property
    def has_items(self) -> bool:
        return bool(self.resolved)

    @property
    def total_calories(self) -> int | None:
        """Sum of known calories, or ``None`` if every item is unknown.

        An unknown item contributes nothing rather than zero, and the caller is
        expected to say the total is partial when ``has_unknown_calories``.
        """
        known = [e.calories for e in self.resolved if e.calories is not None]
        return sum(known) if known else None

    @property
    def has_unknown_calories(self) -> bool:
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


async def _resolve_one(
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
        return resolve_food_diet_entry(food, portions, list(segment.quantity_tokens))
    except NutritionError as exc:
        return _bad_quantity(segment, exc)


async def _resolve_recipe(db, user_id, segment, recipe):
    missing = _missing_quantity(segment)
    if missing is not None:
        return missing
    ingredients = await db.get_recipe_ingredients(user_id, recipe["id"])
    try:
        return resolve_recipe_diet_entry(
            recipe, ingredients, list(segment.quantity_tokens)
        )
    except NutritionError as exc:
        return _bad_quantity(segment, exc)


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
        return resolve_catalog_food_entry(
            chosen, portions, list(segment.quantity_tokens)
        )
    except NutritionError as exc:
        return _bad_quantity(segment, exc)
