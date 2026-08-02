"""Deterministic parsing of a typed meal line into name/quantity segments.

Pure and dependency-free by design: no Telegram, no database, no configuration.
The output is a *description of what the user typed*, never a nutrition claim.
Turning a segment into calories is the job of :mod:`bot.nutrition_resolution`,
working from stored definitions.

This module is also the contract Release 4 has to satisfy. A language model may
only ever produce the same shape this produces — ``{food, qty, unit}`` — so the
deterministic path stays the default and the fallback, and no model output can
introduce a nutrient number.

Supported shapes, chosen because they are what people actually type *and say*:

* ``100g oats`` / ``100 g oats`` — leading quantity, attached or spaced
* ``oats 100g`` — trailing quantity
* ``2 eggs`` — leading count with the unit carried by the food name
* ``coffee`` — no quantity at all
* ``I have eaten 100g rice`` — a spoken opener before the amount

That last shape arrived with voice notes: people type "100g rice" but say "I
have eaten 100g rice", which buries the amount where neither the leading nor the
trailing pattern can reach it. A closed list of opening words that cannot be food
is removed first. It is not general language parsing, and it does not replace the
Release 4 model — that still handles genuinely conversational input.

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

#: Words that can open a spoken meal description but can never begin a food name.
#:
#: Voice made this necessary. People type "100g rice" but *say* "I have eaten
#: 100g rice" — the amount lands mid-phrase, where neither the leading nor the
#: trailing pattern can reach it, and the whole item was lost. Stripping these
#: from the front is not natural-language parsing and not a guess: the list is
#: closed, every entry is a pronoun, an auxiliary, an eating verb, or a meal
#: name, and none of them is a food. Anything not on the list stops the strip
#: immediately, so a real name is never eaten away.
#:
#: This does not replace the Release 4 parser. It rescues the common opener;
#: genuinely conversational input is still the model's job.
_LEADING_FILLER = frozenset(
    {
        "i", "im", "ive", "id", "we", "weve", "my", "me",
        "just", "then", "also", "today", "now",
        "eat", "eaten", "ate", "eating",
        "have", "has", "had", "having",
        "take", "took", "taken", "taking",
        "drank", "drink", "drunk", "consumed",
        "for", "in", "at", "on", "of",
    }
)

#: Meal names, stripped only as a *second attempt*.
#:
#: "for lunch I had 2 eggs" needs them gone, but "breakfast cereal" and
#: "snack bar" are real foods whose names begin with one. Removing them
#: unconditionally would quietly rename a food the user actually eats, so they
#: are used only when the first parse found no amount and dropping them finds
#: one — a strictly better reading, never a worse one.
_MEAL_WORD_FILLER = _LEADING_FILLER | {
    "breakfast", "lunch", "dinner", "snack", "brunch", "supper", "meal",
}


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


def _strip_leading_filler(text: str, filler: frozenset[str]) -> str:
    """Drop opening words that cannot be part of a food name.

    Stops at the first word not in ``filler``, so "chicken and rice" and "date
    syrup" are untouched. Returns the original text if stripping would leave
    nothing — an all-filler segment is better reported as unrecognized than
    silently erased.
    """
    words = text.split()
    index = 0
    while index < len(words):
        candidate = words[index].strip(_EDGE_PUNCTUATION).casefold()
        if candidate not in filler:
            break
        index += 1
    if index == 0 or index >= len(words):
        return text
    return " ".join(words[index:])


def _name_has_filler(name: str) -> bool:
    """Whether a parsed name still contains words no food name would carry."""
    return any(
        word.strip(_EDGE_PUNCTUATION).casefold() in _MEAL_WORD_FILLER
        for word in name.split()
    )


def _match_quantity(text: str) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(name, quantity_tokens)`` if this text carries an amount."""
    match = _LEADING_RE.match(text)
    if match is not None:
        name = _clean_name(match.group(3))
        if name:
            return name, _quantity_tokens(match.group(1), match.group(2))

    match = _TRAILING_RE.match(text)
    if match is not None:
        name = _clean_name(match.group(1))
        if name:
            return name, _quantity_tokens(match.group(2), match.group(3))

    return None


def _parse_segment(raw: str) -> ParsedSegment | None:
    """Split one segment into a name and optional quantity tokens."""
    original = " ".join(raw.split())
    if not original:
        return None
    if len(original) > MAX_SEGMENT_LENGTH:
        original = original[:MAX_SEGMENT_LENGTH].rstrip()

    # Parse from the filler-stripped form, but keep ``raw`` as what the user
    # actually said: an unresolved item is reported back to them, and echoing a
    # trimmed version of their own words would read as though the bot misheard.
    #
    # Note there is no "bare quantity" special case. After a leading number, a
    # single remaining word is genuinely ambiguous — "2 eggs" is a count of a
    # food, "100 g" is a unit with no food — and this module deliberately does
    # not know which strings are units. Treating it as a food name is right for
    # what people actually type; the degenerate "100g" simply fails to resolve
    # and is reported as unknown, which is the honest outcome either way.
    #
    # Meal words are only dropped on the second attempt, and only if doing so
    # actually finds an amount. That keeps "breakfast cereal" intact while still
    # reading "for lunch I had 2 eggs".
    text = _strip_leading_filler(original, _LEADING_FILLER)
    wider = _strip_leading_filler(original, _MEAL_WORD_FILLER)

    # Both readings are scored, not just the first that matches. The trailing
    # pattern is greedy enough to "succeed" badly — "lunch I had 2 eggs" parses
    # as name "lunch I had" with unit "eggs" — and a bad match must not block a
    # good one. A name still carrying filler words is the tell.
    found = _match_quantity(text)
    if wider != text and (found is None or _name_has_filler(found[0])):
        better = _match_quantity(wider)
        if better is not None and not _name_has_filler(better[0]):
            found = better

    if found is not None:
        name, tokens = found
        return ParsedSegment(raw=original, name=name, quantity_tokens=tokens)

    cleaned = _clean_name(text)
    return ParsedSegment(raw=original, name=cleaned or original)


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
