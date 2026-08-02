"""Optional model-assisted meal parsing — segmentation only, never nutrition.

Release 4. This module may return exactly one thing: a list of
``{food, qty, unit}`` triples, the same shape :mod:`bot.meal_text` produces
deterministically. Those triples are then resolved against *stored* definitions
by :mod:`bot.nutrition_resolution`, so no calorie or macro value in the database
can originate from a model. That is a structural guarantee, not a prompt
instruction: :func:`_coerce_items` discards every field it does not recognize,
so a model volunteering ``"calories": 500`` has that value dropped before it can
reach anything.

Everything here is designed to be skippable:

* no key, no consent, no unresolved items → the module is never called;
* any failure — network, quota, timeout, malformed output, refusal — returns an
  empty list, and the caller keeps its deterministic result. Logging a meal must
  never depend on a rate-limited free tier being awake.

Provider pluggability is a small protocol, not a framework. ``GeminiParser``
speaks Google's REST API directly; swapping in an OpenAI-compatible endpoint
means writing another class with the same ``parse`` method and no handler change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "MealParser",
    "GeminiParser",
    "ParsedItem",
    "build_parser",
    "MAX_MODEL_ITEMS",
]

#: Never accept more items than a meal can hold, whatever the model returns.
MAX_MODEL_ITEMS = 20
_MAX_FIELD_LENGTH = 60
# Measured against the live API: the Flash models are "thinking" models, and with
# reasoning left on a one-line segmentation took long enough to blow an 8s budget
# on ordinary input. Thinking is disabled below, which brings a typical call well
# under a second; this budget is the slow-network allowance, not the usual case.
_TIMEOUT_SECONDS = 15.0

#: Number words the model (or a user) may hand back instead of a digit. Mapping
#: "two" to 2 is faithful transcription, not invention, and it is done here —
#: deterministically, in our code — rather than trusted to the model.
_NUMBER_WORDS = {
    "a": "1", "an": "1", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "half": "0.5",
}
_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Deliberately terse and closed-world. The model is asked to *segment*, not to
# know anything about food. It is told to leave unknowns empty rather than guess,
# because a plausible invented unit is worse than a missing one — the missing one
# gets reported to the user, the invented one silently resolves to the wrong
# amount.
_SYSTEM_PROMPT = """\
You split a short meal description into items. You do not know nutrition.

Return JSON only: {"items": [{"food": str, "qty": str, "unit": str}, ...]}

Rules:
- "food" is the food name alone, with no amount in it.
- "qty" is DIGITS ONLY ("2", "0.5"). Write number words as digits: two -> "2",
  "a"/"an" -> "1", half -> "0.5". Use "" if the user gave no amount at all.
