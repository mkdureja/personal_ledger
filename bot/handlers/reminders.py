"""
Daily habit reminder via PTB JobQueue.

Sends an evening message listing unchecked habits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from .common import escape_html
from ..config import ALLOWED_USER_IDS, today_local
from ..routine import Anchor, Targets, pick_quote

logger = logging.getLogger(__name__)

# Bounded delivery retry so a brief Telegram outage at the scheduled minute
# doesn't silently drop the day's nudge.
_MAX_SEND_ATTEMPTS = 3
# Honor a Telegram-requested ``RetryAfter`` up to this long. Waiting a minute in a
# background job is fine; a longer rate-limit is deferred to the next run instead
# of wasting attempts on a delay Telegram already told us is too short.
_MAX_RETRY_AFTER_SECONDS = 60.0


async def _send_with_retry_classified(
    bot, chat_id: int, text: str
) -> tuple[bool, str | None]:
    """Send one HTML message with bounded backoff; return ``(ok, error_category)``.

    Honors Telegram's ``RetryAfter`` (up to :data:`_MAX_RETRY_AFTER_SECONDS`) and
    retries a couple of ``NetworkError``s. ``error_category`` is a sanitized label
    — never a token or raw exception/URL: ``None`` on success, ``"permanent"`` for
    a non-retryable error, ``"rate_limited"`` when Telegram asks to wait longer
    than we will block, and ``"retry_exhausted"`` when transient retries run out.
    """
    for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            return True, None
        except RetryAfter as exc:
            requested = float(getattr(exc, "retry_after", 1))
            if requested > _MAX_RETRY_AFTER_SECONDS:
                # Capping the wait below what Telegram demands only guarantees the
                # retry is rate-limited too. Stop and let the next scheduled run
                # resume, recording an honest, sanitized category.
                logger.warning(
                    "Telegram asked to retry chat %s after %.0fs (over the %.0fs "
                    "cap); deferring to the next run",
                    chat_id, requested, _MAX_RETRY_AFTER_SECONDS,
                )
                return False, "rate_limited"
            delay = requested + 0.5
        except (BadRequest, Forbidden):
            # Permanent (bad chat, blocked bot, malformed message) — do not retry.
            # Note: in PTB these subclass NetworkError, so catch them first.
            logger.exception("Permanent send failure to chat %s", chat_id)
            return False, "permanent"
        except NetworkError:
            delay = min(2.0**attempt, 5.0)
        except TelegramError:
            logger.exception("Permanent send failure to chat %s", chat_id)
            return False, "permanent"
        if attempt < _MAX_SEND_ATTEMPTS:
            await asyncio.sleep(delay)
    logger.warning(
        "Gave up sending to chat %s after %d attempts", chat_id, _MAX_SEND_ATTEMPTS
    )
    return False, "retry_exhausted"


async def _send_with_retry(bot, chat_id: int, text: str) -> bool:
    """Bounded-retry send returning only success (see the classified variant)."""
    ok, _category = await _send_with_retry_classified(bot, chat_id, text)
    return ok


async def _deliver_chunks(
    context: ContextTypes.DEFAULT_TYPE,
    db,
    user_id: int,
    job_key: str,
    local_date: str,
    messages: Sequence[str],
) -> int:
    """Deliver a user's reminder chunks with durable, resumable per-chunk state.

    Skips chunks already marked delivered (idempotent across a duplicate job run
    or a process restart), resumes at the first undelivered chunk, and stops this
    user's remaining chunks on a hard failure so the next run resumes them. The
    database lock is held only for the short state read/write — never across the
    network send or its backoff sleeps. Returns the number of chunks delivered
    this call. Failures are isolated to this user; the caller continues to others.
    """
    if not messages:
        return 0
    delivered_indices = await db.get_delivered_chunk_indices(
        user_id, job_key, local_date
    )
    delivered_now = 0
    for index, text in enumerate(messages):
        if index in delivered_indices:
            continue
        ok, category = await _send_with_retry_classified(context.bot, user_id, text)
        await db.record_chunk_delivery(
            user_id, job_key, local_date, index,
            delivered=ok, error_category=category,
        )
        if not ok:
            break  # resume this user's remaining chunks on the next run
        delivered_now += 1
    return delivered_now

# Keep a little headroom below Telegram's documented 4096-character ceiling.
# Counting UTF-16 code units is conservative for emoji-heavy names and matches
# the unit Telegram uses for message-entity offsets.
_REMINDER_MESSAGE_LIMIT = 4000
_CONTINUATION_HEADER = "⏰ <b>Evening Reminder</b> (continued)\n\n"
_REMINDER_FOOTER = "\n\nTap /habits to check them off!"


def _telegram_text_units(text: str) -> int:
    """Return Telegram's UTF-16 text length for conservative limit checks."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _split_habit_name(name: object, payload_limit: int) -> list[str]:
    """Escape and split one habit without cutting an HTML entity in half."""
    first_prefix = "⬜ "
    continuation_prefix = "↳ "
    fragments: list[str] = []
    current = first_prefix
    current_units = _telegram_text_units(current)

    for character in str(name):
        escaped_character = escape_html(character)
        character_units = _telegram_text_units(escaped_character)
        if current_units + character_units > payload_limit:
            fragments.append(current)
            current = continuation_prefix
            current_units = _telegram_text_units(current)
        current += escaped_character
        current_units += character_units

    fragments.append(current)
    return fragments


