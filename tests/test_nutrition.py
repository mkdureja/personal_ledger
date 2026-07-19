"""Unit and arithmetic tests for saved-food nutrition calculations."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.nutrition import (
    FOOD_BASE_UNITS,
    RECIPE_YIELD_UNITS,
    NutritionError,
    aggregate_recipe_nutrients,
    finalize_log_nutrients,
    is_reserved_portion_name,
    normalize_catalog_name,
    parse_nutrient_labels,
    parse_quantity,
    resolve_food_base_amount,
    scale_food_nutrients,
)


@pytest.mark.parametrize(
    ("tokens", "base_unit", "base_amount"),
    [
        (["220gm"], "g", Decimal("220")),
        (["220", "grams"], "g", Decimal("220")),
        (["0.22", "kg"], "g", Decimal("220.00")),
        (["1.5l"], "ml", Decimal("1500.0")),
        (["2", "pcs"], "piece", Decimal("2")),
        (["0.5", "servings"], "serving", Decimal("0.5")),
    ],
)
def test_quantity_aliases_convert_to_canonical_units(
    tokens: list[str], base_unit: str, base_amount: Decimal
) -> None:
    allowed = RECIPE_YIELD_UNITS
    parsed = parse_quantity(tokens, allowed_base_units=allowed)

    assert parsed.base_unit == base_unit
    assert parsed.base_amount == base_amount


def test_named_portion_is_deferred_to_the_food_definition() -> None:
    parsed = parse_quantity(["1", "Medium"])

    assert parsed.base_unit is None
    assert parsed.base_amount is None
    assert parsed.unit_key == "medium"

    amount = resolve_food_base_amount(
        {"name": "Apple", "base_unit": "g"},
        [{"name_key": "medium", "base_amount": 182.0}],
        parsed,
    )
    assert amount == Decimal("182.0")


def test_named_portions_are_food_specific_and_exact() -> None:
    quantity = parse_quantity(["2", "medium"])

    with pytest.raises(NutritionError, match="Unknown portion"):
        resolve_food_base_amount(
            {"name": "Orange", "base_unit": "g"},
            [{"name_key": "large", "base_amount": 220}],
            quantity,
        )


def test_cross_dimension_conversion_is_rejected() -> None:
    quantity = parse_quantity(["220ml"])

    with pytest.raises(NutritionError, match="different dimension"):
        resolve_food_base_amount(
            {"name": "Apple", "base_unit": "g"}, [], quantity
        )


def test_explicit_standard_unit_portion_allows_cross_dimension_conversion() -> None:
    quantity = parse_quantity(["2", "pcs"])

    amount = resolve_food_base_amount(
        {"name": "Egg", "base_unit": "g"},
        [{"name_key": "piece", "base_amount": 50}],
        quantity,
    )

    assert amount == Decimal("100")


def test_scaled_standard_alias_uses_canonical_portion_mapping() -> None:
    quantity = parse_quantity(["1kg"], allowed_base_units=RECIPE_YIELD_UNITS)

    amount = resolve_food_base_amount(
        {"name": "Soup", "base_unit": "ml"},
        [{"name_key": "g", "base_amount": 1}],
        quantity,
    )

    assert amount == Decimal("1000")


@pytest.mark.parametrize(
    "tokens",
    [
        [],
        ["220"],
        ["0g"],
        ["-1", "g"],
        ["nan", "g"],
        ["inf", "g"],
        [f".{('0' * 400)}1g"],
        ["1000001g"],
        ["1", "g", "extra"],
    ],
)
def test_invalid_quantities_are_rejected(tokens: list[str]) -> None:
    with pytest.raises(NutritionError):
        parse_quantity(tokens)


def test_food_quantity_rejects_recipe_servings() -> None:
    with pytest.raises(NutritionError, match="not valid"):
        parse_quantity(["1serving"], allowed_base_units=FOOD_BASE_UNITS)


def test_nutrient_labels_allow_partial_values_and_zero() -> None:
    values = parse_nutrient_labels(["KCAL=52", "p=0", "C=13.8"])

    assert values == {
        "calories": 52.0,
        "protein_g": 0.0,
        "carbs_g": 13.8,
        "fat_g": None,
    }


@pytest.mark.parametrize(
    "tokens",
    [
        [],
        ["x=1"],
        ["kcal=-1"],
        ["p=nan"],
        [f"p=.{'0' * 400}1"],
        ["cal=1", "kcal=2"],
    ],
)
def test_invalid_or_duplicate_nutrient_labels_are_rejected(
    tokens: list[str],
) -> None:
    with pytest.raises(NutritionError):
        parse_nutrient_labels(tokens)


def test_food_nutrients_scale_from_the_configured_basis() -> None:
    nutrients = scale_food_nutrients(
        {
            "basis_amount": 100,
            "calories": 52,
            "protein_g": 0.3,
            "carbs_g": 14,
            "fat_g": None,
        },
        Decimal("220"),
    )

    assert nutrients == {
        "calories": Decimal("114.4"),
        "protein_g": Decimal("0.66"),
        "carbs_g": Decimal("30.8"),
        "fat_g": None,
    }


def test_recipe_aggregation_propagates_unknown_nutrients() -> None:
    ingredients = [
        {
            "base_amount": 200,
            "food_basis_amount": 100,
            "food_calories": 50,
            "food_protein_g": 5,
            "food_carbs_g": 10,
            "food_fat_g": 1,
        },
        {
            "base_amount": 50,
            "food_basis_amount": 100,
            "food_calories": 100,
            "food_protein_g": None,
            "food_carbs_g": 20,
            "food_fat_g": 2,
        },
    ]

    nutrients = aggregate_recipe_nutrients(ingredients, Decimal("0.5"))

    assert nutrients["calories"] == Decimal("75.00")
    assert nutrients["protein_g"] is None
    assert nutrients["carbs_g"] == Decimal("15.00")
    assert nutrients["fat_g"] == Decimal("1.50")


def test_empty_recipe_is_not_loggable() -> None:
    with pytest.raises(NutritionError, match="no ingredients"):
        aggregate_recipe_nutrients([])


def test_final_rounding_is_half_up_and_applied_once() -> None:
    finalized = finalize_log_nutrients(
        {
            "calories": Decimal("52.5"),
            "protein_g": Decimal("1.005"),
            "carbs_g": Decimal("2.004"),
            "fat_g": None,
        }
    )

    assert finalized == {
        "calories": 53,
        "protein_g": 1.01,
        "carbs_g": 2.0,
        "fat_g": None,
    }


@pytest.mark.parametrize(
    "nutrients",
    [
        {"calories": Decimal("100000.01")},
        {"protein_g": Decimal("1000.01")},
        {"carbs_g": Decimal("Infinity")},
        {"fat_g": Decimal("-0.01")},
    ],
)
def test_final_log_bounds_are_enforced(nutrients: dict[str, Decimal]) -> None:
    with pytest.raises(NutritionError):
        finalize_log_nutrients(nutrients)


def test_catalog_keys_are_nfkc_casefolded_and_control_characters_rejected() -> None:
    display, key = normalize_catalog_name("  APPLE\u3000Bowl  ")
    assert display == "APPLE Bowl"
    assert key == "apple bowl"

    for invalid in ("apple\x00hidden", "apple\nadmin", "apple\tadmin"):
        with pytest.raises(NutritionError, match="control"):
            normalize_catalog_name(invalid)


@pytest.mark.parametrize("name", ["g", "GRAMS", "kg", "ml", "pcs", "servings"])
def test_standard_aliases_cannot_be_reused_as_portion_names(name: str) -> None:
    assert is_reserved_portion_name(name)


def test_standard_alias_can_be_an_explicit_cross_dimension_portion() -> None:
    assert is_reserved_portion_name("grams", "g")
    assert not is_reserved_portion_name("piece", "g")
    assert not is_reserved_portion_name("serving", "g")
