"""Durable meal receipts: the rendering plus the targeted ``Undo`` control.

A receipt is what a completed fast mutation leaves behind. Unlike the guided
flow's ephemeral keyboards — which are tied to the newest message and expire
with the conversation — a receipt stays valid after later meals are logged: its
buttons name one exact meal, so pressing ``Undo`` on an older receipt removes
that meal and never a newer one (plan §10.1/§10.3).

The rendering helpers live here rather than in ``diet.py`` so both the Home
surface and the Diet flow can reuse them without importing each other.
"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..callback_data import parse_base36
from ..config import phase1_enabled_for
from ..keyboards import meal_receipt_keyboard, reply_keyboard_remove
from ..meal_models import DietItemSourceType, MealReceipt, UndoStatus
from .common import authorized_callback, escape_html, reply_html

logger = logging.getLogger(__name__)

# Full-match, base-36 only. Permissive prefix matching would let a malformed or
# hand-crafted payload reach the owner check with an unparsed target.
RECEIPT_UNDO_PATTERN = r"^mr_undo_[0-9a-z]+_[0-9a-z]+$"
RECEIPT_MORE_PATTERN = r"^mr_more_[0-9a-z]+$"
RECEIPT_CURRENT_PATTERN = r"^mr_current_[0-9a-z]+_[0-9a-z]+$"
_RECEIPT_UNDO_RE = re.compile(r"^mr_undo_([0-9a-z]+)_([0-9a-z]+)$")

_RETIRED_HINT = "That quick action isn't available."
# How many item lines a receipt shows before collapsing the rest.
_MAX_RECEIPT_ITEMS = 6
_MAX_ITEM_NAME = 60
_MAX_SUMMARY = 200


def _amount(value: float | None) -> str:
    """Format a stored amount without a pointless decimal tail."""
    return f"{float(value):g}"


def _bounded(text: object, limit: int) -> str:
    """Escape a stored display value after bounding its message contribution."""
    value = str(text)
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return escape_html(value)


def _item_line(item) -> str:
    """One ``• name — amount`` line for a snapshotted meal item."""
    name = _bounded(item.display_name, _MAX_ITEM_NAME)
    if item.entered_amount is None:
        return f"• {name}"
    unit = f" {escape_html(item.entered_unit)}" if item.entered_unit else ""
    return f"• {name} — {_amount(item.entered_amount)}{unit}"


def _nutrient_line(receipt: MealReceipt) -> str:
    """The calories/macros line, omitting anything the snapshot left unknown."""
    nutrients = receipt.header.nutrients
    parts: list[str] = []
    if nutrients.calories is not None:
        parts.append(f"🔥 <b>{int(nutrients.calories)}</b> cal")
    macros = [
        f"{label} {_amount(value)} g"
        for label, value in (
            ("P", nutrients.protein_g),
            ("C", nutrients.carbs_g),
            ("F", nutrients.fat_g),
        )
        if value is not None
    ]
    if macros:
        parts.append("🥩 " + " · ".join(macros))
    return "\n" + " · ".join(parts) if parts else ""


def format_meal_receipt(receipt: MealReceipt, headline: str) -> str:
    """Render one completed meal: exact id, meal type, items, and totals."""
    header = receipt.header
    lines = [
        f"{headline} · meal #{header.meal_id}",
        f"🍽️ <b>{escape_html(header.meal_type.title())}</b>",
    ]
    if receipt.items:
        shown = receipt.items[:_MAX_RECEIPT_ITEMS]
        lines.extend(_item_line(item) for item in shown)
        remaining = len(receipt.items) - len(shown)
        if remaining > 0:
            lines.append(f"• …and {remaining} more")
    else:
        # Pre-structured or free-text meals keep only the header description.
        lines.append(f"🥘 {_bounded(header.food_items, _MAX_SUMMARY)}")
    return "\n".join(lines) + _nutrient_line(receipt)


def can_use_current_values(receipt: MealReceipt) -> bool:
    """Whether this meal has anything a re-resolution could actually update."""
    return any(
        item.source_type is not DietItemSourceType.FREETEXT
        for item in receipt.items
    )


async def send_meal_receipt(
    message, receipt: MealReceipt, headline: str
) -> None:
    """Send a receipt with its durable Undo / Log another controls."""
    await reply_html(
        message,
        format_meal_receipt(receipt, headline),
        reply_markup=meal_receipt_keyboard(
            receipt.header.user_id,
            receipt.header.meal_id,
            can_use_current=can_use_current_values(receipt),
        ),
    )


async def _retire_receipt(query, text: str) -> None:
    """Replace a receipt's buttons with a final status line."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire receipt markup", exc_info=True)
    try:
        await query.message.reply_text(text)
    except TelegramError:
        logger.debug("Could not report receipt outcome", exc_info=True)


@authorized_callback
async def undo_from_receipt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the exact meal this receipt was rendered for (plan §10.3).

    Allowed during an active guided flow on purpose: it targets a fixed
    completed meal and never touches conversation state. A repeated press is a
    no-op, and an intervening meal is unaffected.
    """
    query = update.callback_query
    match = _RECEIPT_UNDO_RE.fullmatch(query.data or "")
    if match is None:
        await query.answer("This action isn't available.", show_alert=True)
        return
    try:
        owner = parse_base36(match.group(1))
        meal_id = parse_base36(match.group(2))
    except ValueError:
        await query.answer("This action isn't available.", show_alert=True)
        return
    if owner != update.effective_user.id:
        await query.answer("This action belongs to another user.", show_alert=True)
        return

    await query.answer()

    # Re-check the flag immediately before the DB call: a receipt outlives a
    # restart, and a disabled feature must retire its controls, not mutate.
    if not phase1_enabled_for(owner):
        await _retire_receipt(query, _RETIRED_HINT)
        try:
            await query.message.reply_text(
                "Use /undo to remove a recent entry.",
                reply_markup=reply_keyboard_remove(),
            )
        except TelegramError:
            logger.debug("Could not synchronize keyboard", exc_info=True)
        return

    db = context.bot_data["db"]
    result = await db.delete_meal_if_recent(owner, meal_id)
    if result.status is UndoStatus.DELETED:
        await _retire_receipt(query, f"↩️ Undone — meal #{meal_id} removed.")
    elif result.status is UndoStatus.ALREADY_REMOVED:
        await _retire_receipt(query, f"↩️ Already removed — meal #{meal_id}.")
    else:
        # Expired: the meal stays. Retire the button so the receipt cannot
        # promise an action it will never perform.
        await _retire_receipt(
            query,
            f"⏳ Meal #{meal_id} is more than 24h old and was kept.",
        )
