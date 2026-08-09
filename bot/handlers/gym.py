"""``/gym`` — a tappable workout log with per-set detail.

Rewritten after live use. The old flow asked four questions in a row, all by
typing, with no buttons anywhere: exercise name, sets, reps, weight. That made
the commonest action in the app the slowest, spelled the same exercise
differently every week, and could only record a workout where every set was
identical — which is not how anyone trains.

The shape now is:

    Workout → muscle group → exercise → one set at a time

Nothing is typed except the numbers, and even those collapse to a single tap
(**🔁 Same again**) when a set repeats. An exercise missing from the shared list
is added once, in the flow, and belongs to that user from then on.

Two things are deliberate:

* **A set is recorded individually.** ``12 × 40``, ``10 × 45``, ``8 × 50`` is
  three rows, not "3 sets" of something averaged. The header keeps the uniform
  numbers only when they *are* uniform, so a summary can still say "3×10 @ 50kg"
  without ever inventing one.
* **The exercise is written when you finish it**, not per set. A set is cheap to
  re-enter; a half-saved exercise that analytics counts is not. Abandoning
  mid-exercise loses only that exercise, exactly as before.

``/gym <exercise> <sets> <reps> [weight]`` still works unchanged for anyone who
prefers one line to four taps.
"""

from __future__ import annotations

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    ContextTypes,
    filters,
)

from .home import home_fallback_handlers
from .common import (
    ACTIVE_CONTROL_FILTER,
    AUTH_FILTER,
    active_conversation_hint,
    activate_conversation,
    active_flow_control_interceptor,
    authorized_callback,
    buttons_or_cancel_catchall,
    cancel_handler,
    conversation_available,
    escape_html,
    finish_conversation,
    mutation_source,
    parse_float,
    parse_int,
    reply_html,
    timeout_handler,
    voice_mid_flow_interceptor,
)
from ..config import CONVERSATION_TIMEOUT
from ..exercise_seed import MUSCLE_GROUPS, group_label

logger = logging.getLogger(__name__)

# Conversation states. EXERCISE is the tap surface (groups + recents); SET_INPUT
# takes "reps [weight]"; AFTER_SET and AFTER_EXERCISE are button-only.
EXERCISE, PICK, SET_INPUT, AFTER_SET, AFTER_EXERCISE, NEW_NAME = range(6)

MAX_GYM_SETS = 100
MAX_GYM_REPS = 1_000
MAX_WEIGHT_KG = 1_000.0
MAX_EXERCISE_NAME_LENGTH = 50
MAX_GYM_EXERCISES = 10
#: Matches the database's per-exercise cap; kept here so the flow refuses before
#: it builds a draft the write path would reject.
MAX_SETS_PER_EXERCISE = 30

_GYM_MORE_RE = re.compile(r"^gym_(\d+)_(yes|no)$")
_TAP_RE = re.compile(r"^gx_([a-z]+)_(\d+)(?:_(.+))?$")

_SET_HINT = (
    "Send <b>reps</b> and <b>weight</b> — e.g. <code>10 50</code>.\n"
    "Just reps (<code>15</code>) means bodyweight.\n"
    "Whole exercise at once: <code>3x10 40</code>, "
    "or one set per line — <code>12 40, 10 45, 8 50</code>."
)

