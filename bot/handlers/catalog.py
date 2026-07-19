"""Stateless ``/food`` and ``/recipe`` catalog commands.

Catalog references are deliberately explicit (``food:key`` and
``recipe:key``).  This keeps the long-standing free-text ``/diet`` grammar
unambiguous while allowing callers to scale saved nutrition definitions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Iterable, Sequence

from telegram import Update
from telegram.ext import ContextTypes

from .. import nutrition
from .common import active_conversation_flow, escape_html, reply_html


_CATALOG_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,48}[a-z0-9])?$")
_MAX_OUTPUT_UTF16 = 3_800
_MAX_DYNAMIC_TEXT = 120
_MAX_ERROR_TEXT = 500

_FOOD_USAGE = (
    "🥗 <b>Saved foods</b>\n"
    "<code>/food add &lt;key&gt; per=&lt;qtyunit&gt; "
    "[kcal=&lt;n&gt; p=&lt;g&gt; c=&lt;g&gt; f=&lt;g&gt;]</code>\n"
    "<code>/food portion &lt;key&gt; &lt;portion&gt;=&lt;qtyunit&gt;</code>\n"
    "<code>/food unportion &lt;key&gt; &lt;portion&gt;</code>\n"
    "<code>/food list</code> · <code>/food show &lt;key&gt;</code> · "
    "<code>/food remove &lt;key&gt;</code>\n\n"
    "Use one-token ASCII keys; kebab-case is recommended. At least one "
    "nutrient is required. Saving an existing key replaces its profile; "
    "omitted nutrients become unknown."
)

_RECIPE_USAGE = (
    "🍲 <b>Saved recipes</b>\n"
    "<code>/recipe add &lt;key&gt; yield=&lt;qtyunit&gt;</code>\n"
    "<code>/recipe ingredient &lt;recipe&gt; food:&lt;food&gt; "
    "&lt;qtyunit&gt;</code> (attached or spaced)\n"
    "<code>/recipe removeitem &lt;recipe&gt; food:&lt;food&gt;</code>\n"
    "<code>/recipe list</code> · <code>/recipe show &lt;key&gt;</code> · "
    "<code>/recipe remove &lt;key&gt;</code>"
)


@dataclass(frozen=True)
class ResolvedCatalogDietEntry:
    """A calculated catalog item ready to be snapshotted into ``diet_logs``."""

    display_text: str
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _bounded_html(value: object, max_chars: int = _MAX_DYNAMIC_TEXT) -> str:
    text = str(value)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return escape_html(text)


def _bounded_plain(value: object, max_chars: int = _MAX_ERROR_TEXT) -> str:
    text = str(value)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


async def _reply_chunked(message: Any, heading: str, lines: Iterable[str]) -> None:
    """Send escaped/bounded catalog rows without exceeding Telegram's limit."""
    chunks: list[str] = []
    current = heading
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if current and _utf16_length(candidate) > _MAX_OUTPUT_UTF16:
            chunks.append(current)
            current = f"{heading} <i>(continued)</i>\n{line}"
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        await reply_html(message, chunk)


async def _command_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ensure the user exists and reject commands during a guided flow."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)
    if active_conversation_flow(context) is not None:
        await update.effective_message.reply_text(
            "⏳ Finish the current guided log or use /cancel in the chat where "
            "it started before managing saved foods and recipes."
        )
        return None
    return db, user.id


def _catalog_key(raw: str, label: str) -> str:
    _display, key = nutrition.normalize_catalog_name(
        raw, label, max_length=50
    )
    if not _CATALOG_KEY_RE.fullmatch(key):
        raise nutrition.NutritionError(
            f"{label} must be one token using letters, numbers, hyphens, or "
            "underscores (kebab-case is recommended)."
        )
    return key


def _reference_key(token: str, prefix: str) -> str:
    marker = f"{prefix}:"
    if not token.casefold().startswith(marker) or len(token) <= len(marker):
        raise nutrition.NutritionError(f"Use an explicit {prefix}:<key> reference.")
    return _catalog_key(token[len(marker) :], prefix.title())


