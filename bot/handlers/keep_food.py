"""Keep a typed meal entry as a saved food.

Every other way of logging a meal starts from something already stored — a
saved food, a recipe, a shared-catalog row. Typing one out is the escape hatch
for everything else, and it costs a name plus four numbers *every time*, because
nothing about it is remembered. That is the single largest source of friction in
this bot for anyone whose ledger is still empty.

The fix is not another command. The moment a typed entry is saved is the only
moment when the name is on screen, the nutrition is known to be complete (the
meal could not have been written otherwise), and the person has just proved they
eat the thing. So each typed item of a saved meal is kept **automatically**, as
a food defined as *one of what you just ate*:

* ``base_unit="piece"``, ``basis_amount=1`` — the entry described one helping,
  not a per-100 g label, and inventing a gram weight nobody supplied would make
  every future log from it silently wrong.
* a named portion ``portion`` = 1 piece, so the amount screen has something to
  tap instead of only "✍️ Custom amount".
* that same ``1 portion`` stored as the usual, which is what promotes the food
  to a one-tap ⚡ row in the picker.

Keeping it automatically rather than offering a 💾 button is a deliberate
reversal. The button was there and worked; what it needed was for someone to
notice it in the same second they had just finished logging, which is exactly
when attention leaves. Three typed meals in one day went unsaved that way, so
the same food was typed again the next day. The safer default is the one where
the ledger remembers: keeping something you will not eat again costs a row in a
list capped at 500 and a 🗑 tap to undo, while *not* keeping it costs a name and
four numbers every single time.

So the post-save keyboard now says what was kept and offers 🗑 to drop it.
The 💾 offer survives for anything the automatic pass could not keep — a
duplicate name, a full food list — because a failed automatic save should leave
the manual path visible rather than nothing at all.

The automatic pass runs from the guided flow's post-save keyboard only. The
one-shot ``/diet lunch pasta 500 p=… c=… f=…`` form answers with a receipt that
carries no room for a 🗑 row, and keeping a food with no visible way to undo it
is exactly the trade this module refuses to make in the other direction.

Both buttons are deliberately *not* part of the diet ConversationHandler. They
are registered alongside targeted meal Undo and suggestion Withdraw, outside
every flow: the keyboard stays in the scrollback after the conversation times
out, and a button that quietly stops working is worse than one that was never
offered. Nothing large rides in either callback — they name a meal id plus an
item position or a food id, and the name and the four nutrients are read back
from ``diet_log_items`` at tap time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..keyboards import (
    DROP_FOOD_PREFIX,
    KEEP_FOOD_PREFIX,
    log_another_keyboard,
    parse_drop_food,
    parse_keep_food,
)
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
DROP_FOOD_PATTERN = rf"^{DROP_FOOD_PREFIX}_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$"

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


async def meal_keep_state(
    db: Any, user_id: int, items: Sequence[Mapping[str, Any]]
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """What one meal's typed items are, right now, in the food list.

    Returns ``(kept, offerable)``: ``(food_id, name)`` for the typed items that
    already exist as a saved food, and ``(item_order, name)`` for those that do
    not. One pass answers both, so the keyboard can never show a 💾 offer and a
    🗑 drop for the same item — the food either exists or it does not.

    Names repeated within the meal appear once. Both lists share the
    :data:`MAX_KEEP_OFFERS` budget: they are drawn on the same keyboard, above
    the Log another / Done pair, and it is the total height that matters.
    """
    kept: list[tuple[int, str]] = []
    offerable: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(kept) + len(offerable) >= MAX_KEEP_OFFERS:
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
        existing = await db.get_food_by_key(user_id, name)
        if existing is not None:
            kept.append((int(existing["id"]), name))
        else:
            offerable.append((_item_order(index, item), name))
    return kept, offerable


async def keep_food_offers(
    db: Any, user_id: int, items: Sequence[Mapping[str, Any]]
) -> list[tuple[int, str]]:
    """Which items of one meal are not yet saved foods.

    ``(item_order, display_name)`` pairs, in meal order — the offerable half of
    :func:`meal_keep_state`.
    """
    _kept, offerable = await meal_keep_state(db, user_id, items)
    return offerable


@dataclass(frozen=True)
class KeptFood:
    """One typed item that is now a saved food."""

    food_id: int
    name: str
    #: Whether it also got the ``1 portion`` usual that makes it a ⚡ one-tap row.
    tappable: bool


async def _store_typed_item(
    db: Any, user_id: int, name: str, item: Mapping[str, Any]
) -> tuple[str, KeptFood | None, dict[str, Any]]:
    """Save one typed item as a food, priced exactly as it was just logged.

    Returns ``(status, kept, result)``. ``status`` is ``"saved"`` or whatever
    ``save_food`` reported (``"limit"``, ``"unit_mismatch"``, …), so both the
    automatic pass and the manual button branch on the same vocabulary rather
    than each deciding for itself what a refusal means.
    """
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
    status = str(result.get("status") or "")
    if status not in ("added", "updated"):
        return status, None, result

    food = result.get("food") or {}
    food_id = food.get("id")
    if food_id is None:  # pragma: no cover - save_food always returns the row
        return "saved", None, result
    tappable = await _make_tappable(db, user_id, int(food_id))
    return "saved", KeptFood(int(food_id), name, tappable), result


async def auto_keep_typed_items(
    db: Any, user_id: int, items: Sequence[Mapping[str, Any]]
) -> tuple[list[KeptFood], list[tuple[int, str]]]:
    """Keep every typed item of a just-saved meal, returning what happened.

    ``(kept, still_offerable)`` — the foods now saved, and the ``(item_order,
    name)`` pairs an automatic save could not take, which still deserve a 💾
    button. Nothing here may cost the user their keyboard: a failure on one item
    leaves it in the offerable list and the loop carries on to the next.
    """
    kept: list[KeptFood] = []
    unkept: list[tuple[int, str]] = []
    for item_order, name in await keep_food_offers(db, user_id, items):
        item = next(
            (
                row
                for index, row in enumerate(items)
                if _item_order(index, row) == item_order
            ),
            None,
        )
        if item is None:  # pragma: no cover - offers come from these same items
            continue
        try:
            status, food, _result = await _store_typed_item(db, user_id, name, item)
        except Exception:
            logger.warning(
                "Could not keep a typed entry automatically", exc_info=True
            )
            unkept.append((item_order, name))
            continue
        if food is None:
            logger.info("Typed entry not kept automatically: %s", status)
            unkept.append((item_order, name))
            continue
        kept.append(food)
    return kept, unkept


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


def saved_food_message(kept: KeptFood, item: Mapping[str, Any]) -> str:
    """Confirm one kept food, priced as it was logged.

    Says what the new food *costs* rather than only that it was saved: the
    number is what future one-tap logs will write, and a saved food nobody can
    price is one nobody will trust enough to tap.
    """
    amount = f"1 {KEEP_PORTION_NAME}" if kept.tappable else f"1 {KEEP_BASE_UNIT}"
    closing = (
        "It's in your meal picker now — one tap logs it."
        if kept.tappable
        else f"It's in your meal picker now; ask for <code>1 {KEEP_BASE_UNIT}</code>."
    )
    return (
        f"💾 <b>Saved {escape_html(kept.name)}</b> to your foods.\n"
        f"{escape_html(amount)} = {_nutrition_line(item)}\n{closing}"
    )


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
    """Redraw the keep/drop rows, keeping every other control on the message.

    Recomputed from the meal rather than edited in place, so each row is exactly
    what the food list now says: a saved item shows 🗑, an unsaved one shows 💾,
    and the row that was just used swaps to its inverse rather than vanishing.
    Best effort — an un-editable message leaves a button that reports the truth
    when tapped, so a failure here is not worth surfacing.
    """
    try:
        items = await db.get_diet_log_items(user_id, meal_id)
        kept, offerable = await meal_keep_state(db, user_id, items)
        await query.edit_message_reply_markup(
            reply_markup=log_another_keyboard(
                user_id,
                meal_id=meal_id if (kept or offerable) else None,
                keepable=offerable,
                kept=kept,
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
        _status, kept, result = await _store_typed_item(db, user_id, name, item)
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

    if kept is None:  # pragma: no cover - every other status is handled above
        await _retire_offer(query, "⚠️ Couldn't save that — try again.")
        return

    await _redraw_offers(query, db, user_id, meal_id)
    await _retire_offer(query, saved_food_message(kept, item))


def _names_in_meal(items: Sequence[Mapping[str, Any]]) -> set[str]:
    """The storable names of a meal's typed items, folded for comparison."""
    names: set[str] = set()
    for item in items:
        if str(item.get("source_type") or _FREETEXT) != _FREETEXT:
            continue
        name = _storable_name(item)
        if name is not None:
            names.add(name.casefold())
    return names


