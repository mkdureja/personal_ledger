"""Deterministic parsing of a typed meal line into name/quantity segments.

Pure and dependency-free by design: no Telegram, no database, no configuration.
The output is a *description of what the user typed*, never a nutrition claim.
Turning a segment into calories is the job of :mod:`bot.nutrition_resolution`,
working from stored definitions.

This module is also the contract Release 4 has to satisfy. A language model may
only ever produce the same shape this produces — ``{food, qty, unit}`` — so the
deterministic path stays the default and the fallback, and no model output can
introduce a nutrient number.

Supported shapes, chosen because they are what people actually type:

* ``100g oats`` / ``100 g oats`` — leading quantity, attached or spaced
* ``oats 100g`` — trailing quantity
* ``2 eggs`` — leading count with the unit carried by the food name
* ``coffee`` — no quantity at all

Segments are split on commas, newlines, ``+``, and a standalone ``and``. Nothing
here guesses a quantity: a segment without one is reported as having none, and
the caller decides whether that is resolvable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["ParsedSegment", "parse_meal_text", "MAX_SEGMENTS", "MAX_SEGMENT_LENGTH"]

#: A meal is a handful of items. The cap matches ``nutrition.MAX_MEAL_ITEMS`` in
#: spirit — refuse absurd input early rather than build a draft nobody wants.
MAX_SEGMENTS = 20
MAX_SEGMENT_LENGTH = 120

#: Split on commas, newlines, semicolons, ``+``, and a standalone "and"/"&".
_SPLIT_RE = re.compile(r",|\n|;|\+|&|\band\b", re.IGNORECASE)

#: Sentence punctuation to shave off a name's ends. Dictated text arrives
#: punctuated — "200g salmon." — and a trailing full stop made the name miss an
#: otherwise exact catalog match. Only the ends are touched, so hyphens and
#: apostrophes inside a name ("half-fat", "shepherd's pie") survive.
_EDGE_PUNCTUATION = ".,;:!?\"'`()[]{}"

#: ``100g`` / ``2.5 kg`` / ``1/2 cup`` are not all supported; only a decimal
#: amount optionally followed by a unit word. The unit is left as written and
#: validated later by the shared quantity parser, so this module never decides
#: which units exist.
_NUMBER = r"\d+(?:\.\d+)?"
_LEADING_RE = re.compile(rf"^({_NUMBER})\s*([a-zA-Z]+)?\s+(.*)$")
_TRAILING_RE = re.compile(rf"^(.*?)\s+({_NUMBER})\s*([a-zA-Z]+)?$")


@dataclass(frozen=True)
class ParsedSegment:
    """One typed item: the name as written, plus any quantity tokens found.

    ``quantity_tokens`` is exactly what the shared quantity parser expects — a
    sequence like ``["100", "g"]`` or ``["2"]``. It is empty when the user gave
    no amount, which is information, not an error.
    """

    raw: str
    name: str
    quantity_tokens: tuple[str, ...] = field(default=())

    @property
    def has_quantity(self) -> bool:
        return bool(self.quantity_tokens)


def _quantity_tokens(amount: str, unit: str | None) -> tuple[str, ...]:
    return (amount, unit) if unit else (amount,)


def _clean_name(value: str) -> str:
    """Trim sentence punctuation from a food name's edges."""
    return value.strip().strip(_EDGE_PUNCTUATION).strip()


def _parse_segment(raw: str) -> ParsedSegment | None:
    """Split one segment into a name and optional quantity tokens."""
    text = " ".join(raw.split())
    if not text:
        return None
    if len(text) > MAX_SEGMENT_LENGTH:
        text = text[:MAX_SEGMENT_LENGTH].rstrip()

    # Note there is no "bare quantity" special case. After a leading number, a
    # single remaining word is genuinely ambiguous — "2 eggs" is a count of a
    # food, "100 g" is a unit with no food — and this module deliberately does
    # not know which strings are units. Treating it as a food name is right for
    # what people actually type; the degenerate "100g" simply fails to resolve
    # and is reported as unknown, which is the honest outcome either way.
    match = _LEADING_RE.match(text)
    if match is not None:
        amount, unit = match.group(1), match.group(2)
        name = _clean_name(match.group(3))
        if name:
            return ParsedSegment(
                raw=text, name=name, quantity_tokens=_quantity_tokens(amount, unit)
            )

    match = _TRAILING_RE.match(text)
    if match is not None:
        name, amount, unit = _clean_name(match.group(1)), match.group(2), match.group(3)
        if name:
            return ParsedSegment(
                raw=text, name=name, quantity_tokens=_quantity_tokens(amount, unit)
            )

    cleaned = _clean_name(text)
    return ParsedSegment(raw=text, name=cleaned or text)


def parse_meal_text(text: str) -> list[ParsedSegment]:
    """Split a typed meal into segments, in the order the user wrote them.

    Returns an empty list for empty input. Never raises for ordinary text: an
    unparseable segment becomes a name with no quantity, which the caller
    surfaces as unresolved rather than discarding silently.
    """
    if not isinstance(text, str):
        return []
    segments: list[ParsedSegment] = []
    for chunk in _SPLIT_RE.split(text):
        parsed = _parse_segment(chunk)
        if parsed is not None:
            segments.append(parsed)
        if len(segments) >= MAX_SEGMENTS:
            break
    return segments