def _request_for_item(quantity_tokens: Sequence[str]):
    if not quantity_tokens:
        raise nutrition.NutritionError("A quantity and unit are required.")
    return nutrition.parse_quantity(
        quantity_tokens,
        # ``serving`` may be an explicitly configured food portion even
        # though it is not a food's native storage dimension.
        allowed_base_units=nutrition.RECIPE_YIELD_UNITS,
        allow_named=True,
    )


def _parse_bare_quantity(raw: str, *, recipe: bool = False):
    """Parse an amount/unit assignment through the shared quantity parser."""
    allowed = (
        nutrition.RECIPE_YIELD_UNITS if recipe else nutrition.FOOD_BASE_UNITS
    )
    return nutrition.parse_quantity(
        [raw], allowed_base_units=allowed, allow_named=False
    )


def _format_decimal(value: object) -> str:
    return nutrition.format_decimal(value)


def _quantity_display(request: Any) -> tuple[object, str]:
    """Return an amount paired with the unit that amount actually represents."""
    if request.base_unit is not None:
        return request.amount, request.unit_key
    return request.amount, request.unit


def _recipe_ingredient_line(row: dict[str, Any]) -> str:
    """Render both the entered quantity and any immutable resolution detail."""
    display_unit = row["display_unit"]
    standard = nutrition.canonical_unit_alias(display_unit)
    same_dimension = standard is not None and standard[0] == row["food_base_unit"]
    resolved = ""
    if not same_dimension:
        resolved = (
            " <i>(resolved: "
            f"{_bounded_html(_format_decimal(row['base_amount']))} "
            f"{_bounded_html(row['food_base_unit'])})</i>"
        )
    archived = ""
    if not row["food_is_active"]:
        archived = " <i>(archived food definition)</i>"
    return (
        f"• {_bounded_html(_format_decimal(row['display_amount']))} "
        f"{_bounded_html(display_unit)}{resolved} "
        f"<code>{_bounded_html(row['food_key'])}</code>{archived}"
    )


def _format_nutrient_number(value: object) -> str:
    """Bound nutrient precision without turning small positives into zero."""
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        return "?"
    if number == 0:
        return "0"
    if number < Decimal("0.01") or number >= Decimal("1000000"):
        with localcontext() as context:
            context.rounding = ROUND_HALF_UP
            return format(number, ".6g").lower()
    try:
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        with localcontext() as context:
            context.rounding = ROUND_HALF_UP
            return format(number, ".6g").lower()
    return _format_decimal(rounded)


def _nutrient_values(tokens: Sequence[str]) -> dict[str, float | None]:
    """Parse labeled nutrients and adapt the shared result to DB field names."""
    return nutrition.parse_nutrient_labels(tokens)


def _nutrient_summary(row: dict[str, Any], prefix: str = "") -> str:
    fields = (
        ("calories", "kcal"),
        ("protein_g", "P"),
        ("carbs_g", "C"),
        ("fat_g", "F"),
    )
    parts: list[str] = []
    for field, label in fields:
        value = row.get(f"{prefix}{field}")
        if value is None:
            parts.append(f"{label} ?")
        else:
            suffix = "" if field == "calories" else "g"
            parts.append(
                f"{label} {_bounded_html(_format_nutrient_number(value))}{suffix}"
            )
    return " · ".join(parts)


def _status_error(result: dict[str, Any], noun: str) -> str:
    status = result.get("status")
    if status == "limit":
        return f"❌ {noun.title()} limit reached ({result.get('limit', '?')})."
    if status == "unit_mismatch":
        expected = _bounded_html(result.get("expected_unit", "?"))
        provided = _bounded_html(result.get("provided_unit", "?"))
        return f"❌ Unit mismatch: expected {expected}, received {provided}."
    return f"❌ {noun.title()} was not found."


