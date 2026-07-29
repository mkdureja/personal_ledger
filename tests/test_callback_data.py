"""Strict base-36 callback encode/decode (plan §6.1 / §9.2)."""

from __future__ import annotations

import pytest

from bot.callback_data import parse_base36, to_base36

MAX_SIGNED_64 = 2**63 - 1


@pytest.mark.parametrize("value", [0, 1, 9, 10, 35, 36, 123456789, MAX_SIGNED_64])
def test_round_trip(value: int) -> None:
    assert parse_base36(to_base36(value)) == value


def test_encoding_is_canonical_lowercase() -> None:
    assert to_base36(0) == "0"
    assert to_base36(10) == "a"
    assert to_base36(35) == "z"
    assert to_base36(36) == "10"
    assert to_base36(1295) == "zz"


def test_max_signed_64bit_tokens_stay_under_64_bytes() -> None:
    # Every Phase 1 payload embeds a couple of these; the compact encoding must
    # keep the whole callback comfortably under Telegram's 64-byte cap.
    token = to_base36(MAX_SIGNED_64)
    assert len(token.encode("utf-8")) < 64
    assert len(token) == 13


def test_encode_rejects_negative() -> None:
    with pytest.raises(ValueError):
        to_base36(-1)


def test_encode_rejects_bool() -> None:
    # bool is an int subclass; a stray True must not silently encode as "1".
    with pytest.raises(TypeError):
        to_base36(True)


@pytest.mark.parametrize(
    "token",
    [
        "",  # empty
        "-1",  # signed
        " 5",  # leading whitespace
        "5 ",  # trailing whitespace
        "1a2 ",
        "A",  # uppercase
        "Z9",
        "10.0",  # punctuation
        "١٢",  # non-ASCII digits
    ],
)
def test_parse_rejects_malformed(token: str) -> None:
    with pytest.raises(ValueError):
        parse_base36(token)


@pytest.mark.parametrize("token", ["00", "01", "0a", "007", "0z"])
def test_parse_rejects_non_canonical_leading_zero(token: str) -> None:
    with pytest.raises(ValueError):
        parse_base36(token)


def test_parse_accepts_single_zero() -> None:
    assert parse_base36("0") == 0
