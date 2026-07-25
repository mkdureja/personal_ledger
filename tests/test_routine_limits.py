"""Tests for routine field size caps (guard against oversized anchor payloads)."""

from __future__ import annotations

import pytest

from bot.routine import (
    MAX_ANCHOR_EMOJI_LENGTH,
    MAX_ANCHOR_ID_LENGTH,
    MAX_ANCHOR_TITLE_LENGTH,
    MAX_QUOTE_LENGTH,
    RoutineConfigError,
    load_routine,
)


def _write(tmp_path, text):
    path = tmp_path / "routine.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_oversized_quote_rejected(tmp_path):
    quote = "x" * (MAX_QUOTE_LENGTH + 1)
    text = f'quotes:\n  - "{quote}"\nanchors:\n  - {{id: a, time: "08:00"}}\n'
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


def test_oversized_anchor_id_rejected(tmp_path):
    long_id = "i" * (MAX_ANCHOR_ID_LENGTH + 1)
    text = f'anchors:\n  - {{id: {long_id}, time: "08:00"}}\n'
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


def test_oversized_anchor_title_rejected(tmp_path):
    long_title = "t" * (MAX_ANCHOR_TITLE_LENGTH + 1)
    text = f'anchors:\n  - {{id: a, time: "08:00", title: "{long_title}"}}\n'
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


def test_oversized_anchor_emoji_rejected(tmp_path):
    long_emoji = "e" * (MAX_ANCHOR_EMOJI_LENGTH + 1)
    text = f'anchors:\n  - {{id: a, time: "08:00", emoji: "{long_emoji}"}}\n'
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


def test_max_length_values_are_accepted(tmp_path):
    quote = "x" * MAX_QUOTE_LENGTH
    title = "t" * MAX_ANCHOR_TITLE_LENGTH
    text = (
        f'quotes:\n  - "{quote}"\n'
        f'anchors:\n  - {{id: a, time: "08:00", title: "{title}", quote: true}}\n'
    )
    routine = load_routine(_write(tmp_path, text))
    assert routine is not None
    assert routine.anchors[0].title == title
