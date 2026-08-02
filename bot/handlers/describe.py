"""``/describe`` — log a whole meal by typing it, deterministically.

Release 3.2. The user types ``2 eggs, 100g oats``; the segments are parsed by
:mod:`bot.meal_text`, resolved against their own foods and recipes (then the
shared catalog) by :mod:`bot.services.typed_meal`, and shown for confirmation.
Only after a meal-type tap does the existing atomic ``log_diet_with_items`` path
write anything.

Three rules the screen has to make visible rather than merely obey:

* nothing is written until the confirm tap;
* items that could not be resolved are listed with the reason, and are **not**
  saved — a partially understood meal never turns into a silently smaller log;
  and
* nutrition always comes from a stored definition, so an unknown food is a
  prompt to add it, never an estimate.

Release 4 layers a model onto this by supplying more segments in the same shape.
It does not get its own save path.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import config
from ..callback_data import parse_base36, to_base36
from ..meal_text import MAX_SEGMENTS, parse_meal_text
from ..services.llm_parser import build_parser
from ..services.typed_meal import (
    TypedMealPlan,
    augment_plan_with_parser,
    plan_typed_meal,
)
from .common import (
    active_conversation_flow,
    authorized_callback,
    escape_html,
    mutation_source,
    reply_html,
)

logger = logging.getLogger(__name__)

#: Kept in step with ``diet.VALID_MEALS``; asserted by the tests rather than
#: imported, so this module does not depend on the large diet conversation.
MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")
_MEAL_EMOJI = {
    "breakfast": "🌅",
    "lunch": "🥗",
    "dinner": "🍽️",
    "snack": "🍎",
}

_PENDING_KEY = "describe_pending"
_MAX_INPUT_LENGTH = 500
_ACTIVE_FLOW_HINT = "Finish that first, or send /cancel."

_USAGE = (
    "📝 <b>Describe a meal</b>\n\n"
    "Type what you ate and I'll look each item up:\n"
    "<code>/describe 2 eggs, 100g oats, coffee</code>\n\n"
    "Amounts can lead or trail (<code>100g oats</code> or <code>oats 100g</code>). "
    "Items are separated by commas.\n"
    "Nutrition always comes from your saved foods or the shared catalog — "
    "I never estimate it."
)


def _confirm_keyboard(user_id: int, token: int) -> InlineKeyboardMarkup:
    """Meal-type buttons that double as the confirm action.

    One tap chooses the meal type *and* saves, because asking for the type and
    then asking again for confirmation adds a step without adding a decision.
    """
    owner = to_base36(user_id)
    rev = to_base36(token)
    rows = [
        [
            InlineKeyboardButton(
                f"{_MEAL_EMOJI[meal]} {meal.title()}",
                callback_data=f"desc_save_{owner}_{rev}_{meal}",
            )
            for meal in MEAL_TYPES[index : index + 2]
        ]
        for index in (0, 2)
    ]
    rows.append(
        [InlineKeyboardButton("✖️ Cancel", callback_data=f"desc_cancel_{owner}_{rev}")]
    )
    return InlineKeyboardMarkup(rows)


def render_plan(plan: TypedMealPlan) -> str:
    """Compose the confirmation text for a resolved plan."""
    lines = ["📝 <b>Ready to log</b>\n"]

    for entry in plan.resolved:
        calories = (
            f"{entry.calories} cal" if entry.calories is not None else "calories unknown"
        )
        lines.append(f"• {escape_html(entry.display_text)} — {escape_html(calories)}")

    if plan.resolved:
        total = plan.total_calories
        if total is None:
            lines.append("\n<b>Total:</b> unknown")
        else:
            suffix = " (partial — some items unknown)" if plan.has_unknown_calories else ""
            lines.append(f"\n<b>Total:</b> {total} cal{escape_html(suffix)}")

    if plan.unresolved:
        lines.append("\n⚠️ <b>Not logged</b>")
        for item in plan.unresolved:
            detail = f" — {escape_html(item.explanation)}"
            lines.append(f"• {escape_html(item.name)}{detail}")
            if item.candidates:
                options = ", ".join(item.candidates)
                lines.append(f"  <i>did you mean: {escape_html(options)}?</i>")
        lines.append(
            "\nThese are left out. Add one with <code>/food add</code>, "
            "or give an amount and describe again."
        )

    if plan.model_assisted:
        # Say it plainly. The user consented to text being sent, but consent is
        # not the same as knowing it happened on this particular message.
        lines.append(
            "\n<i>🤖 AI helped read this. Amounts and nutrition still come from "
            "your saved foods — check the items above.</i>"
        )

    if plan.resolved:
        lines.append("\nPick a meal type to save:")
    return "\n".join(lines)


async def describe_usage_reply(message: Message) -> None:
    """Explain the command. Used by the Home bar, which carries no meal text."""
    await reply_html(message, _USAGE)


async def describe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/describe <what you ate>``."""
    if active_conversation_flow(context) is not None:
        await update.effective_message.reply_text(_ACTIVE_FLOW_HINT)
        return

    text = " ".join(context.args or []).strip()
    await start_describe(update.effective_message, context, update.effective_user.id, text)


