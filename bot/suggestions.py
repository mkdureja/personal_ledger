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

from .meal_models import PREFERENCE_SOURCE_TYPES

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
# An explicit meal shortcut outranks even a pin *within its meal type*, because
# it is the more specific statement: a pin says "always show me this", a shortcut
# says "this belongs to my snacks". Both are the user's own words rather than
# anything inferred, so the narrower one wins where it applies.
_SHORTCUT_BONUS = 2_000_000.0


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
    #: Explicitly marked by the user as belonging to the meal type being
    #: rendered. Set by the caller from ``get_meal_shortcuts``.
    is_meal_shortcut: bool = False


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
    if candidate.is_meal_shortcut:
        value += _SHORTCUT_BONUS
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


def annotate_defaults(
    choices: list[dict], preferences: dict[tuple[str, int], dict]
) -> list[dict]:
    """Attach each choice's stored "usual" amount from one batched preference map.

    Pure, no I/O: the caller reads ``get_food_preferences`` **once** per render and
    passes the map here, so a picker never issues a query per displayed row and the
    keyboard layer performs no I/O at all.

    Each returned choice gains:

    * ``default`` — ``{"amount", "unit"}`` when a *complete* pair is stored, else
      ``None``. This is what makes a tap write immediately, so it is also what a
      label must disclose.
    * ``needs_repair`` — ``True`` when exactly one half of the pair is stored. A
      half-stored default is neither usable nor absent; it must surface as
      needing repair rather than silently doing nothing.

    Catalog rows are annotated too, as of schema v16. Before that the preference
    table's CHECK could not hold one, which meant a shared staple could never be
    an instant row and the only way to get one was a private copy of something
    the catalog already had. The row stays shared; the usual amount stays this
    user's.
    """
    annotated: list[dict] = []
    for choice in choices:
        enriched = dict(choice)
        enriched["default"] = None
        enriched["needs_repair"] = False
        source_type = choice.get("source_type")
        if source_type in PREFERENCE_SOURCE_TYPES:
            row = preferences.get((source_type, choice.get("id"))) or {}
            amount = row.get("default_amount")
            unit = row.get("default_unit")
            if amount is not None and unit is not None:
                enriched["default"] = {"amount": float(amount), "unit": str(unit)}
            elif (amount is None) != (unit is None):
                enriched["needs_repair"] = True
        annotated.append(enriched)
    return annotated
