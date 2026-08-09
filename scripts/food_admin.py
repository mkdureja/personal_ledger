r"""Add saved foods and recipes from this machine instead of from Telegram.

Typing a food into a chat is fine for one food. Building the ten or fifteen
staples that make ``⚡`` one-tap rows possible is a different job: it wants a
form, a keyboard, and both ledgers visible at once. That is what this is.

    .\.venv\Scripts\python.exe -m scripts.food_admin

It prints a ``http://127.0.0.1:8765/?token=...`` URL and opens it. Everything
happens in that page; stop the server with Ctrl+C.

Three properties are deliberate:

* **Every write goes through** :class:`bot.database.DatabaseManager`. Nothing
  here writes SQL. Mandatory nutrition, name normalization, portion rules, and
  the owner checks are the same code the bot runs, so a food added here is
  indistinguishable from one added with ``/food add``.
* **It never migrates.** If the database's ``user_version`` is not the version
  this checkout knows, it refuses to start and says so. Migration belongs to the
  bot's startup preflight, which takes a verified backup first; a maintenance
  tool quietly bumping the schema would skip that gate.
* **It is bound to the loopback interface and needs a per-run token.** A page in
  a browser can post to ``localhost`` without being able to read the reply, so a
  loopback bind alone is not an authorization check. The token is, and the
  ``Origin`` guard closes the same hole from the other side.

Safe to run while the bot is polling: both processes open the same WAL database
with a busy timeout, so a write here waits for the bot's write rather than
failing. It is still calmer to add a batch of foods while the bot is stopped.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import secrets
import sqlite3
import sys
import threading
import unicodedata
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot import nutrition  # noqa: E402
from bot.database import DatabaseManager  # noqa: E402
from bot.meal_models import DefaultQuantity  # noqa: E402
from ledger_schema import LATEST_SCHEMA_VERSION  # noqa: E402

#: The page is a sibling file rather than a string constant so it can be edited
#: with an HTML editor and diffed as HTML.
PAGE_PATH = Path(__file__).resolve().parent / "food_admin.html"

#: The shared catalog is code-seeded, not a runtime table. ``seed_catalog`` is a
#: snapshot reconciliation: on every bot start it deactivates any curated row
#: absent from this file. So a catalog food added straight to the database would
#: silently disappear at the next restart — the only durable way to add one is to
#: add it here, which also means every change arrives as a reviewable git diff.
CATALOG_SEED_PATH = PROJECT_ROOT / "bot" / "catalog_seed.py"

#: Generous for a form post, small enough that a stray upload cannot exhaust
#: memory on a machine that is also running the bot.
MAX_BODY_BYTES = 64 * 1024

#: A write goes to at most two ledgers and takes one lock; anything past this is
#: a stuck writer, and failing tells the user more than hanging does.
REQUEST_TIMEOUT_SECONDS = 30.0


class AdminError(ValueError):
    """A request that the caller can fix by changing what they typed."""


@dataclass(frozen=True)
class PortionSpec:
    """A named portion, already expressed in the food's own base unit."""

    name: str
    amount: float


@dataclass(frozen=True)
class FoodSpec:
    name: str
    base_unit: str
    basis_amount: float
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    portions: tuple[PortionSpec, ...] = ()
    default: DefaultQuantity | None = None


@dataclass(frozen=True)
class IngredientSpec:
    """One food in a recipe, named the way the user knows it."""

    food: str
    amount: float
    unit: str


@dataclass(frozen=True)
class RecipeSpec:
    name: str
    yield_amount: float
    yield_unit: str
    ingredients: tuple[IngredientSpec, ...]
    default: DefaultQuantity | None = None


# --------------------------------------------------------------------------
# Payload validation. Pure: no database, no network, no event loop.
# --------------------------------------------------------------------------


def _text(data: Mapping[str, Any], field: str, *, limit: int) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdminError(f"{field.replace('_', ' ').capitalize()} is required.")
    text = value.strip()
    if len(text) > limit:
        raise AdminError(
            f"{field.replace('_', ' ').capitalize()} must be "
            f"{limit} characters or fewer."
        )
    return text