#: One message may carry a whole exercise: sets split on newlines or commas.
_SET_SPLIT_RE = re.compile(r"[\n,;]+")
#: ``3x10`` is sets × reps, the universal gym shorthand — never reps × weight.
_SETS_BY_REPS_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(.+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _tap(action: str, user_id: int, payload: str | None = None) -> str:
    """Callback data carrying its owner, so a stale tap can be rejected."""
    return f"gx_{action}_{user_id}" + (f"_{payload}" if payload else "")


def _weight_text(weight: float | None) -> str:
    return f"{weight:g}kg" if weight is not None else "bodyweight"


def _set_line(index: int, reps: int, weight: float | None) -> str:
    return f"  {index}. {reps} × {_weight_text(weight)}"


def _draft_summary(exercise: str, sets: list[dict]) -> str:
    lines = [f"🏋️ <b>{escape_html(exercise)}</b>"]
    lines += [_set_line(i, s["reps"], s["weight_kg"]) for i, s in enumerate(sets, 1)]
    volume = sum(
        s["reps"] * float(s["weight_kg"]) for s in sets if s["weight_kg"] is not None
    )
    if volume:
        lines.append(f"\n<i>Volume so far: {volume:g} kg</i>")
    return "\n".join(lines)


def _groups_keyboard(user_id: int, recents: list[str]) -> InlineKeyboardMarkup:
    """Recent exercises first, then the muscle groups two per row.

    Repeat work is the norm in a gym, so last session's exercise is usually the
    fastest route to this one's.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for name in recents[:4]:
        rows.append(
            [InlineKeyboardButton(f"🔁 {name}", callback_data=_tap("r", user_id, name))]
        )
    keys = list(MUSCLE_GROUPS)
    rows += [
        [
            InlineKeyboardButton(group_label(key), callback_data=_tap("g", user_id, key))
            for key in keys[index : index + 2]
        ]
        for index in range(0, len(keys), 2)
    ]
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=_tap("x", user_id))])
    return InlineKeyboardMarkup(rows)


def _exercises_keyboard(user_id: int, group_key: str, rows) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                str(row["name"]), callback_data=_tap("e", user_id, str(row["id"]))
            )
        ]
        for row in rows
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                "➕ Add your own", callback_data=_tap("add", user_id, group_key)
            )
        ]
    )
    # Offered here rather than on a screen of its own: a separate "detail or
    # not?" step would cost the per-set user a tap on every single workout to
    # serve the person who never wants one. On this screen both are one tap.
    buttons.append(
        [
            InlineKeyboardButton(
                f"✅ Just log {group_label(group_key).casefold()}",
                callback_data=_tap("only", user_id, group_key),
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton("⬅️ Back", callback_data=_tap("back", user_id))]
    )
    return InlineKeyboardMarkup(buttons)


def _after_set_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔁 Same again", callback_data=_tap("same", user_id)
                ),
                InlineKeyboardButton(
                    "✏️ Different", callback_data=_tap("diff", user_id)
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Done with this exercise", callback_data=_tap("done", user_id)
                )
            ],
        ]
    )


def _after_exercise_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Another exercise", callback_data=_tap("more", user_id)
                )
            ],
            [
                InlineKeyboardButton(
                    "🏁 Finish workout", callback_data=_tap("fin", user_id)
                )
            ],
        ]
    )


def _exercise_summary(
    exercise: str, sets: int, reps: int, weight: float | None
) -> str:
    """One safe HTML line for a uniform exercise (the shortcut path)."""
    weight_text = f" @ {weight:g}kg" if weight is not None else " (bodyweight)"
    return f"🏋️ {escape_html(exercise)} — {sets}×{reps}{weight_text}"


def _logged_summary(exercise: str, sets: list[dict]) -> str:
    """One line naming what was saved, uniform or not."""
    reps = {s["reps"] for s in sets}
    weights = {s["weight_kg"] for s in sets}
    if len(reps) == 1 and len(weights) == 1:
        return _exercise_summary(
            exercise, len(sets), next(iter(reps)), next(iter(weights))
        )
    detail = ", ".join(f"{s['reps']}×{_weight_text(s['weight_kg'])}" for s in sets)
    return f"🏋️ {escape_html(exercise)} — {escape_html(detail)}"


def _workout_confirmation(exercises: list[str]) -> str:
    count = len(exercises)
    summary = "\n".join(exercises)
    return (
        f"✅ <b>Workout logged!</b> "
        f"({count} exercise{'s' if count != 1 else ''})\n\n{summary}"
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
async def _finish_workout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message,
    exercises: list[str],
) -> int:
    """End a persisted workout even if Telegram cannot deliver its summary."""
    text = (
        _workout_confirmation(exercises)
        if exercises
        else "✖️ Nothing logged this time."
    )
    try:
        await reply_html(message, text)
    except TelegramError:
        logger.warning("Could not deliver workout confirmation", exc_info=True)
    finish_conversation(update, context, "gym")
    return ConversationHandler.END


async def _remove_callback_markup(query: object) -> None:
    """Best-effort removal of an inline keyboard after it is consumed."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not remove stale gym keyboard", exc_info=True)


def _validate_tap(query, user_id: int) -> tuple[str, str | None] | None:
    """Parse a ``gx_*`` callback and confirm it belongs to the tapping user."""
    match = _TAP_RE.fullmatch(query.data or "")
    if match is None or int(match.group(2)) != user_id:
        return None
    return match.group(1), match.group(3)


