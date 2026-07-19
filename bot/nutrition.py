"""Deterministic unit parsing and nutrition arithmetic for saved foods.

The catalog intentionally supports a small set of exact unit conversions.
Named portions are resolved against one food; mass, volume, count, and recipe
servings are never converted across dimensions implicitly.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping, Sequence


MAX_CATALOG_NAME_LENGTH = 100
MAX_PORTION_NAME_LENGTH = 50
MAX_CATALOG_AMOUNT = Decimal("1000000")
MAX_NUTRIENT_VALUE = Decimal("1000000")
MAX_LOG_CALORIES = Decimal("100000")
MAX_LOG_MACRO_GRAMS = Decimal("1000")

FOOD_BASE_UNITS = frozenset({"g", "ml", "piece"})
RECIPE_YIELD_UNITS = frozenset({"g", "ml", "piece", "serving"})
NUTRIENT_FIELDS = ("calories", "protein_g", "carbs_g", "fat_g")

_DECIMAL_TEXT = r"[+]?(?:\d+(?:\.\d*)?|\.\d+)"
_DECIMAL_RE = re.compile(rf"^{_DECIMAL_TEXT}$")
_COMPACT_QUANTITY_RE = re.compile(rf"^({_DECIMAL_TEXT})([^\d\s].*)$")
_NUTRIENT_RE = re.compile(r"^(kcal|cal|p|c|f)=(.*)$", re.IGNORECASE)

# alias -> (canonical dimension/unit, multiplier into that unit)
_STANDARD_UNITS: dict[str, tuple[str, Decimal]] = {
    "g": ("g", Decimal("1")),
    "gm": ("g", Decimal("1")),
    "gms": ("g", Decimal("1")),
    "gram": ("g", Decimal("1")),
    "grams": ("g", Decimal("1")),
    "kg": ("g", Decimal("1000")),
    "kgs": ("g", Decimal("1000")),
    "kilogram": ("g", Decimal("1000")),
    "kilograms": ("g", Decimal("1000")),
    "ml": ("ml", Decimal("1")),
    "milliliter": ("ml", Decimal("1")),
    "milliliters": ("ml", Decimal("1")),
    "millilitre": ("ml", Decimal("1")),
    "millilitres": ("ml", Decimal("1")),
    "l": ("ml", Decimal("1000")),
    "ltr": ("ml", Decimal("1000")),
    "liter": ("ml", Decimal("1000")),
    "liters": ("ml", Decimal("1000")),
    "litre": ("ml", Decimal("1000")),
    "litres": ("ml", Decimal("1000")),
    "piece": ("piece", Decimal("1")),
    "pieces": ("piece", Decimal("1")),
    "pc": ("piece", Decimal("1")),
    "pcs": ("piece", Decimal("1")),
    "each": ("piece", Decimal("1")),
    "serving": ("serving", Decimal("1")),
    "servings": ("serving", Decimal("1")),
    "serve": ("serving", Decimal("1")),
    "serves": ("serving", Decimal("1")),
}


class NutritionError(ValueError):
    """Raised when a catalog name, quantity, unit, or calculation is invalid."""


@dataclass(frozen=True)
class ParsedQuantity:
    """A positive quantity with either a canonical or food-specific unit."""

    amount: Decimal
    unit: str
    unit_key: str
    base_unit: str | None
    base_amount: Decimal | None


def _clean_text(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise NutritionError(f"{field_name} must be text.")
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(character).startswith("C")
        for character in normalized
    ):
        raise NutritionError(f"{field_name} contains unsupported control characters.")
    cleaned = " ".join(normalized.split())
    if not cleaned:
        raise NutritionError(f"{field_name} cannot be empty.")
    if len(cleaned) > max_length:
        raise NutritionError(
            f"{field_name} must be {max_length} characters or less."
        )
    return cleaned


def normalize_catalog_name(
    value: object,
    field_name: str = "Name",
    max_length: int = MAX_CATALOG_NAME_LENGTH,
) -> tuple[str, str]:
    """Return a bounded display name and its Unicode-aware lookup key."""
    display = _clean_text(value, field_name, max_length)
    key = display.casefold()
    if len(key) > max_length:
        raise NutritionError(
            f"{field_name} must be {max_length} characters or less."
        )
    return display, key


def canonical_unit_alias(unit: object) -> tuple[str, Decimal] | None:
    """Return a standard unit and multiplier, or ``None`` for a named portion."""
    display = _clean_text(unit, "Unit", MAX_PORTION_NAME_LENGTH)
    return _STANDARD_UNITS.get(display.casefold())


def is_reserved_portion_name(
    value: object, food_base_unit: str | None = None
) -> bool:
    """Return whether a portion alias duplicates a food's own base dimension."""
    _display, key = normalize_catalog_name(
        value, "Portion name", MAX_PORTION_NAME_LENGTH
    )
    standard = _STANDARD_UNITS.get(key)
    if standard is None:
        return False
    return food_base_unit is None or standard[0] == food_base_unit


