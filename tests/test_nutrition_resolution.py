"""Release 3.1 — the resolver lives below the handler layer, and stays there.

The extraction exists to fix a layering inversion: ``DatabaseManager`` needed
pure nutrition resolution, the only copy lived in a Telegram handler module, so
the data layer lazy-imported from ``bot.handlers.catalog`` inside a locked write.

Moving code proves nothing on its own — the value is the constraint that it
cannot drift back. These tests read the module's source rather than its runtime
behavior, because ``import telegram`` succeeding in a test process says nothing
about whether *this* module caused it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bot import nutrition_resolution
from bot.nutrition_resolution import (
    ResolvedCatalogDietEntry,
    resolve_catalog_food_entry,
    resolve_food_diet_entry,
    resolve_recipe_diet_entry,
)

_MODULE = Path(nutrition_resolution.__file__)
_DATABASE = Path(__file__).resolve().parents[1] / "bot" / "database.py"

#: Anything that would drag the Telegram layer, a driver, or config back in.
_FORBIDDEN_ROOTS = {
    "telegram",
    "aiosqlite",
    "sqlite3",
}
_FORBIDDEN_LOCAL = {
    "config",
    "database",
    "handlers",
    "keyboards",
    "main",
}


def _imported_names(path: Path) -> set[str]:
    """Every module name imported anywhere in ``path``, including inside bodies."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # ``from . import x`` has module None; record the relative target.
            base = node.module or ""
            names.add(base)
            if node.level and base:
                names.add(base.split(".")[0])
            elif node.level:
                names.update(alias.name for alias in node.names)
    return {name for name in names if name}


def test_the_resolver_module_imports_no_telegram_driver_or_config():
    imported = _imported_names(_MODULE)
    roots = {name.split(".")[0] for name in imported}
    assert not (roots & _FORBIDDEN_ROOTS), f"forbidden import: {roots & _FORBIDDEN_ROOTS}"
    assert not (roots & _FORBIDDEN_LOCAL), f"layering violation: {roots & _FORBIDDEN_LOCAL}"


def test_the_resolver_module_performs_no_database_io():
    """No awaits and no cursor work: every input arrives as a plain mapping."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Await, ast.AsyncWith))]


def test_the_database_layer_no_longer_reaches_into_the_handler_package():
    """The inversion this release removes must not reappear."""
    source = _DATABASE.read_text(encoding="utf-8")
    assert "from .handlers.catalog import" not in source
    assert "from .nutrition_resolution import" in source


def test_catalog_still_re_exports_the_resolvers_for_existing_callers():
    from bot.handlers import catalog

    assert catalog.resolve_food_diet_entry is resolve_food_diet_entry
    assert catalog.resolve_recipe_diet_entry is resolve_recipe_diet_entry
    assert catalog.resolve_catalog_food_entry is resolve_catalog_food_entry
    assert catalog.ResolvedCatalogDietEntry is ResolvedCatalogDietEntry


# ---------------------------------------------------------------------------
# Behavior, exercised directly against the new module
# ---------------------------------------------------------------------------
_OATS = {
    "id": 7,
    "name": "oats",
    "base_unit": "g",
    "basis_amount": 100.0,
    "calories": 380,
    "protein_g": 13.0,
    "carbs_g": 67.0,
    "fat_g": 7.0,
}


def test_a_food_resolves_to_a_scaled_snapshot_with_provenance():
    entry = resolve_food_diet_entry(_OATS, [], ["50", "g"])

    assert entry.source_type == "food"
    assert entry.source_id == 7
    assert entry.entered_amount == 50.0
    assert entry.resolved_base_amount == 50.0
    assert entry.resolved_base_unit == "g"
    assert entry.calories == 190  # half of 380 per 100 g
    assert "oats" in entry.display_text


def test_a_catalog_food_reuses_the_food_maths_but_restamps_provenance():
    shared = dict(_OATS, provider="usda", provider_revision="r1")

    entry = resolve_catalog_food_entry(shared, [], ["100", "g"])

    assert entry.source_type == "catalog"
    assert (entry.source_provider, entry.source_revision) == ("usda", "r1")
    assert entry.calories == 380


def test_an_unknown_nutrient_stays_unknown_rather_than_becoming_zero():
    sparse = {**_OATS, "protein_g": None, "carbs_g": None, "fat_g": None}

    entry = resolve_food_diet_entry(sparse, [], ["100", "g"])

    assert entry.calories == 380
    assert entry.protein_g is None
    assert entry.carbs_g is None
    assert entry.fat_g is None


def test_a_recipe_in_the_wrong_dimension_is_refused():
    from bot.nutrition import NutritionError

    recipe = {"id": 3, "name": "curry", "yield_unit": "serving", "yield_amount": 4}
    with pytest.raises(NutritionError):
        resolve_recipe_diet_entry(recipe, [{"food_id": 1}], ["100", "g"])


def test_as_item_carries_every_column_the_child_row_needs():
    item = resolve_food_diet_entry(_OATS, [], ["100", "g"]).as_item()

    assert set(item) == {
        "source_type", "source_id", "source_provider", "source_revision",
        "display_name", "entered_amount", "entered_unit",
        "resolved_base_amount", "resolved_base_unit",
        "calories", "protein_g", "carbs_g", "fat_g",
    }