async def _show_groups(message, context, user_id: int, *, prefix: str = "") -> int:
    """Render the muscle-group picker, with this user's recent exercises on top."""
    db = context.bot_data["db"]
    try:
        recents = [str(row["exercise"]) for row in await db.get_recent_exercises(user_id)]
    except Exception:  # A shortcut row is a convenience, never a blocker.
        logger.warning("Could not read recent exercises", exc_info=False)
        recents = []
    body = f"{prefix}🏋️ <b>Log Workout</b>\n\nPick a muscle group, or repeat a recent one:"
    await reply_html(message, body, reply_markup=_groups_keyboard(user_id, recents))
    return EXERCISE


@authorized_callback
async def stale_gym_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Acknowledge a workout button that has no active gym conversation."""
    query = update.callback_query
    match = _GYM_MORE_RE.fullmatch(query.data or "")
    if match is not None:
        if int(match.group(1)) != update.effective_user.id:
            await query.answer(
                "This workout prompt belongs to another user.", show_alert=True
            )
            return
        await query.answer("This workout prompt has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return

    tap = _TAP_RE.fullmatch(query.data or "")
    if tap is None:
        await query.answer("This workout prompt is no longer valid.", show_alert=True)
        return
    if int(tap.group(2)) != update.effective_user.id:
        await query.answer(
            "This workout prompt belongs to another user.", show_alert=True
        )
        return
    await query.answer("This workout has already finished — tap 🏋️ Workout to start a new one.", show_alert=True)
    await _remove_callback_markup(query)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def gym_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /gym, with the one-line shortcut still available."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    if not await conversation_available(update, context, "gym"):
        return ConversationHandler.END

    args = context.args or []

    # Shortcut: /gym <exercise> <sets> <reps> [weight] — every set identical.
    if len(args) >= 3:
        exercise = args[0]
        if len(exercise) > MAX_EXERCISE_NAME_LENGTH:
            await update.message.reply_text(
                f"❌ Exercise name too long (max {MAX_EXERCISE_NAME_LENGTH} characters)."
            )
            return ConversationHandler.END
        sets, err = parse_int(args[1], "Sets", max_value=MAX_GYM_SETS)
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END
        reps, err = parse_int(args[2], "Reps", max_value=MAX_GYM_REPS)
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END

        weight = None
        if len(args) >= 4:
            weight, err = parse_float(args[3], "Weight", max_value=MAX_WEIGHT_KG)
            if err:
                await update.message.reply_text(err)
                return ConversationHandler.END

        if sets > MAX_SETS_PER_EXERCISE:
            await update.message.reply_text(
                f"❌ That's more than {MAX_SETS_PER_EXERCISE} sets in one exercise."
            )
            return ConversationHandler.END
        return await _log_shortcut(update, context, exercise, sets, reps, weight)

    return await _begin_gym_flow(update, context)


async def _log_shortcut(
    update: Update, context, exercise: str, sets: int, reps: int, weight: float | None
) -> int:
    """Write the one-line form, where every set is identical by definition."""
    db = context.bot_data["db"]
    await db.log_gym_sets(
        update.effective_user.id,
        exercise,
        [{"reps": reps, "weight_kg": weight}] * sets,
        source=mutation_source(update),
    )
    try:
        await reply_html(
            update.message,
            "✅ <b>Exercise logged!</b>\n"
            f"{_exercise_summary(exercise, sets, reps, weight)}",
        )
    except TelegramError:
        logger.warning("Could not deliver gym confirmation", exc_info=True)
    return ConversationHandler.END


async def _begin_gym_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the guided gym flow from either /gym or the Workout tap."""
    context.user_data["gym_exercises"] = []
    context.user_data.pop("gym_sets", None)
    context.user_data.pop("gym_current_exercise", None)
    activate_conversation(update, context, "gym")
    try:
        return await _show_groups(
            update.effective_message, context, update.effective_user.id
        )
    except BaseException:
        finish_conversation(update, context, "gym")
        raise