def _parse_positive_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value.strip()):
        raise NutritionError(f"{field_name} must be a positive decimal number.")
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise NutritionError(
            f"{field_name} must be a positive decimal number."
        ) from exc
    if not number.is_finite():
        raise NutritionError(f"{field_name} must be finite.")
    if number <= 0:
        raise NutritionError(f"{field_name} must be greater than zero.")
    if float(number) == 0:
        raise NutritionError(f"{field_name} is too small to store reliably.")
    if number > MAX_CATALOG_AMOUNT:
        raise NutritionError(
            f"{field_name} must be {format_decimal(MAX_CATALOG_AMOUNT)} or less."
        )
    return number


def parse_quantity(
    tokens: Sequence[str],
    *,
    allowed_base_units: frozenset[str] = FOOD_BASE_UNITS,
    allow_named: bool = True,
) -> ParsedQuantity:
    """Parse ``220gm``, ``220 gm``, or a food-specific ``1 medium``.

    ``tokens`` must contain exactly the quantity expression. Standard aliases
    are converted to their canonical base amount. A named portion retains its
    key for food-specific resolution.
    """
    if len(tokens) == 1:
        match = _COMPACT_QUANTITY_RE.fullmatch(tokens[0].strip())
        if match is None:
            raise NutritionError(
                "Quantity must include an amount and unit, such as 220g or 1 medium."
            )
        raw_amount, raw_unit = match.groups()
    elif len(tokens) == 2:
        raw_amount, raw_unit = tokens
    else:
        raise NutritionError(
            "Quantity must include one amount and one unit, such as 220 g or 1 medium."
        )

    amount = _parse_positive_decimal(raw_amount, "Quantity")
    unit, unit_key = normalize_catalog_name(
        raw_unit, "Unit", MAX_PORTION_NAME_LENGTH
    )
    standard = _STANDARD_UNITS.get(unit_key)
    if standard is None:
        if not allow_named:
            raise NutritionError(f"Unsupported unit: {unit}.")
        return ParsedQuantity(amount, unit, unit_key, None, None)

    base_unit, multiplier = standard
    if base_unit not in allowed_base_units:
        raise NutritionError(f"Unit {unit} is not valid here.")
    base_amount = amount * multiplier
    if base_amount > MAX_CATALOG_AMOUNT:
        raise NutritionError(
            f"Converted quantity must be {format_decimal(MAX_CATALOG_AMOUNT)} or less."
        )
    return ParsedQuantity(
        amount=amount,
        unit=base_unit,
        unit_key=unit_key,
        base_unit=base_unit,
        base_amount=base_amount,
    )


