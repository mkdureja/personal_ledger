"""Targets for monitored behaviours — the arithmetic of "how am I doing".

A *monitor* is the inverse of a habit. A habit asks "did you do the good thing
today?" and a tick is a win. A monitor asks "how often did this happen?" and the
honest answer is a count, which may be zero, may be five, and may carry a
quantity and a variant with it. Smoking wants to stay at zero; a drink is fine
once a month; cannabis has a comfortable band rather than a ceiling. None of
those are expressible as a daily checkbox, which is why they are not habits.

Three decisions are worth stating plainly:

**The period belongs to the target, not to the screen.** "Zero" is a statement
about a day, "once a month" about a month, "two to four" about a week. Storing
one period per target means the status line compares a count to the window the
user actually meant, rather than to whatever window the report happens to render.

**Below a floor is not a failure.** A range like 2–4 per week reads as *under*
for most of the week simply because the week is not over. The status vocabulary
keeps ``under`` distinct from ``over`` so a screen can show "not yet" without
the language of a breach — being at 1 on Tuesday is not the same event as being
at 5 on Sunday.

**A count with no target is still worth showing.** A monitor may exist purely to
be observed. An untargeted monitor reports its count and claims nothing about it.

This module is deliberately pure: standard library only, no I/O, no Telegram, no
database, no configuration — the same rule ``bot.weight_series`` follows, for the
same reason. The definition of "over your limit" must not be able to drift into a
handler and disagree with itself between two screens.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

#: The windows a target can be stated over. A calendar week starts on Monday and
#: a month is a calendar month, so "this week" means the same thing here as it
#: does on a wall calendar — a rolling 7-day window would make the same count
#: read differently depending on the hour it was asked about.
TargetPeriod = Literal["day", "week", "month"]
PERIODS: tuple[TargetPeriod, ...] = ("day", "week", "month")

#: How a count relates to its target.
#:
#: ``clear``       — inside the target (including a zero target that is holding).
#: ``over``        — past the ceiling. The only status that reports a breach.
#: ``under``       — below the floor of a range. "Not yet", not "failed".
#: ``untargeted``  — the monitor states no target; the count stands alone.
Status = Literal["clear", "over", "under", "untargeted"]

#: An upper bound on any stated target, and on a parsed count. High enough for
#: anything a person tracks by hand (a heavy smoker's day fits inside it), low
#: enough that a typo like ``100000/day`` is refused rather than stored.
MAX_TARGET_COUNT = 999

_PERIOD_PHRASE: dict[str, str] = {
    "day": "today",
    "week": "this week",
    "month": "this month",
}

_PERIOD_NOUN: dict[str, str] = {
    "day": "per day",
    "week": "per week",
    "month": "per month",
}

_STATUS_ICON: dict[str, str] = {
    "clear": "✅",
    "over": "⚠️",
    # Deliberately not a warning glyph: a range is under its floor for most of
    # the period by construction, and flagging that as a problem would train the
    # user to ignore the icon that does mean something.
    "under": "⏳",
    "untargeted": "•",
}


class TargetParseError(ValueError):
    """A user-facing reason a typed target could not be understood."""


@dataclass(frozen=True)
class Target:
    """A stated intention about how often something should happen.

    ``minimum`` and ``maximum`` are inclusive and either may be ``None``:
    ``maximum=0`` is "zero", ``maximum=5`` is "at most five", and a pair is a
    band. Both ``None`` means the monitor is observed without a target.
    """

    period: TargetPeriod = "day"
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        if self.period not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}, got {self.period!r}")
        for label, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label} must be a whole number or None")
            if value < 0 or value > MAX_TARGET_COUNT:
                raise ValueError(f"{label} must be between 0 and {MAX_TARGET_COUNT}")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum must not exceed maximum")

    @property
    def is_open(self) -> bool:
        """Whether the monitor states no target at all."""
        return self.minimum is None and self.maximum is None

    @property
    def is_zero(self) -> bool:
        """Whether the target is "none of this, ever"."""
        return self.maximum == 0

    def describe(self) -> str:
        """The target as a short phrase: ``zero``, ``≤5 per day``, ``2–4 per week``."""
        if self.is_open:
            return "no target"
        noun = _PERIOD_NOUN[self.period]
        if self.is_zero:
            # "zero per day" is noise: zero is zero on every window, and the
            # period only matters once there is a nonzero allowance to spend.
            return "zero"
        if self.minimum is not None and self.maximum is not None:
            if self.minimum == self.maximum:
                return f"{self.minimum} {noun}"
            return f"{self.minimum}–{self.maximum} {noun}"
        if self.maximum is not None:
            return f"≤{self.maximum} {noun}"
        return f"≥{self.minimum} {noun}"

    def bounds_label(self) -> str:
        """Just the numbers: ``target zero``, ``≤5``, ``2–4``, ``≥2``.

        Used where the surrounding sentence already names the window, so a
        status line reads "3 this week (2–4)" rather than repeating "per week".
        """
        if self.is_open:
            return ""
        if self.is_zero:
            return "target zero"
        if self.minimum is not None and self.maximum is not None:
            if self.minimum == self.maximum:
                return str(self.minimum)
            return f"{self.minimum}–{self.maximum}"
        if self.maximum is not None:
            return f"≤{self.maximum}"
        return f"≥{self.minimum}"


def period_bounds(period: str, day: date) -> tuple[date, date]:
    """The inclusive ``(start, end)`` of the ``period`` containing ``day``.

    The window ends on ``day`` itself, never at the period's calendar end: a
    status line reports what has happened, and counting to the end of a week that
    has not happened yet would compare a partial count against a full allowance.
    """
    if period == "day":
        return day, day
    if period == "week":
        return day - timedelta(days=day.weekday()), day
    if period == "month":
        return day.replace(day=1), day
    raise ValueError(f"period must be one of {PERIODS}, got {period!r}")


def evaluate(count: int, target: Target) -> Status:
    """Classify ``count`` against ``target``.

    ``over`` wins over ``under`` when a degenerate target could somehow report
    both, because a breached ceiling is the fact worth surfacing.
    """
    if target.is_open:
        return "untargeted"
    if target.maximum is not None and count > target.maximum:
        return "over"
    if target.minimum is not None and count < target.minimum:
        return "under"
    return "clear"


def status_icon(status: Status) -> str:
    """The glyph for a status, or a neutral dot for anything unrecognized."""
    return _STATUS_ICON.get(status, "•")


def period_phrase(period: str) -> str:
    """How a period reads inside a sentence: ``today`` / ``this week``."""
    return _PERIOD_PHRASE.get(period, "today")


def format_progress(count: int, target: Target) -> str:
    """One line of the form ``3 this week (2–4) ✅``.

    The count and its window come first because that is the fact; the target and
    the verdict follow. An untargeted monitor prints the fact and stops.
    """
    phrase = period_phrase(target.period)
    if target.is_open:
        return f"{count} {phrase}"
    status = evaluate(count, target)
    return f"{count} {phrase} ({target.bounds_label()}) {status_icon(status)}"


# ---------------------------------------------------------------------------
# Parsing a typed target
# ---------------------------------------------------------------------------
#: ``zero`` / ``none`` — the whole target in one word.
_ZERO_RE = re.compile(r"^(?:zero|none)$", re.IGNORECASE)
#: ``<=5/day``, ``≤5 per day``, ``max 5/week``, ``5/month``.
_CAP_RE = re.compile(
    r"^(?:<=|≤|max\s+|up\s+to\s+)?(\d{1,3})\s*(?:/|\s+per\s+)\s*(day|week|month)$",
    re.IGNORECASE,
)
#: ``2-4/week``, ``2 – 4 per week``.
_RANGE_RE = re.compile(
    r"^(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(?:/|\s+per\s+)\s*(day|week|month)$",
    re.IGNORECASE,
)
#: ``>=2/week`` — a floor with no ceiling.
_FLOOR_RE = re.compile(
    r"^(?:>=|≥|at\s+least\s+)(\d{1,3})\s*(?:/|\s+per\s+)\s*(day|week|month)$",
    re.IGNORECASE,
)
#: Whether a field was *meant* as a target. Anything that opens with a digit or a
#: complete comparison operator, or is the bare word zero/none, is a target
#: attempt — so a malformed one is reported as a bad target rather than quietly
#: filed as some other field.
#:
#: The operators are matched whole (``<=``, not a bare ``<``) so a field that
#: merely starts with an angle bracket — pasted markup, most likely — is handled
#: by the caller's generic branch, which escapes it before quoting it back.
_TARGET_ATTEMPT_RE = re.compile(
    r"^(?:<=|>=|≤|≥|\d|zero$|none$|max\s|at\s+least\s|up\s+to\s)", re.IGNORECASE
)


def looks_like_target(text: str) -> bool:
    """Whether ``text`` was plainly intended as a target specification."""
    return _TARGET_ATTEMPT_RE.match((text or "").strip()) is not None


def parse_target(text: str) -> Target:
    """Parse a typed target such as ``2-4/week`` into a :class:`Target`.

    Accepted, case-insensitively and with ``per`` interchangeable with ``/``:

    * ``zero`` / ``none`` → nothing, ever
    * ``<=5/day``, ``max 5 per day``, ``5/day`` → a ceiling
    * ``2-4/week`` → a band
    * ``>=2/week``, ``at least 2 per week`` → a floor

    Raises :class:`TargetParseError` with a message meant for the user.
    """
    raw = (text or "").strip()
    if not raw:
        raise TargetParseError("A target can't be empty.")

    if _ZERO_RE.match(raw):
        return Target(period="day", maximum=0)

    match = _RANGE_RE.match(raw)
    if match is not None:
        low, high = int(match.group(1)), int(match.group(2))
        if low > high:
            raise TargetParseError(
                f"That range runs backwards — write it as <i>{high}-{low}</i>."
            )
        return _build(period=match.group(3), minimum=low, maximum=high)

    match = _FLOOR_RE.match(raw)
    if match is not None:
        return _build(period=match.group(2), minimum=int(match.group(1)))

    match = _CAP_RE.match(raw)
    if match is not None:
        return _build(period=match.group(2), maximum=int(match.group(1)))

    raise TargetParseError(
        "I couldn't read that target. Try <i>zero</i>, <i>≤5/day</i>, "
        "<i>1/month</i>, or <i>2-4/week</i>."
    )


def _build(
    *, period: str, minimum: int | None = None, maximum: int | None = None
) -> Target:
    """Construct a :class:`Target`, re-raising bounds failures as user-facing."""
    try:
        return Target(
            period=period.lower(),  # type: ignore[arg-type]
            minimum=minimum,
            maximum=maximum,
        )
    except ValueError as exc:
        raise TargetParseError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Display keys
# ---------------------------------------------------------------------------
def split_emoji_prefix(name: str) -> tuple[str | None, str]:
    """Split a leading emoji off a monitor name: ``🌿 Cannabis`` → ``("🌿", "Cannabis")``.

    A monitor board is read at a glance, and a leading glyph is what makes four
    rows scannable. Keeping the emoji in its own field — rather than inside the
    name — means the de-duplication key is the *word*, so "🌿 Cannabis" and
    "Cannabis" cannot both be active at once.

    Only a genuine symbol counts as a prefix: anything alphanumeric stays part of
    the name, so a name is never silently decapitated.
    """
    text = (name or "").strip()
    if not text:
        return None, ""
    head = text[0]
    if head.isalnum() or head in {"_", "-", "(", '"', "'"}:
        return None, text
    if unicodedata.category(head).startswith("P"):
        # Punctuation is not decoration — a name opening with one is unusual
        # enough that guessing would be worse than leaving it alone.
        return None, text
    return head, text[1:].strip() or text