@authorized_callback
async def gym_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Enter the guided gym flow from a Home 'Workout' tap."""
    query = update.callback_query
    await query.answer()
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "gym"):
        return ConversationHandler.END
    return await _begin_gym_flow(update, context)


# ---------------------------------------------------------------------------
# Picking an exercise
# ---------------------------------------------------------------------------
@authorized_callback
async def group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A muscle group was tapped: list its exercises."""
    query = update.callback_query
    parsed = _validate_tap(query, update.effective_user.id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return EXERCISE
    action, payload = parsed
    await query.answer()

    if action == "x":
        await _remove_callback_markup(query)
        return await _finish_workout(
            update, context, query.message, context.user_data.pop("gym_exercises", [])
        )
    if action == "back":
        await _remove_callback_markup(query)
        return await _show_groups(query.message, context, update.effective_user.id)
    if action == "r":  # repeat a recent exercise by name
        await _remove_callback_markup(query)
        return await _start_exercise(query.message, context, str(payload))

    db = context.bot_data["db"]
    rows = await db.list_exercises(update.effective_user.id, str(payload))
    await _remove_callback_markup(query)
    context.user_data["gym_group"] = str(payload)
    if not rows:
        await reply_html(
            query.message,
            f"{group_label(str(payload))} — nothing here yet.\nAdd your first one:",
            reply_markup=_exercises_keyboard(
                update.effective_user.id, str(payload), []
            ),
        )
        return PICK
    await reply_html(
        query.message,
        f"{group_label(str(payload))} — pick an exercise:",
        reply_markup=_exercises_keyboard(
            update.effective_user.id, str(payload), rows
        ),
    )
    return PICK


@authorized_callback
async def exercise_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """An exercise was tapped (or "add your own"/"back")."""
    query = update.callback_query
    parsed = _validate_tap(query, update.effective_user.id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return PICK
    action, payload = parsed
    await query.answer()

    if action == "back":
        await _remove_callback_markup(query)
        return await _show_groups(query.message, context, update.effective_user.id)

    if action == "add":
        await _remove_callback_markup(query)
        context.user_data["gym_group"] = str(payload)
        await reply_html(
            query.message,
            f"➕ What's it called? It'll be saved under {group_label(str(payload))} "
            "for next time.",
        )
        return NEW_NAME

    if action == "only":
        await _remove_callback_markup(query)
        return await _log_group_only(update, context, query.message, str(payload))

    db = context.bot_data["db"]
    row = await db.get_exercise(update.effective_user.id, int(payload))
    await _remove_callback_markup(query)
    if row is None:
        await reply_html(query.message, "That exercise is no longer available.")
        return await _show_groups(query.message, context, update.effective_user.id)
    return await _start_exercise(query.message, context, str(row["name"]))


async def receive_new_exercise_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Save a user's own exercise, then go straight into logging it."""
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("❌ Name can't be empty. What's it called?")
        return NEW_NAME
    if len(name) > MAX_EXERCISE_NAME_LENGTH:
        await update.message.reply_text(
            f"❌ Exercise name too long (max {MAX_EXERCISE_NAME_LENGTH} characters)."
        )
        return NEW_NAME

    db = context.bot_data["db"]
    group_key = context.user_data.get("gym_group", "chest")
    try:
        result = await db.add_user_exercise(
            update.effective_user.id, group_key, name
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return NEW_NAME

    saved = str(result["exercise"]["name"])
    note = "Saved — it'll be in your list from now on.\n\n" if result[
        "status"
    ] == "added" else "You already had that one.\n\n"
    return await _start_exercise(update.message, context, saved, prefix=note)


async def _start_exercise(message, context, exercise: str, *, prefix: str = "") -> int:
    """Open the set-by-set logger for one exercise."""
    context.user_data["gym_current_exercise"] = exercise
    context.user_data["gym_sets"] = []
    await reply_html(
        message,
        f"{prefix}🏋️ <b>{escape_html(exercise)}</b>\n\nSet 1 — {_SET_HINT}",
    )
    return SET_INPUT


# ---------------------------------------------------------------------------
# Logging sets
# ---------------------------------------------------------------------------
def _parse_set_item(text: str) -> tuple[list[dict] | None, str | None]:
    """Parse one item: ``reps``, ``reps weight``, ``NxR``, or ``NxR weight``.

    ``x`` used to be treated as a plain separator, so ``3x10`` became *3 reps at
    10 kg* — wrong reps and an invented weight, silently, for the single most
    natural thing a lifter types. In gym notation ``NxR`` is always sets × reps,
    so that is what it means here, and the weight (if any) follows separately.
    ``@`` is accepted as noise so ``3x10 @ 40`` reads the way people write it.
    """
    cleaned = (text or "").replace("@", " ").strip()
    if not cleaned:
        return None, "❌ Send reps, or reps and weight — e.g. <code>10 50</code>."

    count = 1
    match = _SETS_BY_REPS_RE.match(cleaned)
    if match is not None:
        count, err = parse_int(match.group(1), "Sets", max_value=MAX_SETS_PER_EXERCISE)
        if err:
            return None, err
        if count < 1:
            return None, "❌ That needs at least one set."
        cleaned = match.group(2)

    parts = cleaned.split()
    if not parts or len(parts) > 2:
        return None, "❌ Send reps, or reps and weight — e.g. <code>10 50</code>."
    reps, err = parse_int(parts[0], "Reps", max_value=MAX_GYM_REPS)
    if err:
        return None, err
    weight = None
    if len(parts) == 2:
        weight, err = parse_float(parts[1], "Weight", max_value=MAX_WEIGHT_KG)
        if err:
            return None, err
    # A fresh dict per set: they are stored as individual rows and must not
    # alias, or editing one would edit them all.
    return [{"reps": reps, "weight_kg": weight} for _ in range(count)], None


def _parse_sets(text: str) -> tuple[list[dict] | None, str | None]:
    """Parse one message into one or more sets.

    A whole exercise can be entered at once — one set per line, or separated by
    commas — instead of a message per set. Each item still becomes its own row,
    so nothing is averaged and the storage is identical either way.
    """
    items = [part for part in _SET_SPLIT_RE.split(text or "") if part.strip()]
    if not items:
        return None, "❌ Send reps, or reps and weight — e.g. <code>10 50</code>."
    entries: list[dict] = []
    for item in items:
        parsed, error = _parse_set_item(item)
        if error is not None:
            return None, error
        entries.extend(parsed)
        if len(entries) > MAX_SETS_PER_EXERCISE:
            return None, (
                f"❌ That's more than {MAX_SETS_PER_EXERCISE} sets for one "
                "exercise. Split it into two entries."
            )
    return entries, None


async def receive_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Take one set, or a whole exercise's worth of them."""
    entries, error = _parse_sets(update.message.text)
    if error is not None:
        await reply_html(update.message, f"{error}\n{_SET_HINT}")
        return SET_INPUT
    return await _record_set(update, context, update.message, entries)


async def _record_set(update: Update, context, message, entries: list[dict]) -> int:
    """Append one or more sets to the draft and offer the next action."""
    user_id = update.effective_user.id
    sets: list[dict] = context.user_data.setdefault("gym_sets", [])
    sets.extend(entries)
    if len(sets) >= MAX_SETS_PER_EXERCISE:
        await reply_html(
            message,
            f"That's {MAX_SETS_PER_EXERCISE} sets — saving this exercise now.",
        )
        return await _save_current_exercise(update, context, message)

    exercise = str(context.user_data.get("gym_current_exercise", "Exercise"))
    await reply_html(
        message,
        f"{_draft_summary(exercise, sets)}\n\nNext set?",
        reply_markup=_after_set_keyboard(user_id),
    )
    return AFTER_SET


@authorized_callback
async def after_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """🔁 Same again / ✏️ Different / ✅ Done."""
    query = update.callback_query
    parsed = _validate_tap(query, update.effective_user.id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return AFTER_SET
    action, _payload = parsed
    sets: list[dict] = context.user_data.get("gym_sets") or []
    if not sets:
        await query.answer("This exercise is no longer open.", show_alert=True)
        await _remove_callback_markup(query)
        return await _show_groups(query.message, context, update.effective_user.id)

    await query.answer()
    await _remove_callback_markup(query)

    if action == "same":
        return await _record_set(update, context, query.message, [dict(sets[-1])])
    if action == "diff":
        await reply_html(query.message, f"Set {len(sets) + 1} — {_SET_HINT}")
        return SET_INPUT
    return await _save_current_exercise(update, context, query.message)


async def _log_group_only(update: Update, context, message, group_key: str) -> int:
    """Record that a muscle group was trained, with no per-set detail at all.

    Not everyone wants to log a workout set by set, and the alternative to a
    two-tap entry is not a more detailed entry — it is no entry. The row stores
    NULL reps and NULL weight, which is the same shape the schema already uses
    for an exercise whose sets varied, so nothing downstream has to learn a new
    case. Deliberately no ``gym_sets`` children: inventing a 1×1 set to fill the
    columns would put a number in the ledger that nobody performed.
    """
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    label = group_label(group_key)
    await db.log_gym(user_id, label, 1, None, source=mutation_source(update))

    logged: list[str] = context.user_data.setdefault("gym_exercises", [])
    logged.append(f"🏋️ {escape_html(label)} — trained, no set detail")
    context.user_data.pop("gym_sets", None)
    context.user_data.pop("gym_current_exercise", None)
    return await _finish_workout(
        update, context, message, context.user_data.pop("gym_exercises")
    )


async def _save_current_exercise(update: Update, context, message) -> int:
    """Write the finished exercise, then ask what's next.

    The exercise is written here rather than per set: a set is cheap to re-enter,
    but a half-saved exercise that analytics already counts is not.
    """
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    exercise = str(context.user_data.get("gym_current_exercise", "Exercise"))
    sets: list[dict] = context.user_data.get("gym_sets") or []
    if not sets:
        await reply_html(message, "Nothing to save for that one.")
        return await _show_groups(message, context, user_id)

    await db.log_gym_sets(
        user_id, exercise, sets, source=mutation_source(update)
    )

    logged: list[str] = context.user_data.setdefault("gym_exercises", [])
    logged.append(_logged_summary(exercise, sets))
    context.user_data.pop("gym_sets", None)
    context.user_data.pop("gym_current_exercise", None)

    if len(logged) >= MAX_GYM_EXERCISES:
        return await _finish_workout(
            update, context, message, context.user_data.pop("gym_exercises")
        )

    await reply_html(
        message,
        f"✅ Saved.\n{logged[-1]}\n\n"
        f"<i>{len(logged)} exercise{'s' if len(logged) != 1 else ''} this workout.</i>",
        reply_markup=_after_exercise_keyboard(user_id),
    )
    return AFTER_EXERCISE


@authorized_callback
async def after_exercise_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """➕ Another exercise / 🏁 Finish workout."""
    query = update.callback_query
    parsed = _validate_tap(query, update.effective_user.id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return AFTER_EXERCISE
    action, _payload = parsed
    await query.answer()
    await _remove_callback_markup(query)

    if action == "more":
        return await _show_groups(query.message, context, update.effective_user.id)
    return await _finish_workout(
        update, context, query.message, context.user_data.pop("gym_exercises", [])
    )


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
# Reject voice mid-flow (no download) and nudge on any Home control word before
# it can be captured as an exercise name or a set (plan §8.5/§8.6). Button-only
# states also get a text catchall so stray text never reaches the Home router.
_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_control_guard = MessageHandler(ACTIVE_CONTROL_FILTER, active_flow_control_interceptor)
_text_catchall = MessageHandler(
    filters.TEXT & ~filters.COMMAND, buttons_or_cancel_catchall
)
_TAP_PATTERN = r"^gx_[a-z]+_\d+(?:_.+)?$"

gym_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("gym", gym_command, filters=AUTH_FILTER),
        CallbackQueryHandler(gym_menu_entry, pattern=r"^menu_gym$"),
    ],
    states={
        EXERCISE: [
            _voice_guard,
            CallbackQueryHandler(group_callback, pattern=_TAP_PATTERN),
            _control_guard,
            _text_catchall,
        ],
        PICK: [
            _voice_guard,
            CallbackQueryHandler(exercise_callback, pattern=_TAP_PATTERN),
            _control_guard,
            _text_catchall,
        ],
        NEW_NAME: [
            _voice_guard,
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_exercise_name),
        ],
        SET_INPUT: [
            _voice_guard,
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_set),
        ],
        AFTER_SET: [
            _voice_guard,
            CallbackQueryHandler(after_set_callback, pattern=_TAP_PATTERN),
            _control_guard,
            _text_catchall,
        ],
        AFTER_EXERCISE: [
            _voice_guard,
            CallbackQueryHandler(after_exercise_callback, pattern=_TAP_PATTERN),
            _control_guard,
            _text_catchall,
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("gym", active_conversation_hint, filters=AUTH_FILTER),
        # Home is always reachable: it ends this flow and reports anything
        # unsaved. Must be a fallback — a handler outside the conversation
        # cannot return END into it, so the state would linger and swallow
        # the next ordinary message.
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    # per_message=False is correct: every callback re-validates its owner id from
    # the callback data rather than relying on PTB's per-message tracking.
    per_message=False,
)
