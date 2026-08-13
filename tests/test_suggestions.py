"""Unit tests for the deterministic suggestion ranking (bot/suggestions.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

from bot.suggestions import Candidate, rank

NOW = datetime(2026, 7, 27, 12, 0, 0)


def _c(source_id, name_key, **kw):
    return Candidate(
        source_type=kw.pop("source_type", "food"),
        source_id=source_id,
        name_key=name_key,
        **kw,
    )


def _order(candidates):
    return [(c.source_type, c.source_id) for c in rank(candidates, NOW)]


def test_pin_always_ranks_first_even_with_no_usage():
    pinned = _c(1, "zucchini", is_pinned=True)
    frequent = _c(2, "apple", meal_uses=50, total_uses=99, last_used=NOW)
    assert _order([frequent, pinned])[0] == ("food", 1)


def test_meal_type_frequency_outranks_general_frequency():
    meal_favourite = _c(1, "eggs", meal_uses=5, total_uses=5)
    general = _c(2, "apple", meal_uses=0, total_uses=12)
    # 5*3 + 5 = 20 beats 0*3 + 12 = 12
    assert _order([general, meal_favourite]) == [("food", 1), ("food", 2)]


def test_one_breakfast_outranks_many_dinners():
    """Asking the meal type first has to change what the picker offers.

    Per-use weighting alone could not do this: five dinners of rice scored 5.0
    and buried the single breakfast egg on 4.0, so the breakfast picker led with
    dinner food.
    """
    breakfast_once = _c(1, "eggs", meal_uses=1, total_uses=1)
    dinner_often = _c(2, "rice", meal_uses=0, total_uses=5, last_used=NOW)
    assert _order([dinner_often, breakfast_once]) == [("food", 1), ("food", 2)]


def test_within_the_meal_type_frequency_still_decides():
    """The tier says which question is answered first, not the whole order."""
    usual = _c(1, "oats", meal_uses=9, total_uses=9)
    occasional = _c(2, "eggs", meal_uses=1, total_uses=1)
    assert _order([occasional, usual]) == [("food", 1), ("food", 2)]


def test_a_pin_still_beats_the_meal_type_tier():
    """A pin is the user's own words; the tier is an inference about them."""
    pinned = _c(1, "zucchini", is_pinned=True)
    meal_regular = _c(2, "eggs", meal_uses=20, total_uses=20, last_used=NOW)
    assert _order([meal_regular, pinned])[0] == ("food", 1)


def test_recency_breaks_equal_frequency():
    recent = _c(1, "b-food", total_uses=2, last_used=NOW - timedelta(days=1))
    stale = _c(2, "a-food", total_uses=2, last_used=NOW - timedelta(days=200))
    # equal frequency, but recent gets the +2 recency bonus despite later name
    assert _order([stale, recent])[0] == ("food", 1)


def test_hidden_candidates_are_dropped():
    visible = _c(1, "apple", total_uses=1)
    hidden = _c(2, "banana", total_uses=99, is_pinned=True, hidden=True)
    assert _order([visible, hidden]) == [("food", 1)]


def test_ties_break_deterministically_by_name_then_type_then_id():
    a = _c(3, "alpha")
    b = _c(1, "beta")
    c = _c(2, "alpha", source_type="recipe")
    # all zero score -> order by name_key, then source_type ('food' < 'recipe'),
    # then id: ('food','alpha',3), ('recipe','alpha',2), ('food','beta',1)
    assert _order([b, c, a]) == [("food", 3), ("recipe", 2), ("food", 1)]