def _number(data: Mapping[str, Any], field: str, *, positive: bool) -> float:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # Booleans are ints in Python and would otherwise sail through as 0/1.
        raise AdminError(f"{field.replace('_', ' ').capitalize()} must be a number.")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise AdminError(f"{field.replace('_', ' ').capitalize()} must be a number.")
    if positive and number <= 0:
        raise AdminError(
            f"{field.replace('_', ' ').capitalize()} must be greater than zero."
        )
    if not positive and number < 0:
        raise AdminError(f"{field.replace('_', ' ').capitalize()} cannot be negative.")
    return number


def _unit(value: object, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str):
        raise AdminError(f"{label} is required.")
    unit = value.strip().casefold()
    if unit not in allowed:
        raise AdminError(f"{label} must be one of {', '.join(sorted(allowed))}.")
    return unit


def _optional_default(data: Mapping[str, Any]) -> DefaultQuantity | None:
    """Parse the usual amount that turns a source into a one-tap row."""
    raw = data.get("default")
    if raw in (None, ""):
        return None
    if not isinstance(raw, Mapping):
        raise AdminError("The usual amount must have an amount and a unit.")
    amount = _number(raw, "amount", positive=True)
    unit = raw.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise AdminError("The usual amount needs a unit.")
    return DefaultQuantity(amount=amount, unit=unit.strip())


def parse_portions(raw: object, base_unit: str) -> tuple[PortionSpec, ...]:
    """Validate named portions against the same rules ``/food portion`` uses.

    A portion is stored in the food's own base unit, so ``bowl = 40`` on a
    per-100 g food means forty grams. The reserved-name check is the handler's:
    naming a portion after the food's own dimension would shadow the unit the
    parser already understands.
    """
    if raw in (None, ""):
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise AdminError("Portions must be a list.")
    portions: list[PortionSpec] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise AdminError("Each portion needs a name and an amount.")
        name = _text(entry, "name", limit=nutrition.MAX_PORTION_NAME_LENGTH)
        amount = _number(entry, "amount", positive=True)
        if nutrition.is_reserved_portion_name(name, base_unit):
            raise AdminError(
                f"'{name}' duplicates this food's own base unit, so it would "
                "shadow a unit the app already understands."
            )
        # Mirrors the handler: a standard alias keeps the spelling the user
        # typed, anything else is stored under its normalized key.
        if nutrition.canonical_unit_alias(name) is not None:
            stored = name
        else:
            _display, stored = nutrition.normalize_catalog_name(
                name, "Portion name", nutrition.MAX_PORTION_NAME_LENGTH
            )
        if stored.casefold() in seen:
            raise AdminError(f"Portion '{name}' is listed twice.")
        seen.add(stored.casefold())
        portions.append(PortionSpec(name=stored, amount=amount))
    return tuple(portions)


def parse_food_payload(data: Mapping[str, Any]) -> FoodSpec:
    """Turn a form post into a validated food definition."""
    if not isinstance(data, Mapping):
        raise AdminError("Expected a food object.")
    base_unit = _unit(data.get("base_unit"), nutrition.FOOD_BASE_UNITS, "Base unit")
    return FoodSpec(
        name=_text(data, "name", limit=nutrition.MAX_CATALOG_NAME_LENGTH),
        base_unit=base_unit,
        basis_amount=_number(data, "basis_amount", positive=True),
        # Zero is a real answer for a macro; absent is not. Both are checked
        # again by save_food, which is the rule's actual home.
        calories=_number(data, "calories", positive=False),
        protein_g=_number(data, "protein_g", positive=False),
        carbs_g=_number(data, "carbs_g", positive=False),
        fat_g=_number(data, "fat_g", positive=False),
        portions=parse_portions(data.get("portions"), base_unit),
        default=_optional_default(data),
    )


