"""
Routine configuration: motivational "anchor" nudges loaded from YAML.

An anchor fires once per day at a fixed local time. Each anchor may inspect
today's logs (``checks``) to produce log-aware status lines, and may append a
rotating motivational quote. The file is optional — when it is absent the bot
falls back to the single legacy reminder (see :mod:`bot.main`).

This module is deliberately free of Telegram/database imports so it can be
loaded and validated in isolation. Message composition and sending live in
:mod:`bot.handlers.reminders`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Log categories an anchor may inspect.
VALID_CHECKS = ("study", "gym", "diet", "habits")
# Weekday abbreviations, indexed by ``date.weekday()`` (Mon=0).
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Field size caps. These keep a composed anchor message provably well under
# Telegram's 4096 UTF-16 limit (even all-emoji quotes stay within budget),
# so a stray long value fails at load time rather than at delivery time.
MAX_QUOTE_LENGTH = 500
MAX_ANCHOR_ID_LENGTH = 32
MAX_ANCHOR_TITLE_LENGTH = 64
MAX_ANCHOR_EMOJI_LENGTH = 16


class RoutineConfigError(ValueError):
    """Raised when a routine file exists but is structurally invalid."""


@dataclass(frozen=True)
class Targets:
    """Optional daily targets that make nudges context-aware."""

    study_min: int = 0
    # Subset of WEEKDAYS. Empty means "every day is a gym day".
    gym_days: frozenset[str] = field(default_factory=frozenset)

    def is_gym_day(self, day: date) -> bool:
        """Whether ``day`` is a scheduled gym day (every day if unset)."""
        return not self.gym_days or WEEKDAYS[day.weekday()] in self.gym_days


@dataclass(frozen=True)
class Anchor:
    """A single scheduled nudge."""

    id: str
    at: time  # local wall-clock time (naive; localized at scheduling time)
    emoji: str
    title: str
    checks: tuple[str, ...]
    quote: bool


@dataclass(frozen=True)
class Routine:
    """A fully-parsed, validated routine configuration."""

    quotes: tuple[str, ...]
    anchors: tuple[Anchor, ...]
    targets: Targets


def load_routine(path: str | Path) -> Routine | None:
    """Load and validate a routine file.

    Returns ``None`` when the file does not exist (legacy-reminder fallback).
    Raises :class:`RoutineConfigError` when the file exists but is invalid.
    """
    file_path = Path(path)
    if not file_path.exists():
        logger.info("No routine file at %s — using legacy reminder", file_path)
        return None

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RoutineConfigError(f"routine file is not valid YAML: {exc}") from exc

    if raw is None:
        raise RoutineConfigError("routine file is empty")
    if not isinstance(raw, dict):
        raise RoutineConfigError("routine file must be a mapping at the top level")

    quotes = _parse_quotes(raw.get("quotes", []))
    targets = _parse_targets(raw.get("targets", {}))
    anchors = _parse_anchors(raw.get("anchors", []))
    if not anchors:
        raise RoutineConfigError("routine file must define at least one anchor")

    if not quotes and any(anchor.quote for anchor in anchors):
        logger.warning(
            "routine: %d anchor(s) set 'quote: true' but no quotes are defined — "
            "no motivational line will be shown until you add some",
            sum(anchor.quote for anchor in anchors),
        )

    return Routine(quotes=quotes, anchors=anchors, targets=targets)


def pick_quote(quotes: Sequence[str], anchor_id: str, today: date) -> str | None:
    """Deterministically choose a quote for an anchor on a given day.

    Rotates by day so the line changes daily, and offsets by anchor id so
    different anchors on the same day usually differ. Stable within a day.
    """
    if not quotes:
        return None
    offset = sum(ord(char) for char in anchor_id)
    index = (today.toordinal() + offset) % len(quotes)
    return quotes[index]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _parse_quotes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RoutineConfigError("'quotes' must be a list")
    quotes: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise RoutineConfigError(f"quote #{index} must be a non-empty string")
        cleaned = item.strip()
        if len(cleaned) > MAX_QUOTE_LENGTH:
            raise RoutineConfigError(
                f"quote #{index} is too long (max {MAX_QUOTE_LENGTH} characters)"
            )
        quotes.append(cleaned)
    return tuple(quotes)


def _parse_targets(value: object) -> Targets:
    if not isinstance(value, dict):
        raise RoutineConfigError("'targets' must be a mapping")

    study_min = value.get("study_min", 0)
    # bool is a subclass of int — reject it explicitly.
    if isinstance(study_min, bool) or not isinstance(study_min, int) or study_min < 0:
        raise RoutineConfigError("targets.study_min must be a non-negative integer")

    raw_days = value.get("gym_days", [])
    if not isinstance(raw_days, list):
        raise RoutineConfigError("targets.gym_days must be a list of weekday names")
    lookup = {day.lower(): day for day in WEEKDAYS}
    gym_days: set[str] = set()
    for entry in raw_days:
        key = entry.strip().lower() if isinstance(entry, str) else None
        if key not in lookup:
            raise RoutineConfigError(
                f"targets.gym_days entry {entry!r} must be one of {', '.join(WEEKDAYS)}"
            )
        gym_days.add(lookup[key])

    return Targets(study_min=study_min, gym_days=frozenset(gym_days))


def _parse_time(value: object) -> time:
    if not isinstance(value, str):
        raise RoutineConfigError(f"anchor time {value!r} must be a 'HH:MM' string")
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise RoutineConfigError(f"anchor time {value!r} must be 'HH:MM'")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RoutineConfigError(f"anchor time {value!r} is out of range")
    return time(hour=hour, minute=minute)


def _parse_anchors(value: object) -> tuple[Anchor, ...]:
    if not isinstance(value, list):
        raise RoutineConfigError("'anchors' must be a list")

    anchors: list[Anchor] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise RoutineConfigError(f"anchor #{index} must be a mapping")

        anchor_id = item.get("id")
        if not isinstance(anchor_id, str) or not anchor_id.strip():
            raise RoutineConfigError(f"anchor #{index} needs a non-empty 'id'")
        anchor_id = anchor_id.strip()
        if len(anchor_id) > MAX_ANCHOR_ID_LENGTH:
            raise RoutineConfigError(
                f"anchor id {anchor_id!r} is too long (max {MAX_ANCHOR_ID_LENGTH})"
            )
        if anchor_id in seen_ids:
            raise RoutineConfigError(f"duplicate anchor id {anchor_id!r}")
        seen_ids.add(anchor_id)

        at = _parse_time(item.get("time"))

        checks_raw = item.get("checks", [])
        if not isinstance(checks_raw, list):
            raise RoutineConfigError(f"anchor {anchor_id!r}: 'checks' must be a list")
        checks: list[str] = []
        for check in checks_raw:
            if check not in VALID_CHECKS:
                raise RoutineConfigError(
                    f"anchor {anchor_id!r}: unknown check {check!r} "
                    f"(valid: {', '.join(VALID_CHECKS)})"
                )
            if check in checks:
                raise RoutineConfigError(
                    f"anchor {anchor_id!r}: duplicate check {check!r}"
                )
            checks.append(check)

        emoji = item.get("emoji", "")
        title = item.get("title", anchor_id)
        if not isinstance(emoji, str) or not isinstance(title, str):
            raise RoutineConfigError(
                f"anchor {anchor_id!r}: 'emoji' and 'title' must be strings"
            )
        if len(emoji.strip()) > MAX_ANCHOR_EMOJI_LENGTH:
            raise RoutineConfigError(
                f"anchor {anchor_id!r}: 'emoji' too long (max {MAX_ANCHOR_EMOJI_LENGTH})"
            )
        if len(title.strip()) > MAX_ANCHOR_TITLE_LENGTH:
            raise RoutineConfigError(
                f"anchor {anchor_id!r}: 'title' too long (max {MAX_ANCHOR_TITLE_LENGTH})"
            )

        quote = item.get("quote", False)
        if not isinstance(quote, bool):
            raise RoutineConfigError(f"anchor {anchor_id!r}: 'quote' must be true or false")

        anchors.append(
            Anchor(
                id=anchor_id,
                at=at,
                emoji=emoji.strip(),
                title=title.strip() or anchor_id,
                checks=tuple(checks),
                quote=quote,
            )
        )

    return tuple(anchors)
