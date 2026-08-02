"""A small, hand-curated starter nutrition catalog.

This is a deliberately tiny generic set (public, per-100g/per-piece values) so
the shared catalog is useful out of the box without ingesting an external
dataset. Values are approximate and labelled ``curated``; a later revision or a
licensed import (USDA FoodData Central, etc.) can replace or extend it — every
row carries provider + revision so a refresh is auditable and never rewrites a
completed log's snapshot.

Bump ``CATALOG_REVISION`` when values change; ``seed_catalog`` upserts by
``(provider, provider_food_id)``.
"""

from __future__ import annotations

CATALOG_PROVIDER = "curated"
CATALOG_REVISION = "2026.2"


def _food(fid, name, unit, basis, cal, p, c, f, *, category=None, portions=None, aliases=None):
    return {
        "provider_food_id": fid,
        "display_name": name,
        "base_unit": unit,
        "basis_amount": basis,
        "calories": cal,
        "protein_g": p,
        "carbs_g": c,
        "fat_g": f,
        "category": category,
        "portions": portions or [],
        "aliases": aliases or [],
    }


# Per-100g unless the base unit is 'piece' (then per 1 piece).
CATALOG_FOODS: list[dict] = [
    _food("apple", "Apple", "g", 100, 52, 0.3, 14, 0.2, category="fruit",
          portions=[{"name": "medium", "base_amount": 182}]),
    _food("banana", "Banana", "g", 100, 89, 1.1, 23, 0.3, category="fruit",
          portions=[{"name": "medium", "base_amount": 118}]),
    _food("orange", "Orange", "g", 100, 47, 0.9, 12, 0.1, category="fruit",
          portions=[{"name": "medium", "base_amount": 130}]),
    _food("rice-cooked", "White rice (cooked)", "g", 100, 130, 2.7, 28, 0.3,
          category="grain", portions=[{"name": "bowl", "base_amount": 150}]),
    _food("roti", "Roti / chapati", "g", 100, 297, 11, 46, 7.5, category="grain",
          portions=[{"name": "piece", "base_amount": 40}], aliases=["chapati", "phulka"]),
    _food("bread-slice", "Bread slice", "piece", 1, 79, 2.7, 14.8, 1.0,
          category="grain", aliases=["toast"]),
    _food("oats", "Oats (dry)", "g", 100, 389, 16.9, 66, 6.9, category="grain",
          portions=[{"name": "bowl", "base_amount": 40}]),
    _food("egg", "Egg", "piece", 1, 78, 6.3, 0.6, 5.3, category="protein",
          aliases=["anda"]),
    _food("chicken-breast", "Chicken breast (cooked)", "g", 100, 165, 31, 0, 3.6,
          category="protein"),
    _food("paneer", "Paneer", "g", 100, 265, 18, 1.2, 20, category="protein",
          portions=[{"name": "cube", "base_amount": 20}]),
    _food("dal-cooked", "Dal (cooked)", "g", 100, 116, 9, 20, 0.4, category="protein",
          portions=[{"name": "bowl", "base_amount": 150}], aliases=["lentils"]),
    _food("milk", "Milk", "ml", 100, 42, 3.4, 5, 1.0, category="dairy",
          portions=[{"name": "glass", "base_amount": 250}]),
    _food("curd", "Curd / yogurt", "g", 100, 61, 3.5, 4.7, 3.3, category="dairy",
          portions=[{"name": "bowl", "base_amount": 150}], aliases=["yogurt", "dahi"]),
    _food("skyr", "Skyr", "g", 100, 101, 11, 9.5, 2.1, category="dairy"),
    _food("potato-boiled", "Potato (boiled)", "g", 100, 87, 1.9, 20, 0.1,
          category="vegetable"),
    _food("almonds", "Almonds", "g", 100, 579, 21, 22, 50, category="nuts",
          portions=[{"name": "handful", "base_amount": 28}], aliases=["badam"]),
    _food("peanut-butter", "Peanut butter", "g", 100, 588, 25, 20, 50, category="spread",
          portions=[{"name": "tbsp", "base_amount": 16}]),
]