@authorized_callback
async def drop_kept_food_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """🗑 on an automatically kept food: put it back the way it was.

    Archiving is deliberately narrow. The button names the meal it was drawn
    under as well as the food, and the food is only archived if its name is
    still one of that meal's typed items — so an old keyboard rediscovered in
    the scrollback cannot archive a food that has since become something the
    user relies on. The meal itself is untouched: this undoes the *keeping*, not
    the logging, and /undo is still what removes a meal.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    target = parse_drop_food(query.data or "", user_id)
    if target is None:
        await query.answer(
            "That button belongs to another user or is no longer valid.",
            show_alert=True,
        )
        return
    meal_id, food_id = target

    db = context.bot_data["db"]
    food = await db.get_food_by_id(user_id, food_id)
    if food is None:
        await query.answer("That food is already gone.", show_alert=True)
        return

    items = await db.get_diet_log_items(user_id, meal_id)
    if str(food.get("name") or "").casefold() not in _names_in_meal(items):
        await query.answer(
            "That food is no longer the one this meal saved.", show_alert=True
        )
        return

    await query.answer()
    try:
        result = await db.archive_food(user_id, food_id)
    except Exception:
        logger.exception("Could not drop a kept food")
        await _retire_offer(query, "⚠️ Couldn't remove that — try again.")
        return
    if result.get("status") != "updated":
        await _retire_offer(query, "🗑 That food is already gone.")
        return

    # The usual amount goes with it. Left behind, it would silently reattach to
    # the food if the same name were kept again later and land a stored default
    # nobody chose this time.
    try:
        await db.clear_default_quantity(user_id, "food", food_id)
    except Exception:
        logger.debug("Kept food dropped with its usual amount left behind", exc_info=True)

    await _redraw_offers(query, db, user_id, meal_id)
    await _retire_offer(
        query,
        f"🗑 Dropped <b>{escape_html(str(food.get('name')))}</b> from your foods. "
        "The meal itself is still logged.",
    )


#: The keep-food button, for registration outside every conversation.
keep_food_handler = CallbackQueryHandler(keep_food_callback, pattern=KEEP_FOOD_PATTERN)
#: Its inverse, registered the same way and for the same reason.
drop_kept_food_handler = CallbackQueryHandler(
    drop_kept_food_callback, pattern=DROP_FOOD_PATTERN
)