def _build_reminder_messages(
    habit_names: Sequence[object],
    *,
    first_header: str | None = None,
    continuation_header: str = _CONTINUATION_HEADER,
    footer: str = _REMINDER_FOOTER,
) -> list[str]:
    """Build complete, independently valid HTML messages below Telegram's limit.

    Headers/footer default to the legacy evening-reminder text. Callers (e.g.
    routine anchors) may override them so the chunked habit list is branded to
    match the message that precedes it.
    """
    if not habit_names:
        return []

    count = len(habit_names)
    if first_header is None:
        first_header = (
            "⏰ <b>Evening Reminder</b>\n\n"
            f"You still have {count} habit{'s' if count > 1 else ''} unchecked today:\n\n"
        )
    maximum_overhead = max(
        _telegram_text_units(first_header),
        _telegram_text_units(continuation_header),
    ) + _telegram_text_units(footer)
    payload_limit = _REMINDER_MESSAGE_LIMIT - maximum_overhead

    fragments = [
        fragment
        for name in habit_names
        for fragment in _split_habit_name(name, payload_limit)
    ]

    payloads: list[str] = []
    current_lines: list[str] = []
    current_units = 0
    for fragment in fragments:
        fragment_units = _telegram_text_units(fragment)
        separator_units = 1 if current_lines else 0
        if current_lines and current_units + separator_units + fragment_units > payload_limit:
            payloads.append("\n".join(current_lines))
            current_lines = []
            current_units = 0
            separator_units = 0
        current_lines.append(fragment)
        current_units += separator_units + fragment_units

    if current_lines:
        payloads.append("\n".join(current_lines))

    messages: list[str] = []
    for index, payload in enumerate(payloads):
        header = first_header if index == 0 else continuation_header
        chunk_footer = footer if index == len(payloads) - 1 else ""
        message = f"{header}{payload}{chunk_footer}"
        if _telegram_text_units(message) > _REMINDER_MESSAGE_LIMIT:
            raise AssertionError("Reminder chunk exceeded the safe Telegram limit")
        messages.append(message)

    return messages


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled daily job: remind each user of unchecked habits."""
    db = context.bot_data["db"]
    today = today_local()

    # Only users who have opted in receive reminders (Phase 4 opt-in model).
    enabled = await db.get_reminder_enabled_users(ALLOWED_USER_IDS)
    if not enabled:
        return

    unchecked_map = await db.get_users_with_unchecked_habits(enabled, today)
    local_date = today.isoformat()

    for user_id, habit_names in unchecked_map.items():
        if not habit_names:
            continue

        messages = _build_reminder_messages(habit_names)
        delivered = await _deliver_chunks(
            context, db, user_id, "daily_habit_reminder", local_date, messages
        )
        # Sanitized: counts only, never the raw Telegram ID (delivery diagnostics
        # must not identify a user — see docs/operations_runbook.md).
        logger.info(
            "Daily reminder: %d unchecked habit(s) across %d message(s), %d delivered",
            len(habit_names),
            len(messages),
            delivered,
        )

    # Also send a "well done" message to users who completed all habits
    for user_id in enabled:
        if user_id not in unchecked_map:
            # Check if they have any habits at all
            habits = await db.get_active_habits(user_id)
            if habits:
                await _deliver_chunks(
                    context,
                    db,
                    user_id,
                    "daily_done",
                    local_date,
                    ["🎉 <b>All habits done today!</b> Great job! 💪"],
                )


# ---------------------------------------------------------------------------
# Routine anchors — log-aware nudges scheduled from routine.yaml
# ---------------------------------------------------------------------------
async def _study_line(db, user_id: int, today, targets: Targets) -> str:
    """One-line study status, target-aware when a study target is set."""
    total = await db.get_today_study_total(user_id, today)
    target = targets.study_min
    if target > 0:
        if total >= target:
            return f"📖 Study: {total}/{target} min — on track 🔥"
        if total > 0:
            return f"📖 Study: {total}/{target} min — {target - total} to go"
        return f"📖 Study: 0/{target} min — a 25-min block?"
    if total > 0:
        return f"📖 Study: {total} min logged 🔥"
    return "📖 Study: nothing logged yet — a 25-min block?"


async def _gym_line(db, user_id: int, today, targets: Targets) -> str:
    """One-line gym status, quiet-but-kind on configured rest days."""
    count = await db.get_today_gym_count(user_id, today)
    if count > 0:
        return "🏋️ Gym: workout logged 💪"
    if not targets.is_gym_day(today):
        return "🏋️ Gym: rest day — recover well 😌"
    return "🏋️ Gym: no workout logged yet"


async def _diet_line(db, user_id: int, today) -> str:
    """One-line diet status with meal count and known calories."""
    meals = await db.get_today_meal_count(user_id, today)
    if meals == 0:
        return "🍽️ Diet: nothing logged — eat + log 🙂"
    kcal, incomplete = await db.get_today_calories(user_id, today)
    line = f"🍽️ Diet: {meals} {'meal' if meals == 1 else 'meals'}"
    if kcal > 0:
        line += f", ~{kcal:,} kcal"
    if incomplete:
        line += " (some missing calories)"
    return line


async def _habit_status(db, user_id: int, today) -> tuple[str, list[str]]:
    """Return (summary_line, unchecked_names).

    summary_line is empty when the user has no active habits, so the anchor
    simply omits the habit line rather than nagging about nothing.
    """
    habits = await db.get_active_habits(user_id)
    if not habits:
        return "", []
    checked = await db.get_checked_habits(user_id, today)
    unchecked = [h["habit_name"] for h in habits if h["id"] not in checked]
    if not unchecked:
        return f"✅ Habits: all {len(habits)} done! 🎉", []
    done = len(habits) - len(unchecked)
    return f"⬜ Habits: {done}/{len(habits)} done — {len(unchecked)} left", unchecked


async def build_anchor_message(
    anchor: Anchor, db, user_id: int, today, targets: Targets, quotes
) -> tuple[str, list[str]]:
    """Compose one anchor's message for a user.

    Returns ``(status_message, unchecked_habit_names)``. The caller sends the
    status message, then (if any) the detailed unchecked-habit list via the
    size-safe splitter shared with the legacy reminder.
    """
    header = f"{escape_html(anchor.emoji)} <b>{escape_html(anchor.title)}</b>".strip()
    body_lines: list[str] = []
    unchecked: list[str] = []

    for check in anchor.checks:
        if check == "study":
            body_lines.append(await _study_line(db, user_id, today, targets))
        elif check == "gym":
            body_lines.append(await _gym_line(db, user_id, today, targets))
        elif check == "diet":
            body_lines.append(await _diet_line(db, user_id, today))
        elif check == "habits":
            summary, unchecked = await _habit_status(db, user_id, today)
            if summary:
                body_lines.append(summary)

    segments = [header]
    if body_lines:
        segments.append("\n".join(body_lines))
    if anchor.quote:
        quote = pick_quote(quotes, anchor.id, today)
        if quote:
            segments.append(f"<i>{escape_html(quote)}</i>")

    return "\n\n".join(segments), unchecked


async def anchor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled routine anchor: send a log-aware nudge to each allowed user."""
    anchor: Anchor = context.job.data
    db = context.bot_data["db"]
    targets: Targets = context.bot_data.get("routine_targets", Targets())
    quotes = context.bot_data.get("routine_quotes", ())
    today = today_local()

    brand = f"{escape_html(anchor.emoji)} <b>{escape_html(anchor.title)}</b>".strip()
    first_header = f"{brand} — habits still to check:\n\n"
    continuation_header = f"{brand} (continued)\n\n"

    # Owner-scoped opt-in: only users who enabled reminders get anchors.
    enabled = await db.get_reminder_enabled_users(ALLOWED_USER_IDS)
    local_date = today.isoformat()
    job_key = f"anchor_{anchor.id}"
    for user_id in enabled:
        try:
            message, unchecked = await build_anchor_message(
                anchor, db, user_id, today, targets, quotes
            )
        except Exception:
            logger.exception("Failed to build '%s' anchor for user %d", anchor.id, user_id)
            continue  # one user's build failure never blocks the others

        # Chunk 0 is the status message; the (possibly long) unchecked-habit list,
        # branded to this anchor, follows as chunks 1..N. Durable delivery skips
        # chunks already sent on a duplicate run or after a restart, and stops
        # this user's remaining chunks on a hard failure (resumed next run).
        messages = [message]
        if unchecked:
            messages.extend(
                _build_reminder_messages(
                    unchecked,
                    first_header=first_header,
                    continuation_header=continuation_header,
                )
            )
        delivered = await _deliver_chunks(
            context, db, user_id, job_key, local_date, messages
        )
        # Report actual delivery (never a blanket "Sent" on failure) and keep the
        # diagnostic sanitized — counts only, no raw Telegram ID.
        logger.info(
            "Anchor '%s': delivered %d of %d chunk(s)",
            anchor.id, delivered, len(messages),
        )