def parse_recipe_payload(data: Mapping[str, Any]) -> RecipeSpec:
    """Turn a form post into a validated recipe definition."""
    if not isinstance(data, Mapping):
        raise AdminError("Expected a recipe object.")
    raw_ingredients = data.get("ingredients")
    if not isinstance(raw_ingredients, Sequence) or isinstance(
        raw_ingredients, (str, bytes)
    ):
        raise AdminError("A recipe needs a list of ingredients.")
    if not raw_ingredients:
        raise AdminError(
            "A recipe needs at least one ingredient — its nutrition is "
            "calculated from them, never stored on the recipe itself."
        )
    ingredients: list[IngredientSpec] = []
    for entry in raw_ingredients:
        if not isinstance(entry, Mapping):
            raise AdminError("Each ingredient needs a food, an amount, and a unit.")
        ingredients.append(
            IngredientSpec(
                food=_text(entry, "food", limit=nutrition.MAX_CATALOG_NAME_LENGTH),
                amount=_number(entry, "amount", positive=True),
                unit=_text(entry, "unit", limit=nutrition.MAX_PORTION_NAME_LENGTH),
            )
        )
    return RecipeSpec(
        name=_text(data, "name", limit=nutrition.MAX_CATALOG_NAME_LENGTH),
        yield_amount=_number(data, "yield_amount", positive=True),
        yield_unit=_unit(
            data.get("yield_unit"), nutrition.RECIPE_YIELD_UNITS, "Yield unit"
        ),
        ingredients=tuple(ingredients),
        default=_optional_default(data),
    )


@dataclass(frozen=True)
class CatalogSpec:
    """One shared-catalog food, as it will be written into the seed file."""

    food_id: str
    name: str
    base_unit: str
    basis_amount: float
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    category: str | None = None
    portions: tuple[PortionSpec, ...] = ()
    aliases: tuple[str, ...] = ()


def slugify(name: str) -> str:
    """A stable ``provider_food_id`` from a display name.

    ``seed_catalog`` keys on ``(provider, provider_food_id)``, so this id is the
    identity of the row across every future revision. Kept to lowercase ASCII
    words joined by hyphens, matching the ids already in the file.
    """
    cleaned = []
    for char in unicodedata.normalize("NFKD", name).casefold():
        if char.isalnum() and char.isascii():
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    return "".join(cleaned).strip("-")[:60]


def load_catalog_seed(path: Path | None = None) -> tuple[str, list[dict]]:
    """Read the seed file from disk and return its revision and foods.

    Executed rather than imported so a food added during this session shows up
    immediately, without restarting the tool. The file is part of this
    repository and contains only literals and one helper, which is the same code
    a normal import would run.
    """
    # Resolved at call time, not bound as a default, so a test can redirect the
    # module attribute and never touch the repository's own seed file.
    path = path or CATALOG_SEED_PATH
    source = path.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    return str(namespace["CATALOG_REVISION"]), list(namespace["CATALOG_FOODS"])


def parse_catalog_payload(
    data: Mapping[str, Any], existing_ids: Sequence[str]
) -> CatalogSpec:
    """Validate a catalog form post.

    Stricter than a private food in one way: all four nutrients are required
    with no exception, because a catalog row is a definition shared by everyone
    and inherited by every meal logged from it.
    """
    if not isinstance(data, Mapping):
        raise AdminError("Expected a catalog food object.")
    name = _text(data, "name", limit=nutrition.MAX_CATALOG_NAME_LENGTH)
    base_unit = _unit(data.get("base_unit"), nutrition.FOOD_BASE_UNITS, "Base unit")

    raw_id = data.get("food_id")
    food_id = (
        slugify(str(raw_id)) if isinstance(raw_id, str) and raw_id.strip()
        else slugify(name)
    )
    if not food_id:
        raise AdminError("That name has no letters or numbers to build an id from.")
    if food_id in existing_ids:
        raise AdminError(
            f"The catalog already has an entry with id '{food_id}'. Ids are the "
            "row's identity across revisions, so pick a distinct one."
        )

    category = data.get("category")
    if category is not None and not isinstance(category, str):
        raise AdminError("Category must be text.")
    category = (category or "").strip() or None

    raw_aliases = data.get("aliases") or []
    if isinstance(raw_aliases, str):
        raw_aliases = [part for part in raw_aliases.split(",")]
    if not isinstance(raw_aliases, Sequence):
        raise AdminError("Aliases must be a list.")
    aliases: list[str] = []
    for alias in raw_aliases:
        if not isinstance(alias, str):
            raise AdminError("Each alias must be text.")
        text = alias.strip()
        if text and text.casefold() not in {a.casefold() for a in aliases}:
            aliases.append(text)

    return CatalogSpec(
        food_id=food_id,
        name=name,
        base_unit=base_unit,
        basis_amount=_number(data, "basis_amount", positive=True),
        calories=_number(data, "calories", positive=False),
        protein_g=_number(data, "protein_g", positive=False),
        carbs_g=_number(data, "carbs_g", positive=False),
        fat_g=_number(data, "fat_g", positive=False),
        category=category,
        portions=parse_portions(data.get("portions"), base_unit),
        aliases=tuple(aliases),
    )


