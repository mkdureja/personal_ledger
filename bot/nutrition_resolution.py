"""Pure resolution of a saved food, recipe, or catalog item into a log entry.

This module exists to break a layering inversion. ``DatabaseManager`` needs to
turn "this source, this quantity" into a nutrient snapshot, and the only
implementation lived in ``bot/handlers/catalog.py`` — so the data layer
lazy-imported from a Telegram handler module inside a locked write. The
resolvers were always pure; only their address was wrong.

The contract here is deliberately narrow, and Release 4 depends on it: a parser
(deterministic today, model-assisted later) may only produce
``{food, qty, unit}``. Everything nutritional is computed *here*, from stored
definitions. That is what makes "no calorie value in the database came from a
language model" a structural property rather than a promise.

Constraints, enforced by :mod:`tests.test_nutrition_resolution`:

* no Telegram import;
* no database I/O — every input arrives as a plain mapping or sequence; and
* no application configuration.

Resolving something that requires a lookup (``food:oats``) stays in the caller:
it fetches the rows, then calls the matching function here.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import nutrition

__all__ = [
    "ResolvedCatalogDietEntry",
    "quantity_display",
    "request_for_item",
    "resolve_catalog_food_entry",
    "resolve_food_diet_entry",
    "resolve_recipe_diet_entry",
]


@dataclass(frozen=True)
class ResolvedCatalogDietEntry:
    """A calculated catalog item ready to be snapshotted into ``diet_logs``.

    Beyond the display text and computed nutrients, it carries the structured
    provenance a ``diet_log_items`` child needs: which food/recipe it came from,
    the quantity the user entered, and the canonical amount it resolved to.
    """

    display_text: str
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    source_type: str = "freetext"
    source_id: int | None = None
    source_provider: str | None = None
    source_revision: str | None = None
    entered_amount: float | None = None
    entered_unit: str | None = None
    resolved_base_amount: float | None = None
    resolved_base_unit: str | None = None

    def as_item(self) -> dict[str, Any]:
        """Render this entry as a ``diet_log_items`` row payload."""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_provider": self.source_provider,
            "source_revision": self.source_revision,
            "display_name": self.display_text,
            "entered_amount": self.entered_amount,
            "entered_unit": self.entered_unit,
            "resolved_base_amount": self.resolved_base_amount,
            "resolved_base_unit": self.resolved_base_unit,
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
        }


def request_for_item(quantity_tokens: Sequence[str]):
    """Parse entered quantity tokens for a food-like item."""
    if not quantity_tokens:
        raise nutrition.NutritionError("A quantity and unit are required.")
    return nutrition.parse_quantity(
        quantity_tokens,
        # ``serving`` may be an explicitly configured food portion even
        # though it is not a food's native storage dimension.
        allowed_base_units=nutrition.RECIPE_YIELD_UNITS,
        allow_named=True,
    )


def quantity_display(request: Any) -> tuple[object, str]:
    """Return an amount paired with the unit that amount actually represents."""
    if request.base_unit is not None:
        return request.amount, request.unit_key
    return request.amount, request.unit


def _finalized_entry(
    display: str, finalized: Mapping[str, Any], **provenance: Any
) -> ResolvedCatalogDietEntry:
    return ResolvedCatalogDietEntry(
        display_text=display,
        calories=finalized.get("calories"),
        protein_g=finalized.get("protein_g"),
        carbs_g=finalized.get("carbs_g"),
        fat_g=finalized.get("fat_g"),
        **provenance,
    )


def resolve_food_diet_entry(
    food: Mapping[str, Any],
    portions: Sequence[Mapping[str, Any]],
    quantity_tokens: Sequence[str],
) -> ResolvedCatalogDietEntry:
    """Calculate a food's nutrition for an entered quantity (no DB access)."""
    request = request_for_item(quantity_tokens)
    resolved_amount = nutrition.resolve_food_base_amount(food, portions, request)
    nutrients = nutrition.scale_food_nutrients(food, resolved_amount)
    finalized = nutrition.finalize_log_nutrients(nutrients)
    display_amount, display_unit = quantity_display(request)
    display = (
        f"{nutrition.format_decimal(display_amount)} {display_unit} {food['name']}"
    )
    return _finalized_entry(
        display,
        finalized,
        source_type="food",
        source_id=food.get("id"),
        entered_amount=float(display_amount),
        entered_unit=str(display_unit),
        resolved_base_amount=float(resolved_amount),
        resolved_base_unit=str(food["base_unit"]),
    )


def resolve_recipe_diet_entry(
    recipe: Mapping[str, Any],
    ingredients: Sequence[Mapping[str, Any]],
    quantity_tokens: Sequence[str],
) -> ResolvedCatalogDietEntry:
    """Calculate a recipe's nutrition for an entered yield quantity (no DB)."""
    request = nutrition.parse_quantity(
        quantity_tokens,
        allowed_base_units=nutrition.RECIPE_YIELD_UNITS,
        allow_named=False,
    )
    if request.base_unit != recipe["yield_unit"]:
        raise nutrition.NutritionError(
            f"This recipe is defined in {recipe['yield_unit']}; "
            f"{request.unit} is a different dimension."
        )
    if not ingredients:
        raise nutrition.NutritionError("This recipe has no ingredients.")
    assert request.base_amount is not None
    batch_factor = request.base_amount / Decimal(str(recipe["yield_amount"]))
    scaled = nutrition.aggregate_recipe_nutrients(ingredients, batch_factor)
    finalized = nutrition.finalize_log_nutrients(scaled)
    display_amount, display_unit = quantity_display(request)
    display = (
        f"{nutrition.format_decimal(display_amount)} {display_unit} "
        f"{recipe['name']} (recipe)"
    )
    return _finalized_entry(
        display,
        finalized,
        source_type="recipe",
        source_id=recipe.get("id"),
        entered_amount=float(display_amount),
        entered_unit=str(display_unit),
        resolved_base_amount=float(request.base_amount),
        resolved_base_unit=str(recipe["yield_unit"]),
    )


def resolve_catalog_food_entry(
    catalog_food: Mapping[str, Any],
    portions: Sequence[Mapping[str, Any]],
    quantity_tokens: Sequence[str],
) -> ResolvedCatalogDietEntry:
    """Calculate a shared-catalog food's nutrition for an entered quantity.

    ``catalog_foods`` mirrors the private ``foods`` shape (``base_unit``,
    ``basis_amount``, nutrients, and a ``name`` alias), so the same resolver is
    reused; only the provenance is stamped as coming from the shared catalog.
    """
    entry = resolve_food_diet_entry(catalog_food, portions, quantity_tokens)
    return dataclasses.replace(
        entry,
        source_type="catalog",
        source_id=catalog_food.get("id"),
        source_provider=catalog_food.get("provider"),
        source_revision=catalog_food.get("provider_revision"),
    )