async def food_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch the stateless saved-food command surface."""
    prepared = await _command_context(update, context)
    if prepared is None:
        return
    db, user_id = prepared
    args = list(context.args or [])
    if not args:
        await reply_html(update.message, _FOOD_USAGE)
        return

    action = args[0].casefold()
    try:
        if action == "add":
            await _food_add(update.message, db, user_id, args)
        elif action == "portion":
            await _food_portion(update.message, db, user_id, args)
        elif action == "unportion":
            await _food_unportion(update.message, db, user_id, args)
        elif action == "list":
            await _food_list(update.message, db, user_id, args)
        elif action == "show":
            await _food_show(update.message, db, user_id, args)
        elif action == "remove":
            await _food_remove(update.message, db, user_id, args)
        else:
            await reply_html(update.message, _FOOD_USAGE)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {_bounded_plain(exc)}")


async def _food_add(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) < 4:
        raise nutrition.NutritionError(
            "Usage: /food add <key> per=<qtyunit> [kcal= p= c= f=]."
        )
    key = _catalog_key(args[1], "Food key")
    per_tokens = [token for token in args[2:] if token.casefold().startswith("per=")]
    if len(per_tokens) != 1 or not per_tokens[0][4:]:
        raise nutrition.NutritionError("Provide exactly one per=<qtyunit> basis.")
    per_index = next(
        index
        for index, token in enumerate(args[2:])
        if token.casefold().startswith("per=")
    )
    nutrient_tokens = [
        token for index, token in enumerate(args[2:]) if index != per_index
    ]
    nutrients = _nutrient_values(nutrient_tokens)
    quantity = _parse_bare_quantity(per_tokens[0][4:])
    assert quantity.base_unit is not None and quantity.base_amount is not None

    result = await db.save_food(
        user_id,
        key,
        quantity.base_unit,
        float(quantity.base_amount),
        **nutrients,
    )
    food = result.get("food")
    if food is None:
        await reply_html(message, _status_error(result, "food"))
        return
    verb = "Added" if result.get("status") == "added" else "Updated"
    await reply_html(
        message,
        f"✅ <b>{verb} food:</b> <code>{_bounded_html(food['name_key'])}</code>\n"
        f"Per {_bounded_html(_format_decimal(food['basis_amount']))} "
        f"{_bounded_html(food['base_unit'])}: {_nutrient_summary(food)}\n"
        f"Log with <code>/diet snack food:{_bounded_html(food['name_key'])} "
        "&lt;qty&gt; &lt;unit-or-portion&gt;</code>",
    )


async def _food_portion(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 3 or "=" not in args[2]:
        raise nutrition.NutritionError(
            "Usage: /food portion <key> <portion>=<qtyunit>."
        )
    food_key = _catalog_key(args[1], "Food key")
    portion_raw, quantity_raw = args[2].split("=", 1)
    if not quantity_raw:
        raise nutrition.NutritionError("Portion quantity cannot be empty.")
    food = await db.get_food_by_key(user_id, food_key)
    if food is None:
        raise nutrition.NutritionError(f"Saved food '{food_key}' was not found.")
    standard_portion = nutrition.canonical_unit_alias(portion_raw)
    if nutrition.is_reserved_portion_name(portion_raw, food["base_unit"]):
        raise nutrition.NutritionError(
            f"'{portion_raw}' duplicates this food's standard base unit."
        )
    portion_key = (
        standard_portion[0]
        if standard_portion is not None
        else _catalog_key(portion_raw, "Portion")
    )
    portion_name = portion_raw if standard_portion is not None else portion_key
    quantity = _parse_bare_quantity(quantity_raw)
    assert quantity.base_unit is not None and quantity.base_amount is not None
    result = await db.save_food_portion(
        user_id,
        food["id"],
        portion_name,
        float(quantity.base_amount),
        quantity.base_unit,
    )
    portion = result.get("portion")
    if portion is None:
        await reply_html(message, _status_error(result, "portion"))
        return
    verb = "Added" if result.get("status") == "added" else "Updated"
    await reply_html(
        message,
        f"✅ <b>{verb} portion:</b> 1 {_bounded_html(portion['name'])} of "
        f"<code>{_bounded_html(food_key)}</code> = "
        f"{_bounded_html(_format_decimal(portion['base_amount']))} "
        f"{_bounded_html(portion['food_base_unit'])}.",
    )


async def _food_unportion(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 3:
        raise nutrition.NutritionError("Usage: /food unportion <key> <portion>.")
    food_key = _catalog_key(args[1], "Food key")
    standard_portion = nutrition.canonical_unit_alias(args[2])
    portion_key = (
        standard_portion[0]
        if standard_portion is not None
        else _catalog_key(args[2], "Portion")
    )
    food = await db.get_food_by_key(user_id, food_key)
    if food is None:
        raise nutrition.NutritionError(f"Saved food '{food_key}' was not found.")
    portions = await db.get_food_portions(user_id, food["id"])
    portion = next((row for row in portions if row["name_key"] == portion_key), None)
    if portion is None:
        raise nutrition.NutritionError(
            f"Portion '{portion_key}' was not found on '{food_key}'."
        )
    result = await db.remove_food_portion(user_id, food["id"], portion["id"])
    if result.get("status") != "updated":
        await reply_html(message, _status_error(result, "portion"))
        return
    await reply_html(
        message,
        f"🗑️ Removed portion <b>{_bounded_html(portion['name'])}</b> from "
        f"<code>{_bounded_html(food_key)}</code>.",
    )


async def _food_list(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 1:
        raise nutrition.NutritionError("Usage: /food list.")
    foods = await db.list_foods(user_id)
    if not foods:
        await reply_html(message, "🥗 No saved foods. Use <code>/food add</code>.")
        return
    lines = [
        f"• <code>{_bounded_html(food['name_key'])}</code> — per "
        f"{_bounded_html(_format_decimal(food['basis_amount']))} "
        f"{_bounded_html(food['base_unit'])} · {_nutrient_summary(food)}"
        for food in foods
    ]
    await _reply_chunked(message, f"🥗 <b>Saved foods ({len(foods)})</b>", lines)


async def _food_show(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 2:
        raise nutrition.NutritionError("Usage: /food show <key>.")
    key = _catalog_key(args[1], "Food key")
    food = await db.get_food_by_key(user_id, key)
    if food is None:
        raise nutrition.NutritionError(f"Saved food '{key}' was not found.")
    portions = await db.get_food_portions(user_id, food["id"])
    lines = [
        f"Basis: {_bounded_html(_format_decimal(food['basis_amount']))} "
        f"{_bounded_html(food['base_unit'])}",
        f"Nutrition: {_nutrient_summary(food)}",
    ]
    if portions:
        lines.append("<b>Portions</b>")
        lines.extend(
            f"• {_bounded_html(row['name'])} = "
            f"{_bounded_html(_format_decimal(row['base_amount']))} "
            f"{_bounded_html(row['food_base_unit'])}"
            for row in portions
        )
    else:
        lines.append("Portions: none")
    await _reply_chunked(
        message, f"🥗 <b>{_bounded_html(food['name'])}</b>", lines
    )


async def _food_remove(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 2:
        raise nutrition.NutritionError("Usage: /food remove <key>.")
    key = _catalog_key(args[1], "Food key")
    food = await db.get_food_by_key(user_id, key)
    if food is None:
        raise nutrition.NutritionError(f"Saved food '{key}' was not found.")
    result = await db.archive_food(user_id, food["id"])
    if result.get("status") != "updated":
        await reply_html(message, _status_error(result, "food"))
        return
    await reply_html(message, f"🗑️ Archived food <code>{_bounded_html(key)}</code>.")


async def recipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch the stateless saved-recipe command surface."""
    prepared = await _command_context(update, context)
    if prepared is None:
        return
    db, user_id = prepared
    args = list(context.args or [])
    if not args:
        await reply_html(update.message, _RECIPE_USAGE)
        return

    action = args[0].casefold()
    try:
        if action == "add":
            await _recipe_add(update.message, db, user_id, args)
        elif action == "ingredient":
            await _recipe_ingredient(update.message, db, user_id, args)
        elif action == "removeitem":
            await _recipe_removeitem(update.message, db, user_id, args)
        elif action == "list":
            await _recipe_list(update.message, db, user_id, args)
        elif action == "show":
            await _recipe_show(update.message, db, user_id, args)
        elif action == "remove":
            await _recipe_remove(update.message, db, user_id, args)
        else:
            await reply_html(update.message, _RECIPE_USAGE)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {_bounded_plain(exc)}")