def _num(value: float) -> str:
    """Render a whole number without a trailing ``.0``, matching the file."""
    return str(int(value)) if float(value).is_integer() else str(float(value))


def _q(text: str) -> str:
    """A double-quoted Python string literal, matching the seed file's style.

    ``repr`` would emit single quotes and switch to double only when the value
    contains an apostrophe, so a generated line would sit in the diff looking
    unlike every line around it.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_catalog_entry(spec: CatalogSpec) -> str:
    """The ``_food(...)`` source line(s) for one catalog food."""
    head = (
        f"    _food({_q(spec.food_id)}, {_q(spec.name)}, {_q(spec.base_unit)}, "
        f"{_num(spec.basis_amount)}, {_num(spec.calories)}, "
        f"{_num(spec.protein_g)}, {_num(spec.carbs_g)}, {_num(spec.fat_g)}"
    )
    tail: list[str] = []
    if spec.category:
        tail.append(f"category={_q(spec.category)}")
    if spec.portions:
        rendered = ", ".join(
            f'{{"name": {_q(p.name)}, "base_amount": {_num(p.amount)}}}'
            for p in spec.portions
        )
        tail.append(f"portions=[{rendered}]")
    if spec.aliases:
        rendered = ", ".join(_q(alias) for alias in spec.aliases)
        tail.append(f"aliases=[{rendered}]")
    if not tail:
        return head + "),\n"
    return head + ",\n" + "".join(f"          {part},\n" for part in tail[:-1]) + (
        f"          {tail[-1]}),\n"
    )


def bump_revision(revision: str) -> str:
    """Advance ``YYYY.N`` to ``YYYY.N+1``.

    Bumping matters: the revision is stamped on every seeded row and copied into
    each logged item's snapshot, which is what makes a later value change
    auditable instead of silently rewriting history.
    """
    match = re.fullmatch(r"(\d+)\.(\d+)", revision.strip())
    if match is None:
        return f"{revision.strip()}.1"
    return f"{match.group(1)}.{int(match.group(2)) + 1}"


def append_catalog_food(spec: CatalogSpec, path: Path | None = None) -> str:
    """Add one food to the seed file and bump the revision. Returns the revision.

    The file is edited rather than regenerated: regenerating would discard the
    comments that explain why particular rows look the way they do (the whey
    scoop, the branded pack values), and those are the rows most likely to be
    misread later. Written atomically-ish — the original text is held and
    restored if the result does not parse, so a failed write can never leave the
    catalog unimportable and the bot unable to start.
    """
    path = path or CATALOG_SEED_PATH
    original = path.read_text(encoding="utf-8")

    revision_match = re.search(
        r'^CATALOG_REVISION\s*=\s*"([^"]+)"', original, re.MULTILINE
    )
    if revision_match is None:
        raise AdminError("Could not find CATALOG_REVISION in the seed file.")
    new_revision = bump_revision(revision_match.group(1))

    closing = original.rstrip()
    if not closing.endswith("]"):
        raise AdminError("The seed file does not end with the food list.")
    cut = original.rindex("]")
    updated = original[:cut] + render_catalog_entry(spec) + original[cut:]
    updated = (
        updated[: revision_match.start()]
        + f'CATALOG_REVISION = "{new_revision}"'
        + updated[revision_match.end() :]
    )

    path.write_text(updated, encoding="utf-8")
    try:
        ast.parse(updated, filename=str(path))
        revision, foods = load_catalog_seed(path)
        if revision != new_revision:
            raise AdminError("The revision did not update as expected.")
        if not any(food["provider_food_id"] == spec.food_id for food in foods):
            raise AdminError("The new food did not appear in the list.")
    except Exception as exc:
        path.write_text(original, encoding="utf-8")
        raise AdminError(f"The edit was rolled back — it did not parse: {exc}") from exc
    return new_revision


def parse_user_ids(raw: object, known: Sequence[int]) -> tuple[int, ...]:
    """Resolve the requested ledgers, refusing any this database does not have.

    Foods are private per user, so "save for both" means two independent rows,
    not one shared one. An unknown id is refused rather than created: creating a
    user is the bot's job, on a real Telegram message.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise AdminError("Choose at least one person to save this for.")
    ids: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise AdminError("User ids must be whole numbers.")
        if value not in known:
            raise AdminError(f"{value} is not a user of this ledger.")
        if value not in ids:
            ids.append(value)
    if not ids:
        raise AdminError("Choose at least one person to save this for.")
    return tuple(ids)