def parse_nutrient_labels(tokens: Sequence[str]) -> dict[str, float | None]:
    """Parse any non-empty subset of kcal/cal, p, c, and f labels."""
    values: dict[str, float | None] = {
        "calories": None,
        "protein_g": None,
        "carbs_g": None,
        "fat_g": None,
    }
    fields = {
        "kcal": ("calories", "Calories"),
        "cal": ("calories", "Calories"),
        "p": ("protein_g", "Protein"),
        "c": ("carbs_g", "Carbs"),
        "f": ("fat_g", "Fat"),
    }
    seen: set[str] = set()
    if not tokens:
        raise NutritionError(
            "Provide at least one nutrient label: kcal=, p=, c=, or f=."
        )

    for token in tokens:
        match = _NUTRIENT_RE.fullmatch(token.strip())
        if match is None:
            raise NutritionError(
                f"Invalid nutrient label: {token}. Use kcal=, p=, c=, or f=."
            )
        label = match.group(1).casefold()
        field, display_name = fields[label]
        if field in seen:
            raise NutritionError(f"{display_name} was provided more than once.")
        seen.add(field)
        raw_value = match.group(2)
        if not _DECIMAL_RE.fullmatch(raw_value.strip()):
            raise NutritionError(f"{display_name} must be a non-negative decimal.")
        try:
            value = Decimal(raw_value.strip())
        except InvalidOperation as exc:
            raise NutritionError(
                f"{display_name} must be a non-negative decimal."
            ) from exc
        if not value.is_finite():
            raise NutritionError(f"{display_name} must be finite.")
        if value < 0 or value > MAX_NUTRIENT_VALUE:
            raise NutritionError(
                f"{display_name} must be between 0 and "
                f"{format_decimal(MAX_NUTRIENT_VALUE)}."
            )
        if value > 0 and float(value) == 0:
            raise NutritionError(f"{display_name} is too small to store reliably.")
        values[field] = float(value)
    return values