async def _recipe_add(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 3 or not args[2].casefold().startswith("yield="):
        raise nutrition.NutritionError("Usage: /recipe add <key> yield=<qtyunit>.")
    key = _catalog_key(args[1], "Recipe key")
    raw_yield = args[2][len("yield=") :]
    if not raw_yield:
        raise nutrition.NutritionError("Recipe yield cannot be empty.")
    quantity = _parse_bare_quantity(raw_yield, recipe=True)
    assert quantity.base_unit is not None and quantity.base_amount is not None
    result = await db.save_recipe(
        user_id, key, float(quantity.base_amount), quantity.base_unit
    )
    recipe = result.get("recipe")
    if recipe is None:
        await reply_html(message, _status_error(result, "recipe"))
        return
    verb = "Added" if result.get("status") == "added" else "Updated"
    await reply_html(
        message,
        f"✅ <b>{verb} recipe:</b> <code>{_bounded_html(recipe['name_key'])}</code>\n"
        f"Yield: {_bounded_html(_format_decimal(recipe['yield_amount']))} "
        f"{_bounded_html(recipe['yield_unit'])}\n"
        "Add ingredients with <code>/recipe ingredient "
        f"{_bounded_html(recipe['name_key'])} food:&lt;food&gt; &lt;qty&gt; "
        "&lt;unit-or-portion&gt;</code>.",
    )


async def _recipe_ingredient(
    message: Any, db: Any, user_id: int, args: list[str]
) -> None:
    if len(args) not in {4, 5}:
        raise nutrition.NutritionError(
            "Usage: /recipe ingredient <recipe> food:<food> "
            "<qtyunit> or <qty> <unit-or-portion>."
        )
    recipe_key = _catalog_key(args[1], "Recipe key")
    food_key = _reference_key(args[2], "food")
    recipe = await db.get_recipe_by_key(user_id, recipe_key)
    food = await db.get_food_by_key(user_id, food_key)
    if recipe is None:
        raise nutrition.NutritionError(f"Saved recipe '{recipe_key}' was not found.")
    if food is None:
        raise nutrition.NutritionError(f"Saved food '{food_key}' was not found.")
    portions = await db.get_food_portions(user_id, food["id"])
    request = _request_for_item(args[3:])
    resolved_amount = nutrition.resolve_food_base_amount(food, portions, request)
    result = await db.save_recipe_ingredient(
        user_id,
        recipe["id"],
        food["id"],
        float(resolved_amount),
        food["base_unit"],
        float(request.amount),
        request.unit_key,
    )
    ingredient = result.get("ingredient")
    if ingredient is None:
        await reply_html(message, _status_error(result, "ingredient"))
        return
    verb = "Added" if result.get("status") == "added" else "Updated"
    await reply_html(
        message,
        f"✅ <b>{verb} ingredient:</b> "
        f"{_bounded_html(_format_decimal(ingredient['display_amount']))} "
        f"{_bounded_html(ingredient['display_unit'])} "
        f"<code>{_bounded_html(ingredient['food_key'])}</code> in "
        f"<code>{_bounded_html(recipe_key)}</code>.",
    )


async def _recipe_removeitem(
    message: Any, db: Any, user_id: int, args: list[str]
) -> None:
    if len(args) != 3:
        raise nutrition.NutritionError(
            "Usage: /recipe removeitem <recipe> food:<food>."
        )
    recipe_key = _catalog_key(args[1], "Recipe key")
    food_key = _reference_key(args[2], "food")
    recipe = await db.get_recipe_by_key(user_id, recipe_key)
    if recipe is None:
        raise nutrition.NutritionError(f"Saved recipe '{recipe_key}' was not found.")
    ingredients = await db.get_recipe_ingredients(user_id, recipe["id"])
    ingredient = next(
        (row for row in ingredients if row["food_key"] == food_key), None
    )
    if ingredient is None:
        raise nutrition.NutritionError(
            f"Food '{food_key}' is not an ingredient in '{recipe_key}'."
        )
    result = await db.remove_recipe_ingredient(
        user_id, recipe["id"], ingredient["id"]
    )
    if result.get("status") != "updated":
        await reply_html(message, _status_error(result, "ingredient"))
        return
    await reply_html(
        message,
        f"🗑️ Removed <code>{_bounded_html(food_key)}</code> from "
        f"<code>{_bounded_html(recipe_key)}</code>.",
    )


async def _recipe_list(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 1:
        raise nutrition.NutritionError("Usage: /recipe list.")
    recipes = await db.list_recipes(user_id)
    if not recipes:
        await reply_html(message, "🍲 No saved recipes. Use <code>/recipe add</code>.")
        return
    lines = [
        f"• <code>{_bounded_html(row['name_key'])}</code> — yield "
        f"{_bounded_html(_format_decimal(row['yield_amount']))} "
        f"{_bounded_html(row['yield_unit'])}"
        for row in recipes
    ]
    await _reply_chunked(message, f"🍲 <b>Saved recipes ({len(recipes)})</b>", lines)


async def _recipe_show(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 2:
        raise nutrition.NutritionError("Usage: /recipe show <key>.")
    key = _catalog_key(args[1], "Recipe key")
    recipe = await db.get_recipe_by_key(user_id, key)
    if recipe is None:
        raise nutrition.NutritionError(f"Saved recipe '{key}' was not found.")
    ingredients = await db.get_recipe_ingredients(user_id, recipe["id"])
    lines = [
        f"Yield: {_bounded_html(_format_decimal(recipe['yield_amount']))} "
        f"{_bounded_html(recipe['yield_unit'])}"
    ]
    if not ingredients:
        lines.append("Ingredients: none (this recipe cannot be logged yet)")
    else:
        lines.append("<b>Ingredients</b>")
        lines.extend(_recipe_ingredient_line(row) for row in ingredients)
        totals = nutrition.aggregate_recipe_nutrients(ingredients)
        lines.append(f"Total nutrition: {_nutrient_summary(totals)}")
    await _reply_chunked(
        message, f"🍲 <b>{_bounded_html(recipe['name'])}</b>", lines
    )


async def _recipe_remove(message: Any, db: Any, user_id: int, args: list[str]) -> None:
    if len(args) != 2:
        raise nutrition.NutritionError("Usage: /recipe remove <key>.")
    key = _catalog_key(args[1], "Recipe key")
    recipe = await db.get_recipe_by_key(user_id, key)
    if recipe is None:
        raise nutrition.NutritionError(f"Saved recipe '{key}' was not found.")
    result = await db.archive_recipe(user_id, recipe["id"])
    if result.get("status") != "updated":
        await reply_html(message, _status_error(result, "recipe"))
        return
    await reply_html(message, f"🗑️ Archived recipe <code>{_bounded_html(key)}</code>.")


async def resolve_catalog_diet_entry(
    db: Any,
    user_id: int,
    reference_token: str,
    quantity_tokens: Sequence[str],
) -> ResolvedCatalogDietEntry:
    """Resolve an exact active catalog reference without writing a diet log."""
    prefix, separator, _key = reference_token.partition(":")
    if not separator or prefix.casefold() not in {"food", "recipe"}:
        raise nutrition.NutritionError(
            "Use food:<key> or recipe:<key> for a saved catalog entry."
        )

    if prefix.casefold() == "food":
        key = _reference_key(reference_token, "food")
        food = await db.get_food_by_key(user_id, key)
        if food is None:
            raise nutrition.NutritionError(f"Saved food '{key}' was not found.")
        portions = await db.get_food_portions(user_id, food["id"])
        request = _request_for_item(quantity_tokens)
        resolved_amount = nutrition.resolve_food_base_amount(food, portions, request)
        nutrients = nutrition.scale_food_nutrients(food, resolved_amount)
        finalized = nutrition.finalize_log_nutrients(nutrients)
        display_amount, display_unit = _quantity_display(request)
        display = f"{_format_decimal(display_amount)} {display_unit} {food['name']}"
    else:
        key = _reference_key(reference_token, "recipe")
        recipe = await db.get_recipe_by_key(user_id, key)
        if recipe is None:
            raise nutrition.NutritionError(f"Saved recipe '{key}' was not found.")
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
        ingredients = await db.get_recipe_ingredients(user_id, recipe["id"])
        if not ingredients:
            raise nutrition.NutritionError(f"Recipe '{key}' has no ingredients.")
        assert request.base_amount is not None
        batch_factor = request.base_amount / Decimal(str(recipe["yield_amount"]))
        scaled = nutrition.aggregate_recipe_nutrients(ingredients, batch_factor)
        finalized = nutrition.finalize_log_nutrients(scaled)
        display_amount, display_unit = _quantity_display(request)
        display = (
            f"{_format_decimal(display_amount)} {display_unit} "
            f"{recipe['name']} (recipe)"
        )

    return ResolvedCatalogDietEntry(
        display_text=display,
        calories=finalized.get("calories"),
        protein_g=finalized.get("protein_g"),
        carbs_g=finalized.get("carbs_g"),
        fat_g=finalized.get("fat_g"),
    )
