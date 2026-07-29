"""Strict base-36 encode/decode for compact callback payloads.

Phase 1 callback tokens (owner IDs, UI revisions, item indices) are emitted and
parsed by exactly one strict helper so a payload has a single canonical spelling.
Encoding is unsigned, lowercase ``[0-9a-z]`` with no leading zeros. Decoding
rejects empty, signed, whitespace-bearing, non-ASCII, and non-canonical tokens
(e.g. ``"0a"`` or ``"00"``) rather than silently accepting an alternate spelling
of the same integer — that keeps embedded owner comparisons unambiguous.
"""

from __future__ import annotations

import string

_ALPHABET = string.digits + string.ascii_lowercase  # 0-9a-z


def to_base36(num: int) -> str:
    """Encode a non-negative integer as a canonical lowercase base-36 token."""
    if not isinstance(num, int) or isinstance(num, bool):
        raise TypeError(f"Expected int, got {type(num).__name__}")
    if num < 0:
        raise ValueError(f"Cannot encode a negative number: {num}")
    if num == 0:
        return "0"
    chars: list[str] = []
    while num:
        num, rem = divmod(num, 36)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def parse_base36(text: str) -> int:
    """Decode a canonical lowercase base-36 token to a non-negative integer.

    Raises ``ValueError`` for anything that is not the exact canonical spelling:
    empty strings, characters outside ``[0-9a-z]`` (signs, whitespace, uppercase,
    non-ASCII), and non-canonical forms carrying leading zeros.
    """
    if not isinstance(text, str):
        raise TypeError(f"Expected str, got {type(text).__name__}")
    if not text:
        raise ValueError("Cannot parse an empty base-36 token")
    if any(ch not in _ALPHABET for ch in text):
        raise ValueError(f"Invalid base-36 token: {text!r}")
    value = int(text, 36)
    if to_base36(value) != text:
        raise ValueError(f"Non-canonical base-36 token: {text!r}")
    return value