# --------------------------------------------------------------------------
# Database work. Each function takes an already-connected DatabaseManager.
# --------------------------------------------------------------------------


def _status_message(result: Mapping[str, Any], noun: str) -> str:
    status = str(result.get("status", "error"))
    if status == "unit_mismatch":
        return (
            f"That {noun} is measured in {result.get('expected_unit')}, "
            f"not {result.get('provided_unit')}."
        )
    if status == "not_found":
        return f"That {noun} is no longer available."
    if status == "name_conflict":
        return f"Another {noun} already uses that name."
    return f"The {noun} could not be saved ({status})."


async def load_users(db: DatabaseManager) -> list[dict[str, Any]]:
    """Everyone this ledger knows, in a shape the page can render."""
    rows = await db._query_all(  # noqa: SLF001 - no public listing exists
        "SELECT user_id, first_name, username FROM users ORDER BY user_id"
    )
    return [
        {
            "user_id": row["user_id"],
            "label": row["first_name"] or row["username"] or str(row["user_id"]),
        }
        for row in rows
    ]


async def active_catalog_ids(db: DatabaseManager) -> set[str]:
    """The ``provider_food_id``s currently seeded and active in the database."""
    try:
        rows = await db._query_all(  # noqa: SLF001 - no public listing exists
            "SELECT provider_food_id FROM catalog_foods WHERE is_active = 1"
        )
    except sqlite3.Error:  # pragma: no cover - table exists from v8
        return set()
    return {str(row["provider_food_id"]) for row in rows}


async def load_ledger(db: DatabaseManager, user_id: int) -> dict[str, Any]:
    """One user's saved foods and recipes, with enough detail to avoid retyping."""
    foods = []
    for food in await db.list_foods(user_id):
        portions = await db.get_food_portions(user_id, food["id"])
        foods.append(
            {
                "name": food["name"],
                "base_unit": food["base_unit"],
                "basis_amount": food["basis_amount"],
                "calories": food["calories"],
                "protein_g": food["protein_g"],
                "carbs_g": food["carbs_g"],
                "fat_g": food["fat_g"],
                "portions": [
                    {"name": row["name"], "amount": row["base_amount"]}
                    for row in portions
                ],
            }
        )
    recipes = []
    for recipe in await db.list_recipes(user_id):
        ingredients = await db.get_recipe_ingredients(user_id, recipe["id"])
        recipes.append(
            {
                "name": recipe["name"],
                "yield_amount": recipe["yield_amount"],
                "yield_unit": recipe["yield_unit"],
                "ingredients": [
                    {
                        "food": row["food_name"],
                        "amount": row["display_amount"],
                        "unit": row["display_unit"],
                    }
                    for row in ingredients
                ],
            }
        )
    return {"foods": foods, "recipes": recipes}


async def _apply_default(
    db: DatabaseManager,
    user_id: int,
    source_type: str,
    source_id: int,
    default: DefaultQuantity | None,
) -> str | None:
    """Store the usual amount, reporting rather than raising when it will not fit.

    The definition is already saved and useful by the time this runs, so a
    default that does not resolve downgrades the message instead of turning a
    completed save into a failure.
    """
    if default is None:
        return None
    try:
        await db.set_default_quantity(user_id, source_type, source_id, default)
    except (nutrition.NutritionError, ValueError) as exc:
        return f"saved, but the usual amount was not stored: {exc}"
    return None