async def start_describe(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
) -> None:
    """Parse, resolve, and present a typed meal without writing anything."""
    if not text:
        await reply_html(message, _USAGE)
        return
    if len(text) > _MAX_INPUT_LENGTH:
        await reply_html(
            message,
            f"❌ That's too long (max {_MAX_INPUT_LENGTH} characters). "
            "Try one meal at a time.",
        )
        return

    segments = parse_meal_text(text)
    if not segments:
        await reply_html(message, _USAGE)
        return

    db = context.bot_data["db"]
    await db.ensure_user(user_id, None, None)
    plan = await plan_typed_meal(db, user_id, segments)
    plan = await _maybe_assist(db, user_id, plan)

    if not plan.has_items:
        context.user_data.pop(_PENDING_KEY, None)
        await reply_html(message, _nothing_resolved_text(plan))
        return

    # A monotonic token per presentation: an older preview's buttons stop working
    # as soon as a newer one is sent, so a stale tap cannot save a stale meal.
    token = int(context.user_data.get("describe_token", 0)) + 1
    context.user_data["describe_token"] = token
    context.user_data[_PENDING_KEY] = {
        "token": token,
        "items": [entry.as_item() for entry in plan.resolved],
    }

    await reply_html(
        message, render_plan(plan), reply_markup=_confirm_keyboard(user_id, token)
    )


async def _maybe_assist(db, user_id: int, plan: TypedMealPlan) -> TypedMealPlan:
    """Consult the external parser only when every gate is open.

    Three independent conditions, checked cheapest-first, and all required:
    something was left unresolved, the deployment has a key, and *this user* has
    opted in. The consent read happens per request rather than being cached, so
    revoking it takes effect on the very next message.
    """
    if not plan.unresolved or not config.GEMINI_AVAILABLE:
        return plan
    try:
        if not await db.get_ai_parsing_enabled(user_id):
            return plan
    except Exception:
        # Unreadable consent is treated as absent: never send on a failed check.
        logger.warning("Could not read parsing consent; staying local", exc_info=False)
        return plan

    parser = build_parser(config.GEMINI_API_KEY, config.GEMINI_MODEL)
    return await augment_plan_with_parser(db, user_id, plan, parser)


def _nothing_resolved_text(plan: TypedMealPlan) -> str:
    lines = ["📝 I couldn't match anything you typed.\n"]
    for item in plan.unresolved:
        lines.append(f"• {escape_html(item.name)} — {escape_html(item.explanation)}")
        if item.candidates:
            options = ", ".join(item.candidates)
            lines.append(f"  <i>did you mean: {escape_html(options)}?</i>")
    lines.append(
        "\nSave it first with <code>/food add</code>, or use 🍽️ Log meal to "
        "build the meal step by step."
    )
    if len(plan.unresolved) >= MAX_SEGMENTS:
        lines.append(f"\n<i>Only the first {MAX_SEGMENTS} items were read.</i>")
    return "\n".join(lines)


def _validate_tap(query, user_id: int, expected_parts: int):
    """Parse a ``desc_*`` callback and confirm owner and freshness."""
    parts = (query.data or "").split("_")
    if len(parts) != expected_parts:
        return None
    try:
        owner = parse_base36(parts[2])
        token = parse_base36(parts[3])
    except (ValueError, TypeError):
        return None
    if owner != user_id:
        return None
    return token


@authorized_callback
async def describe_save_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Save the pending typed meal under the tapped meal type."""
    query = update.callback_query
    user_id = update.effective_user.id
    token = _validate_tap(query, user_id, 5)
    meal_type = (query.data or "").rsplit("_", 1)[-1]

    pending = context.user_data.get(_PENDING_KEY)
    if token is None or meal_type not in MEAL_TYPES or not isinstance(pending, dict):
        await _reject(query, "This meal preview is no longer valid.")
        return
    if pending.get("token") != token:
        await _reject(query, "A newer preview replaced this one.")
        return

    items = pending.get("items")
    if not isinstance(items, list) or not items:
        await _reject(query, "This meal preview is no longer valid.")
        return

    db = context.bot_data["db"]
    try:
        await db.log_diet_with_items(
            user_id, meal_type, items, source=mutation_source(update)
        )
    except Exception:
        # Keep the preview and its buttons so the user can simply tap again.
        logger.warning("Typed meal save failed; offering retry", exc_info=True)
        await query.answer("Couldn't save that — try again.", show_alert=True)
        return

    context.user_data.pop(_PENDING_KEY, None)
    await query.answer()
    await _retire(query)
    await reply_html(
        query.message,
        f"✅ Logged {len(items)} item{'s' if len(items) != 1 else ''} "
        f"as <b>{escape_html(meal_type)}</b>. Use /undo if that was wrong.",
    )


@authorized_callback
async def describe_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Discard the pending typed meal without writing."""
    query = update.callback_query
    token = _validate_tap(query, update.effective_user.id, 4)
    if token is None:
        await _reject(query, "This meal preview is no longer valid.")
        return

    context.user_data.pop(_PENDING_KEY, None)
    await query.answer()
    await _retire(query)
    try:
        await query.message.reply_text("✖️ Discarded — nothing was logged.")
    except TelegramError:
        logger.debug("Could not confirm describe cancel", exc_info=True)


async def _reject(query, text: str) -> None:
    try:
        await query.answer(text, show_alert=True)
    except TelegramError:
        logger.debug("Could not answer stale describe callback", exc_info=True)
    await _retire(query)


async def _retire(query) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire describe keyboard", exc_info=True)