def _decimal_value(value: object, field_name: str, *, positive: bool) -> Decimal:
    if isinstance(value, bool):
        raise NutritionError(f"{field_name} must be numeric.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise NutritionError(f"{field_name} must be numeric.") from exc
    if not number.is_finite():
        raise NutritionError(f"{field_name} must be finite.")
    if positive and number <= 0:
        raise NutritionError(f"{field_name} must be greater than zero.")
    if not positive and number < 0:
        raise NutritionError(f"{field_name} cannot be negative.")
    return number


def resolve_food_base_amount(
    food: Mapping[str, object],
    portions: Sequence[Mapping[str, object]],
    quantity: ParsedQuantity,
) -> Decimal:
    """Resolve a standard unit or food-specific named portion to base units."""
    food_unit = str(food["base_unit"])
    if quantity.base_unit is not None:
        if quantity.base_unit == food_unit:
            assert quantity.base_amount is not None
            return quantity.base_amount

        # A cross-dimension conversion is valid only when the user explicitly
        # configured that standard unit as a portion (for example,
        # ``piece=50g`` on a mass-based egg). Aliases resolve through the
        # canonical unit key, so ``2 pcs`` uses the configured ``piece`` map.
        portion = next(
            (
                row
                for row in portions
                if str(row.get("name_key", "")).casefold()
                in {quantity.unit_key, quantity.base_unit}
            ),
            None,
        )
        if portion is None:
            raise NutritionError(
                f"This food is defined in {food_unit}; "
                f"{quantity.unit} is a different dimension."
            )
        per_portion = _decimal_value(
            portion.get("base_amount"), "Portion amount", positive=True
        )
        assert quantity.base_amount is not None
        resolved = quantity.base_amount * per_portion
        if float(resolved) == 0:
            raise NutritionError("Converted quantity is too small to store reliably.")
        if resolved > MAX_CATALOG_AMOUNT:
            raise NutritionError(
                f"Converted quantity must be "
                f"{format_decimal(MAX_CATALOG_AMOUNT)} or less."
            )
        return resolved

    portion = next(
        (
            row
            for row in portions
            if str(row.get("name_key", "")).casefold() == quantity.unit_key
        ),
        None,
    )
    if portion is None:
        raise NutritionError(
            f"Unknown portion '{quantity.unit}' for {food.get('name', 'this food')}."
        )
    per_portion = _decimal_value(
        portion.get("base_amount"), "Portion amount", positive=True
    )
    resolved = quantity.amount * per_portion
    if float(resolved) == 0:
        raise NutritionError("Converted quantity is too small to store reliably.")
    if resolved > MAX_CATALOG_AMOUNT:
        raise NutritionError(
            f"Converted quantity must be {format_decimal(MAX_CATALOG_AMOUNT)} or less."
        )
    return resolved


def scale_food_nutrients(
    food: Mapping[str, object], base_amount: object
) -> dict[str, Decimal | None]:
    """Scale a food's nutrition basis to a resolved canonical amount."""
    amount = _decimal_value(base_amount, "Food amount", positive=True)
    basis = _decimal_value(food.get("basis_amount"), "Nutrition basis", positive=True)
    factor = amount / basis
    result: dict[str, Decimal | None] = {}
    for field in NUTRIENT_FIELDS:
        raw_value = food.get(field)
        if raw_value is None:
            result[field] = None
            continue
        nutrient = _decimal_value(raw_value, field, positive=False)
        result[field] = nutrient * factor
    return result


def aggregate_recipe_nutrients(
    ingredients: Sequence[Mapping[str, object]],
    batch_factor: object = Decimal("1"),
) -> dict[str, Decimal | None]:
    """Aggregate recipe foods, propagating unknown nutrients per field."""
    if not ingredients:
        raise NutritionError("Recipe has no ingredients.")
    factor = _decimal_value(batch_factor, "Recipe quantity", positive=True)
    totals: dict[str, Decimal | None] = {
        field: Decimal("0") for field in NUTRIENT_FIELDS
    }

    for ingredient in ingredients:
        base_amount = _decimal_value(
            ingredient.get("base_amount"), "Ingredient amount", positive=True
        )
        basis = _decimal_value(
            ingredient.get("food_basis_amount"),
            "Ingredient nutrition basis",
            positive=True,
        )
        ingredient_factor = base_amount / basis
        for field in NUTRIENT_FIELDS:
            if totals[field] is None:
                continue
            raw_value = ingredient.get(f"food_{field}")
            if raw_value is None:
                totals[field] = None
                continue
            nutrient = _decimal_value(raw_value, field, positive=False)
            totals[field] = totals[field] + nutrient * ingredient_factor

    return {
        field: value * factor if value is not None else None
        for field, value in totals.items()
    }


def finalize_log_nutrients(
    nutrients: Mapping[str, object | None],
) -> dict[str, int | float | None]:
    """Round calculated nutrition once and enforce existing diet-log bounds."""
    result: dict[str, int | float | None] = {}
    calories = nutrients.get("calories")
    if calories is None:
        result["calories"] = None
    else:
        calorie_value = _decimal_value(calories, "Calories", positive=False)
        if calorie_value > MAX_LOG_CALORIES:
            raise NutritionError(
                f"Calculated calories exceed {format_decimal(MAX_LOG_CALORIES)}."
            )
        result["calories"] = int(
            calorie_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    for field, display_name in (
        ("protein_g", "Protein"),
        ("carbs_g", "Carbs"),
        ("fat_g", "Fat"),
    ):
        raw_value = nutrients.get(field)
        if raw_value is None:
            result[field] = None
            continue
        value = _decimal_value(raw_value, display_name, positive=False)
        if value > MAX_LOG_MACRO_GRAMS:
            raise NutritionError(
                f"Calculated {display_name.lower()} exceeds "
                f"{format_decimal(MAX_LOG_MACRO_GRAMS)} g."
            )
        result[field] = float(
            value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    return result


def format_decimal(value: object) -> str:
    """Render a finite decimal without exponent or trailing zeroes."""
    number = _decimal_value(value, "Value", positive=False)
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
