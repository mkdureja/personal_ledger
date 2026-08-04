"""Offer to keep a typed meal entry as a saved food.

Every other way of logging a meal starts from something already stored — a
saved food, a recipe, a shared-catalog row. Typing one out is the escape hatch
for everything else, and it costs a name plus four numbers *every time*, because
nothing about it is remembered. That is the single largest source of friction in
this bot for anyone whose ledger is still empty.

The fix is not another command. The moment a typed entry is saved is the only
moment when the name is on screen, the nutrition is known to be complete (the
meal could not have been written otherwise), and the person has just proved they
eat the thing. So the post-save keyboard grows one 💾 row per typed item, and a
tap turns that item into a saved food defined as *one of what you just ate*:

* ``base_unit="piece"``, ``basis_amount=1`` — the entry described one helping,
  not a per-100 g label, and inventing a gram weight nobody supplied would make
  every future log from it silently wrong.
* a named portion ``portion`` = 1 piece, so the amount screen has something to
  tap instead of only "✍️ Custom amount".
* that same ``1 portion`` stored as the usual, which is what promotes the food
  to a one-tap ⚡ row in the picker.

The button is deliberately *not* part of the diet ConversationHandler. It is
registered alongside targeted meal Undo and suggestion Withdraw, outside every
flow: the keyboard stays in the scrollback after the conversation times out, and
a button that quietly stops working is worse than one that was never offered.
Nothing large rides in the callback — it names a meal id and an item position,
and both the name and the four nutrients are read back from ``diet_log_items``
at tap time.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..keyboards import KEEP_FOOD_PREFIX, log_another_keyboard, parse_keep_food
from ..meal_models import DefaultQuantity
from ..nutrition import (
    MAX_CATALOG_NAME_LENGTH,
    NUTRIENT_FIELDS,
    NutritionError,
    normalize_catalog_name,
)
from .common import authorized_callback, escape_html, reply_html

logger = logging.getLogger(__name__)

#: Full-match, base-36 only — a permissive prefix would let a malformed payload
#: reach the owner check with an unparsed target.
KEEP_FOOD_PATTERN = rf"^{KEEP_FOOD_PREFIX}_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$"

#: How the saved food is defined. One typed entry is one helping of the thing;
#: ``piece`` is the only base unit that says that without claiming a weight.
KEEP_BASE_UNIT = "piece"
KEEP_BASIS_AMOUNT = 1.0

#: The named portion added so the amount screen has a tappable row. Must not be
#: a standard unit alias — "serving" is one, and would be refused by the quantity
#: parser for a piece-based food — so the plain word "portion" is used.
KEEP_PORTION_NAME = "portion"

#: At most this many 💾 rows under one meal. A meal typed out item by item would
#: otherwise bury Log another / Done under a wall of offers; the rest of the
#: items can still be saved with ``/food add``.
MAX_KEEP_OFFERS = 3

_FREETEXT = "freetext"


def _item_order(index: int, item: Mapping[str, Any]) -> int:
    """The item's position in its meal, from the row or from the sequence.

    Draft dicts on their way *into* a meal carry no ``item_order`` — the write
    path assigns it from their order — while rows read back out carry their
    own. Both callers land here so an offer and the tap that answers it can
    never disagree about which item was meant.
    """
    raw = item.get("item_order")
    return index if raw is None else int(raw)


def _storable_name(item: Mapping[str, Any]) -> str | None:
    """The item's display name if it could be a food name, else ``None``.

    A typed entry may be a whole sentence: the meal description allows far more
    characters than a catalog name, and offering to save something the catalog
    would then refuse is a button that fails after it is pressed.
    """
    try:
        display, _key = normalize_catalog_name(
            item.get("display_name"), "Food name", MAX_CATALOG_NAME_LENGTH
        )
    except NutritionError:
        return None
    return display


def _is_complete(item: Mapping[str, Any]) -> bool:
    """Whether the item carries all four nutrients a saved food requires."""
    return all(item.get(field) is not None for field in NUTRIENT_FIELDS)


async def keep_food_offers(
    db: Any, user_id: int, items: Sequence[Mapping[str, Any]]
) -> list[tuple[int, str]]:
    """Which items of one meal are worth offering as saved foods.

    Returns ``(item_order, display_name)`` pairs, in meal order, for the typed
    items whose name the catalog would accept and that the user has not saved
    already. Names repeated within the meal are offered once — two identical
    typed items would otherwise draw two buttons that do the same thing, the
    second of which reports "you already have that".
    """
    offers: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(offers) >= MAX_KEEP_OFFERS:
            break
        if str(item.get("source_type") or _FREETEXT) != _FREETEXT:
            continue
        if not _is_complete(item):
            continue
        name = _storable_name(item)
        if name is None:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        if await db.get_food_by_key(user_id, name) is not None:
            continue
        offers.append((_item_order(index, item), name))
    return offers


async def _make_tappable(db: Any, user_id: int, food_id: int) -> bool:
    """Add the ``portion`` portion and store it as the usual amount.

    Best effort on purpose: the food itself is already saved and useful by the
    time this runs, so a failure here downgrades the confirmation message rather
    than turning a completed save into an error.
    """
    try:
        result = await db.save_food_portion(
            user_id, food_id, KEEP_PORTION_NAME, 1, KEEP_BASE_UNIT
        )
        if result.get("status") not in ("added", "updated"):
            return False
        await db.set_default_quantity(
            user_id,
            "food",
            food_id,
            DefaultQuantity(amount=1.0, unit=KEEP_PORTION_NAME),
        )
    except (NutritionError, ValueError):
        logger.warning(
            "Saved food %s kept without a one-tap amount", food_id, exc_info=True
        )
        return False
    return True


def _nutrition_line(item: Mapping[str, Any]) -> str:
    """The ``1 portion`` price of the new food, as it was just logged."""
    calories = item.get("calories")
    parts = []
    if calories is not None:
        parts.append(f"🔥 <b>{int(calories)}</b> cal")
    macros = [
        f"{label} {float(item[field]):g} g"
        for label, field in (("P", "protein_g"), ("C", "carbs_g"), ("F", "fat_g"))
        if item.get(field) is not None
    ]
    if macros:
        parts.append("🥩 " + " · ".join(macros))
    return " · ".join(parts)


async def _retire_offer(query, text: str) -> None:
    """Answer in the chat, leaving the keyboard alone.

    The markup is *not* cleared: the same message carries Log another / Done,
    and removing those to report one save would take away the control the user
    is most likely to want next.
    """
    try:
        await reply_html(query.message, text)
    except TelegramError:
        logger.debug("Could not report a keep-food outcome", exc_info=True)


async def _redraw_offers(query, db: Any, user_id: int, meal_id: int) -> None:
    """Drop the row just used, keeping every other control on the message.

    Recomputed from the meal rather than edited in place, so the button that
    vanishes is exactly the one that no longer has anything to do: the food now
    exists, so :func:`keep_food_offers` stops offering it. Best effort — an
    un-editable message leaves a button that reports "you already have that",
    which is honest, so a failure here is not worth surfacing.
    """
    try:
        items = await db.get_diet_log_items(user_id, meal_id)
        remaining = await keep_food_offers(db, user_id, items)
        await query.edit_message_reply_markup(
            reply_markup=log_another_keyboard(
                user_id,
                meal_id=meal_id if remaining else None,
                keepable=remaining,
            )
        )
    except TelegramError:
        logger.debug("Could not redraw the keep-food offers", exc_info=True)
    except Exception:
        logger.warning("Could not redraw the keep-food offers", exc_info=True)


@authorized_callback
async def keep_food_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """💾 on a just-saved typed entry: store it as a food, priced as logged."""
    query = update.callback_query
    user_id = update.effective_user.id
    target = parse_keep_food(query.data or "", user_id)
    if target is None:
        await query.answer(
            "That button belongs to another user or is no longer valid.",
            show_alert=True,
        )
        return
    meal_id, item_order = target

    db = context.bot_data["db"]
    items = await db.get_diet_log_items(user_id, meal_id)
    item = next(
        (row for row in items if int(row.get("item_order", -1)) == item_order),
        None,
    )
    if item is None:
        # The meal was undone, or the receipt outlived its data.
        await query.answer("That entry is no longer there.", show_alert=True)
        return
    if str(item.get("source_type") or _FREETEXT) != _FREETEXT:
        # Only a typed entry is unsaved by definition; anything else already
        # came from a stored source and saving it again would duplicate it.
        await query.answer("That entry is already stored.", show_alert=True)
        return

    name = _storable_name(item)
    if name is None or not _is_complete(item):
        await query.answer("That entry can't be saved as a food.", show_alert=True)
        return

    await query.answer()

    # Checked again here, not only when the button was drawn: the same offer may
    # be tapped twice, and ``save_food`` would treat the second tap as an edit
    # and overwrite whatever the food had grown into since.
    if await db.get_food_by_key(user_id, name) is not None:
        await _redraw_offers(query, db, user_id, meal_id)
        await _retire_offer(
            query,
            f"💾 You already have a saved food called <b>{escape_html(name)}</b> "
            "— nothing was changed.",
        )
        return

    try:
        result = await db.save_food(
            user_id,
            name,
            KEEP_BASE_UNIT,
            KEEP_BASIS_AMOUNT,
            calories=float(item["calories"]),
            protein_g=float(item["protein_g"]),
            carbs_g=float(item["carbs_g"]),
            fat_g=float(item["fat_g"]),
        )
    except (NutritionError, ValueError) as exc:
        await _retire_offer(query, f"⚠️ {escape_html(str(exc))}")
        return
    except Exception:
        logger.exception("Keeping a typed entry as a food failed; nothing written")
        await _retire_offer(query, "⚠️ Couldn't save that — try again.")
        return

    status = result.get("status")
    if status == "limit":
        await _retire_offer(
            query,
            f"⚠️ You already have {result.get('limit')} saved foods — remove one "
            "with <code>/food remove</code> first.",
        )
        return
    if status == "unit_mismatch":
        await _retire_offer(
            query,
            f"💾 A saved food called <b>{escape_html(name)}</b> is defined in "
            f"{escape_html(result.get('expected_unit'))} — nothing was changed.",
        )
        return

    food = result.get("food") or {}
    tappable = await _make_tappable(db, user_id, food.get("id"))
    amount = f"1 {KEEP_PORTION_NAME}" if tappable else f"1 {KEEP_BASE_UNIT}"
    closing = (
        "It's in your meal picker now — one tap logs it."
        if tappable
        else f"It's in your meal picker now; ask for <code>1 {KEEP_BASE_UNIT}</code>."
    )
    await _redraw_offers(query, db, user_id, meal_id)
    await _retire_offer(
        query,
        f"💾 <b>Saved {escape_html(name)}</b> to your foods.\n"
        f"{escape_html(amount)} = {_nutrition_line(item)}\n{closing}",
    )


#: The keep-food button, for registration outside every conversation.
keep_food_handler = CallbackQueryHandler(keep_food_callback, pattern=KEEP_FOOD_PATTERN)