async def apply_food(
    db: DatabaseManager, user_ids: Sequence[int], spec: FoodSpec
) -> list[dict[str, Any]]:
    """Save one food definition into each chosen ledger."""
    results: list[dict[str, Any]] = []
    for user_id in user_ids:
        try:
            saved = await db.save_food(
                user_id,
                spec.name,
                spec.base_unit,
                spec.basis_amount,
                spec.calories,
                spec.protein_g,
                spec.carbs_g,
                spec.fat_g,
            )
            food = saved.get("food")
            if food is None:
                raise AdminError(_status_message(saved, "food"))

            notes: list[str] = []
            for portion in spec.portions:
                stored = await db.save_food_portion(
                    user_id, food["id"], portion.name, portion.amount, spec.base_unit
                )
                if stored.get("portion") is None:
                    notes.append(
                        f"portion '{portion.name}' was not saved: "
                        f"{_status_message(stored, 'portion')}"
                    )
            note = await _apply_default(db, user_id, "food", food["id"], spec.default)
            if note:
                notes.append(note)
            results.append(
                {
                    "user_id": user_id,
                    "ok": True,
                    "status": saved.get("status"),
                    "name": food["name"],
                    "notes": notes,
                }
            )
        except (
            AdminError,
            nutrition.NutritionError,
            ValueError,
            sqlite3.Error,
        ) as exc:
            # Reported per ledger rather than raised: with two users selected, a
            # problem with one must not discard the other's completed save.
            results.append({"user_id": user_id, "ok": False, "error": str(exc)})
    return results


async def apply_recipe(
    db: DatabaseManager, user_ids: Sequence[int], spec: RecipeSpec
) -> list[dict[str, Any]]:
    """Save one recipe into each chosen ledger, resolved against that user's foods.

    Ingredients are named, not numbered, because each user owns a separate copy
    of the same food. The same definition therefore lands correctly in both
    ledgers, and a user missing an ingredient is told which one.
    """
    results: list[dict[str, Any]] = []
    for user_id in user_ids:
        try:
            # Every ingredient is resolved BEFORE the recipe row is created. The
            # recipe and its ingredients are separate writes, so creating the
            # recipe first would leave a nutrition-less empty recipe behind the
            # moment one ingredient turned out to be missing — and an empty
            # recipe totals zero, which is worse than no recipe at all.
            resolved: list[tuple[dict[str, Any], float, float, str]] = []
            for ingredient in spec.ingredients:
                food = await db.get_food_by_key(user_id, ingredient.food)
                if food is None:
                    raise AdminError(
                        f"'{ingredient.food}' is not one of this person's saved "
                        "foods. A recipe can only be built from foods they "
                        "already own — add it as a food first."
                    )
                portions = await db.get_food_portions(user_id, food["id"])
                request = nutrition.parse_quantity(
                    [str(ingredient.amount), ingredient.unit],
                    allowed_base_units=nutrition.RECIPE_YIELD_UNITS,
                    allow_named=True,
                )
                base_amount = nutrition.resolve_food_base_amount(
                    food, portions, request
                )
                resolved.append(
                    (food, float(base_amount), float(request.amount), request.unit_key)
                )

            saved = await db.save_recipe(
                user_id, spec.name, spec.yield_amount, spec.yield_unit
            )
            recipe = saved.get("recipe")
            if recipe is None:
                raise AdminError(_status_message(saved, "recipe"))

            for (food, base_amount, amount, unit_key), ingredient in zip(
                resolved, spec.ingredients
            ):
                stored = await db.save_recipe_ingredient(
                    user_id,
                    recipe["id"],
                    food["id"],
                    base_amount,
                    food["base_unit"],
                    amount,
                    unit_key,
                )
                if stored.get("ingredient") is None:
                    raise AdminError(
                        f"'{ingredient.food}' was not added: "
                        f"{_status_message(stored, 'ingredient')}"
                    )

            notes: list[str] = []
            note = await _apply_default(
                db, user_id, "recipe", recipe["id"], spec.default
            )
            if note:
                notes.append(note)
            results.append(
                {
                    "user_id": user_id,
                    "ok": True,
                    "status": saved.get("status"),
                    "name": recipe["name"],
                    "notes": notes,
                }
            )
        except (
            AdminError,
            nutrition.NutritionError,
            ValueError,
            LookupError,
            sqlite3.Error,
        ) as exc:
            results.append({"user_id": user_id, "ok": False, "error": str(exc)})
    return results


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


