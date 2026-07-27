"""Deterministic, transparent ranking of food/recipe suggestions.

No machine learning: an explicit pin always wins; otherwise candidates are
ordered by same-meal-type frequency over the user's completed history, then
recency, then overall frequency, with a stable name/id tie-break so the order
never wobbles. Learns only from completed meals (``diet_log_items``), never from
exploratory taps. All inputs are already owner-scoped by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Transparent, tunable weights.
_MEAL_TYPE_WEIGHT = 3.0
_GENERAL_WEIGHT = 1.0
_RECENT_DAYS = 7
_MONTH_DAYS = 30
_RECENT_BONUS = 2.0
_MONTH_BONUS = 1.0
# A pin must outrank any inferred score, so its bonus exceeds any realistic
# frequency total for a personal ledger.
_PIN_BONUS = 1_000_000.0


@dataclass(frozen=True)
class Candidate:
    """One rankable saved food or recipe with its usage signal."""

    source_type: str  # 'food' | 'recipe'
    source_id: int
    name_key: str  # normalized, for a deterministic tie-break
    meal_uses: int = 0
    total_uses: int = 0
    last_used: datetime | None = None
    is_pinned: bool = False
    hidden: bool = False


def _recency_bonus(last_used: datetime | None, now: datetime) -> float:
    if last_used is None:
        return 0.0
    age_days = (now - last_used).days
    if age_days <= _RECENT_DAYS:
        return _RECENT_BONUS
    if age_days <= _MONTH_DAYS:
        return _MONTH_BONUS
    return 0.0


def score(candidate: Candidate, now: datetime) -> float:
    """The transparent ranking score (higher is better)."""
    value = (
        candidate.meal_uses * _MEAL_TYPE_WEIGHT
        + candidate.total_uses * _GENERAL_WEIGHT
        + _recency_bonus(candidate.last_used, now)
    )
    if candidate.is_pinned:
        value += _PIN_BONUS
    return value


def rank(candidates: list[Candidate], now: datetime) -> list[Candidate]:
    """Return non-hidden candidates ordered best-first, deterministically.

    Ties break on the normalized name, then source type, then id — so equal
    scores always produce the same order regardless of input ordering.
    """
    visible = [candidate for candidate in candidates if not candidate.hidden]
    return sorted(
        visible,
        key=lambda c: (-score(c, now), c.name_key, c.source_type, c.source_id),
    )