- "unit" is the unit as written (g, ml, cup, slice, ...), or "" if none.
- Never invent an amount or a unit the user did not give. Empty is correct.
- Never output calories, macros, or any nutrition value.
- Split combined items ("eggs and toast") into separate entries.
- Preserve the user's wording for the food name; do not translate or expand it.
"""


@dataclass(frozen=True)
class ParsedItem:
    """One model-proposed item. Strings only — resolution happens elsewhere."""

    food: str
    qty: str = ""
    unit: str = ""

    def as_tokens(self) -> tuple[str, ...]:
        """Quantity tokens in the shape the shared quantity parser expects."""
        if not self.qty:
            return ()
        return (self.qty, self.unit) if self.unit else (self.qty,)


class MealParser(Protocol):
    """The whole provider contract."""

    async def parse(self, text: str) -> list[ParsedItem]:
        """Segment ``text``; return ``[]` on any failure."""
        ...


def _clean_field(value: Any) -> str:
    """Coerce one model field to a bounded, single-line string."""
    if not isinstance(value, str):
        # A number for qty is reasonable; anything else is discarded.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = f"{value:g}"
        else:
            return ""
    return " ".join(value.split())[:_MAX_FIELD_LENGTH]


def _clean_qty(value: Any) -> str:
    """Normalize a quantity to a bare decimal string, or "" if it isn't one.

    A model that returns "two" or "a" is transcribed to a digit. Anything that
    still isn't a number becomes empty, which surfaces downstream as "needs an
    amount" — a clear, fixable message — rather than being passed on to fail as
    an unsupported unit.
    """
    text = _clean_field(value)
    if not text:
        return ""
    candidate = text.replace(",", "").strip()
    try:
        number = float(candidate)
    except ValueError:
        mapped = _NUMBER_WORDS.get(candidate.casefold())
        return mapped or ""
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        return ""
    return f"{number:g}"


def _coerce_items(payload: Any) -> list[ParsedItem]:
    """Accept only well-formed ``{food, qty, unit}`` entries; drop everything else.

    This is the enforcement point for "a model never supplies nutrition". Fields
    outside the triple are not sanitized or logged — they are simply never read,
    so there is no path by which one could reach a log row.
    """
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []

    items: list[ParsedItem] = []
    for raw in raw_items[:MAX_MODEL_ITEMS]:
        if not isinstance(raw, dict):
            continue
        food = _clean_field(raw.get("food"))
        if not food:
            continue
        items.append(
            ParsedItem(
                food=food,
                qty=_clean_qty(raw.get("qty")),
                unit=_clean_field(raw.get("unit")),
            )
        )
    return items


def _extract_text(response: Any) -> str:
    """Pull the model's text out of a Gemini response, tolerantly."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    )


class GeminiParser:
    """Google Gemini backend. Parsing only, bounded, and fail-soft."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client: Any | None = None,
        timeout: float = _TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._timeout = timeout

    async def parse(self, text: str) -> list[ParsedItem]:
        if not self._api_key or not text.strip():
            return []
        try:
            payload = await self._request(text)
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
            # Sanitized category only: never the key, the URL with the key, the
            # user's text, or the raw provider error body.
            logger.info("Meal parser unavailable (%s); using local parsing only",
                        type(exc).__name__)
            return []
        except Exception:
            logger.warning("Meal parser failed unexpectedly", exc_info=False)
            return []

        raw_text = _extract_text(payload)
        if not raw_text:
            return []
        try:
            decoded = json.loads(raw_text)
        except (TypeError, ValueError):
            logger.info("Meal parser returned non-JSON; using local parsing only")
            return []
        return _coerce_items(decoded)

    async def _request(self, text: str) -> Any:
        body = {
            # Only the current message. No history, no user id, no prior logs.
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
            "generationConfig": {
                "temperature": 0,
                # No thinkingConfig here on purpose: the newer Flash models
                # reject thinkingBudget=0 outright (HTTP 400), so the reasoning
                # cost is avoided by choosing a non-thinking model instead — see
                # GEMINI_MODEL in bot/config.py.
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "items": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "food": {"type": "STRING"},
                                    "qty": {"type": "STRING"},
                                    "unit": {"type": "STRING"},
                                },
                                "required": ["food"],
                            },
                        }
                    },
                    "required": ["items"],
                },
            },
        }
        url = _ENDPOINT.format(model=self._model)
        # The key travels in a header, never in the URL, so it cannot leak
        # through a logged request line or an error message containing the URL.
        headers = {"x-goog-api-key": self._api_key}

        if self._client is not None:
            response = await self._client.post(
                url, json=body, headers=headers, timeout=self._timeout
            )
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            return response.json()


def build_parser(api_key: str, model: str) -> MealParser | None:
    """Return a parser, or ``None`` when no key is configured.

    ``None`` is the normal state, not an error: the deterministic path is
    complete on its own and this is strictly an addition to it.
    """
    if not api_key:
        return None
    return GeminiParser(api_key, model)


def items_to_text(items: Sequence[ParsedItem]) -> list[str]:
    """Render model items back into segment strings the local parser can read."""
    lines: list[str] = []
    for item in items:
        qty = " ".join(token for token in item.as_tokens())
        lines.append(f"{qty} {item.food}".strip())
    return lines