class LoopThread:
    """An asyncio loop on its own thread, so blocking handlers can await."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="food-admin-loop", daemon=True
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def run(self, coro: Any, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


def check_schema(db_path: Path) -> int:
    """Refuse to touch a database this checkout does not match.

    Older is the bot's problem to fix, with a backup first. Newer means this
    checkout predates the file and cannot know what its rows mean.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    if version != LATEST_SCHEMA_VERSION:
        raise SystemExit(
            f"ERROR: {db_path} is at schema v{version}, this checkout knows "
            f"v{LATEST_SCHEMA_VERSION}.\n"
            "This tool never migrates: start the bot once so its preflight can "
            "take a verified backup and migrate, then run this again."
        )
    return version


class AdminServer(ThreadingHTTPServer):
    """Carries the shared state each request handler needs."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type, **state: Any) -> None:
        super().__init__(address, handler)
        self.state = state


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "LedgerFoodAdmin/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    @property
    def _state(self) -> dict[str, Any]:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # One quiet line per request; the default writes a full timestamped
        # banner that buries the startup URL the user still needs.
        sys.stderr.write(f"  {self.command} {self.path.split('?')[0]}\n")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page is entirely self-contained; nothing should ever be fetched.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Mapping[str, Any]) -> None:
        self._send(
            code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8"
        )

    def _authorized(self) -> bool:
        """A per-run token, because a loopback bind is not an authorization check."""
        token = self._state["token"]
        header = self.headers.get("X-Admin-Token")
        if header is not None:
            return secrets.compare_digest(header, token)
        query = parse_qs(urlparse(self.path).query)
        supplied = (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied, token)

    def _origin_ok(self) -> bool:
        """Refuse a cross-site post even though it could not read the reply."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlparse(origin).hostname in ("127.0.0.1", "localhost")

    def _read_body(self) -> bytes:
        """Consume the whole request body before answering, always.

        A browser keeps the connection alive, so an early return that leaves
        unread bytes in the socket desynchronizes the *next* request on it. That
        surfaces as a page that mysteriously stops working after one rejected
        save, which is a far harder thing to diagnose than the rejection itself.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise AdminError("The request had no body.")
        if length > MAX_BODY_BYTES:
            # Read what is there anyway; refusing without draining poisons the
            # connection exactly as an unread short body would.
            self.rfile.read(min(length, MAX_BODY_BYTES))
            raise AdminError("That request is too large.")
        return self.rfile.read(length)

    def _decode(self, raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdminError("The request was not valid JSON.") from exc

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(403, {"error": "Open the URL printed at startup."})
            return
        if path == "/":
            try:
                page = PAGE_PATH.read_text(encoding="utf-8")
            except OSError:
                self._json(500, {"error": f"Missing page file: {PAGE_PATH}"})
                return
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._json(200, self._state_payload())
            return
        self._json(404, {"error": "No such page."})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        loop: LoopThread = self._state["loop"]
        db: DatabaseManager = self._state["db"]
        try:
            # Drained first, before any decision, so every path below can return
            # early without leaving the connection out of step.
            raw = self._read_body()
        except AdminError as exc:
            self._json(400, {"error": str(exc)})
            return
        if not self._authorized() or not self._origin_ok():
            self._json(403, {"error": "Open the URL printed at startup."})
            return
        try:
            payload = self._decode(raw)
            if not isinstance(payload, Mapping):
                raise AdminError("Expected an object.")
            # Only the per-ledger routes need to know whose ledger. The catalog
            # is shared and writes a source file, so asking for user ids there
            # would be a field with no meaning that the caller must still fill.
            if path in ("/api/food", "/api/recipe"):
                known = [user["user_id"] for user in self._state["users"]]
                user_ids = parse_user_ids(payload.get("user_ids"), known)
            if path == "/api/food":
                spec = parse_food_payload(payload.get("food") or {})
                results = loop.run(apply_food(db, user_ids, spec))
            elif path == "/api/recipe":
                spec = parse_recipe_payload(payload.get("recipe") or {})
                results = loop.run(apply_recipe(db, user_ids, spec))
            elif path == "/api/catalog":
                # No user_ids: the catalog is shared, and this writes a source
                # file rather than either ledger.
                _revision, foods = load_catalog_seed()
                spec = parse_catalog_payload(
                    payload.get("catalog") or {},
                    [str(food["provider_food_id"]) for food in foods],
                )
                new_revision = append_catalog_food(spec)
                results = [
                    {
                        "user_id": None,
                        "ok": True,
                        "status": "added to the shared catalog",
                        "name": spec.name,
                        "notes": [
                            f"catalog revision is now {new_revision}",
                            "restart the bot to seed it — it is in the source "
                            "file, not the database, until then",
                        ],
                    }
                ]
            else:
                self._json(404, {"error": "No such action."})
                return
        except (AdminError, nutrition.NutritionError, ValueError, OSError) as exc:
            self._json(400, {"error": str(exc)})
            return
        except TimeoutError:
            self._json(
                504,
                {
                    "error": "The database did not respond in time. If the bot "
                    "is mid-write, try again."
                },
            )
            return
        self._json(200, {"results": results, **self._state_payload()})

    def _state_payload(self) -> dict[str, Any]:
        loop: LoopThread = self._state["loop"]
        db: DatabaseManager = self._state["db"]
        users = self._state["users"]
        try:
            revision, catalog = load_catalog_seed()
        except OSError as exc:  # pragma: no cover - the file ships with the repo
            revision, catalog = f"unreadable: {exc}", []
        # Which seed rows are live yet: anything added since the last bot start
        # is in the file but not the database, and saying so is the difference
        # between "it did not work" and "restart to seed it".
        seeded = loop.run(active_catalog_ids(db))
        return {
            "db_path": str(self._state["db_path"]),
            "schema_version": self._state["schema_version"],
            "catalog_revision": revision,
            "catalog_seed_path": str(CATALOG_SEED_PATH),
            "catalog": [
                {
                    "food_id": str(food["provider_food_id"]),
                    "name": str(food["display_name"]),
                    "base_unit": str(food["base_unit"]),
                    "basis_amount": food["basis_amount"],
                    "calories": food["calories"],
                    "protein_g": food["protein_g"],
                    "carbs_g": food["carbs_g"],
                    "fat_g": food["fat_g"],
                    "category": food.get("category"),
                    "portions": list(food.get("portions") or []),
                    "aliases": list(food.get("aliases") or []),
                    "live": str(food["provider_food_id"]) in seeded,
                }
                for food in catalog
            ],
            "users": [
                {**user, **loop.run(load_ledger(db, user["user_id"]))}
                for user in users
            ],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "ledger.db"),
        help="database to edit (default: ledger.db in the project root)",
    )
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2
    schema_version = check_schema(db_path)

    loop = LoopThread()
    loop.start()
    db = DatabaseManager(str(db_path))
    try:
        loop.run(db.connect())
        users = loop.run(load_users(db))
        if not users:
            print(
                "ERROR: this ledger has no users yet. Send the bot a message "
                "first so it can create them.",
                file=sys.stderr,
            )
            return 1

        token = secrets.token_urlsafe(16)
        server = AdminServer(
            ("127.0.0.1", args.port),
            AdminHandler,
            token=token,
            db=db,
            db_path=db_path,
            users=users,
            schema_version=schema_version,
            loop=loop,
        )
        url = f"http://127.0.0.1:{args.port}/?token={token}"
        who = ", ".join(user["label"] for user in users)
        print(f"Ledger food admin — {db_path} (schema v{schema_version})")
        print(f"Ledgers: {who}")
        print(f"\n  {url}\n")
        print("Ctrl+C to stop.")
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            server.server_close()
    finally:
        try:
            loop.run(db.close())
        finally:
            loop.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
