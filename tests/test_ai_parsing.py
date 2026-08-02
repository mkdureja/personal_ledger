"""Release 4 — model-assisted parsing that cannot supply nutrition or leak data.

The model is an optional convenience bolted onto a complete deterministic path,
so almost every test here asserts a *limit* rather than a capability:

1. **Nothing is sent without three independent gates**: something unresolved, a
   configured key, and this user's explicit opt-in. Default is off, and a
   failed consent read counts as "no".
2. **A model can never introduce a nutrient value.** Its output is narrowed to
   ``{food, qty, unit}`` before anything else sees it, and the numbers come from
   the stored definition via the Release 3 resolver.
3. **Every failure is soft.** Timeout, quota, malformed JSON, a raising client —
   each leaves the deterministic result standing, because logging a meal must
   not depend on a free tier being awake.
4. **Only the leftover text leaves the host** — never logs, totals, ids, or the
   items that already resolved locally.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.services import llm_parser
from bot.services.llm_parser import (
    GeminiParser,
    ParsedItem,
    build_parser,
    _coerce_items,
)
from bot.services.typed_meal import (
    TypedMealPlan,
    UnresolvedSegment,
    augment_plan_with_parser,
    plan_typed_meal,
)
from bot.meal_text import parse_meal_text

UID = 123456789

_OATS = {
    "id": 1, "name": "oats", "base_unit": "g", "basis_amount": 100.0,
    "calories": 380, "protein_g": 13.0, "carbs_g": 67.0, "fat_g": 7.0,
}
_EGG = {
    "id": 2, "name": "eggs", "base_unit": "g", "basis_amount": 100.0,
    "calories": 155, "protein_g": 13.0, "carbs_g": 1.1, "fat_g": 11.0,
}


def _db(*, foods=(), recipes=(), catalog=()):
    return SimpleNamespace(
        list_foods=AsyncMock(return_value=list(foods)),
        list_recipes=AsyncMock(return_value=list(recipes)),
        search_catalog=AsyncMock(return_value=list(catalog)),
        get_food_portions=AsyncMock(return_value=[]),
        get_catalog_portions=AsyncMock(return_value=[]),
        get_recipe_ingredients=AsyncMock(return_value=[]),
    )


class _FakeParser:
    """A parser whose output and call log the test controls."""

    def __init__(self, items=(), *, raises=None):
        self.items = list(items)
        self.raises = raises
        self.seen: list[str] = []

    async def parse(self, text):
        self.seen.append(text)
        if self.raises is not None:
            raise self.raises
        return self.items


# ---------------------------------------------------------------------------
# The model can never supply nutrition
# ---------------------------------------------------------------------------
def test_only_food_qty_and_unit_survive_coercion():
    """A model volunteering nutrition has it dropped before anything reads it."""
    payload = {
        "items": [
            {
                "food": "oats",
                "qty": "100",
                "unit": "g",
                "calories": 999,
                "protein_g": 50,
                "source_type": "catalog",
                "source_id": 7,
            }
        ]
    }

    items = _coerce_items(payload)

    assert items == [ParsedItem(food="oats", qty="100", unit="g")]
    assert not hasattr(items[0], "calories")


@pytest.mark.parametrize(
    "payload",
    [
        None, {}, [], "items", 42,
        {"items": None}, {"items": "oats"}, {"items": [None, 3, "x"]},
        {"items": [{}]},                      # no food name
        {"items": [{"food": ""}]},            # blank food name
        {"items": [{"qty": "100", "unit": "g"}]},  # quantity with no food
    ],
)
def test_malformed_model_output_yields_nothing(payload):
    assert _coerce_items(payload) == []


def test_model_output_is_bounded_in_count_and_length():
    payload = {"items": [{"food": f"f{n}"} for n in range(200)]}
    assert len(_coerce_items(payload)) == llm_parser.MAX_MODEL_ITEMS

    long_name = {"items": [{"food": "x" * 500, "qty": "1", "unit": "y" * 500}]}
    item = _coerce_items(long_name)[0]
    assert len(item.food) <= 60 and len(item.unit) <= 60


@pytest.mark.parametrize(
    "given, expected",
    [
        ("2", "2"), ("0.5", "0.5"), ("100", "100"),
        # Number words: the live model returns these despite being asked for
        # digits, so they are normalized here rather than trusted to the prompt.
        ("two", "2"), ("Two", "2"), ("a", "1"), ("an", "1"), ("half", "0.5"),
        ("1,000", "1000"),
        # Not a quantity: becomes empty, which reads downstream as "needs an
        # amount" rather than failing later as an unsupported unit.
        ("some", ""), ("a few", ""), ("", ""), ("-1", ""), ("0", ""),
    ],
)
def test_quantities_are_normalized_to_digits_or_dropped(given, expected):
    item = _coerce_items({"items": [{"food": "oats", "qty": given}]})[0]
    assert item.qty == expected


def test_a_numeric_qty_is_accepted_but_other_types_are_not():
    assert _coerce_items({"items": [{"food": "oats", "qty": 100}]})[0].qty == "100"
    assert _coerce_items({"items": [{"food": "oats", "qty": True}]})[0].qty == ""
    assert _coerce_items({"items": [{"food": "oats", "qty": {"n": 1}}]})[0].qty == ""


async def test_calories_come_from_the_stored_food_not_the_model():
    """The end-to-end version of the same guarantee."""
    db = _db(foods=[_OATS])
    plan = TypedMealPlan(
        unresolved=(UnresolvedSegment(raw="some oats", name="some oats", reason="unknown"),)
    )
    # The model proposes an absurd amount of a food the user *does* have saved.
    parser = _FakeParser([ParsedItem(food="oats", qty="100", unit="g")])

    result = await augment_plan_with_parser(db, UID, plan, parser)

    assert len(result.resolved) == 1
    assert result.resolved[0].calories == 380  # from _OATS, scaled
    assert result.resolved[0].source_type == "food"
    assert result.resolved[0].source_id == 1


# ---------------------------------------------------------------------------
# Gating: nothing is sent unless everything says yes
# ---------------------------------------------------------------------------
async def test_a_fully_resolved_meal_never_calls_the_parser():
    db = _db(foods=[_OATS])
    plan = await plan_typed_meal(db, UID, parse_meal_text("100g oats"))
    parser = _FakeParser([ParsedItem(food="oats", qty="1", unit="g")])

    result = await augment_plan_with_parser(db, UID, plan, parser)

    assert parser.seen == []
    assert result.model_assisted is False


async def test_no_parser_configured_leaves_the_plan_untouched():
    db = _db(foods=[_OATS])
    plan = await plan_typed_meal(db, UID, parse_meal_text("quinoa"))

    result = await augment_plan_with_parser(db, UID, plan, None)

    assert result is plan


def test_build_parser_returns_none_without_a_key():
    assert build_parser("", "gemini-flash-latest") is None
    assert build_parser("k", "gemini-flash-latest") is not None


async def test_only_the_unresolved_text_is_sent():
    """Items that already resolved locally must not leave the host."""
    db = _db(foods=[_OATS])
    plan = await plan_typed_meal(db, UID, parse_meal_text("100g oats, two eggs"))
    assert len(plan.resolved) == 1  # oats resolved locally
    parser = _FakeParser([ParsedItem(food="eggs", qty="100", unit="g")])

    await augment_plan_with_parser(db, UID, plan, parser)

    assert len(parser.seen) == 1
    sent = parser.seen[0]
    assert "two eggs" in sent
    assert "oats" not in sent
    assert str(UID) not in sent


async def test_a_local_resolution_is_never_revisited_by_the_model():
    """Turning the model on cannot change how an already-working meal logs."""
    db = _db(foods=[_OATS, _EGG])
    plan = await plan_typed_meal(db, UID, parse_meal_text("100g oats, a few eggs"))
    before = plan.resolved[0]
    parser = _FakeParser([ParsedItem(food="eggs", qty="50", unit="g")])

    result = await augment_plan_with_parser(db, UID, plan, parser)

    assert result.resolved[0] == before


# ---------------------------------------------------------------------------
# Every failure is soft
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("offline"),
        RuntimeError("boom"),
    ],
)
async def test_a_raising_parser_leaves_the_deterministic_result_standing(error):
    db = _db(foods=[_OATS])
    plan = await plan_typed_meal(db, UID, parse_meal_text("100g oats, quinoa"))
    parser = _FakeParser(raises=error)

    result = await augment_plan_with_parser(db, UID, plan, parser)

    assert len(result.resolved) == 1
    assert result.model_assisted is False


async def test_an_empty_model_response_changes_nothing():
    db = _db(foods=[_OATS])
    plan = await plan_typed_meal(db, UID, parse_meal_text("quinoa"))

    result = await augment_plan_with_parser(db, UID, plan, _FakeParser([]))

    assert result is plan


async def test_a_model_resegmentation_that_still_resolves_nothing_keeps_local_reasons():
    """The user's original wording explains the failure better than the model's."""
    db = _db()
    plan = await plan_typed_meal(db, UID, parse_meal_text("a bowl of quinoa"))
    parser = _FakeParser([ParsedItem(food="quinoa", qty="1", unit="bowl")])

    result = await augment_plan_with_parser(db, UID, plan, parser)

    assert result.resolved == ()
    assert result.unresolved[0].name == "a bowl of quinoa"


# ---------------------------------------------------------------------------
# The Gemini transport itself
# ---------------------------------------------------------------------------
def _response(payload, status=200):
    request = httpx.Request("POST", "https://example.invalid")
    return httpx.Response(status, json=payload, request=request)


class _FakeClient:
    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers or {}, "timeout": timeout}
        )
        if self.raises is not None:
            raise self.raises
        return self.response


def _model_reply(items):
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps({"items": items})}]}}
        ]
    }


async def test_gemini_parser_returns_coerced_items():
    client = _FakeClient(_response(_model_reply([{"food": "oats", "qty": "100", "unit": "g"}])))
    parser = GeminiParser("secret-key", "gemini-flash-latest", client=client)

    items = await parser.parse("100g oats")

    assert items == [ParsedItem(food="oats", qty="100", unit="g")]


async def test_the_key_travels_in_a_header_never_in_the_url():
    client = _FakeClient(_response(_model_reply([])))
    parser = GeminiParser("secret-key", "gemini-flash-latest", client=client)

    await parser.parse("oats")

    call = client.calls[0]
    assert call["headers"]["x-goog-api-key"] == "secret-key"
    assert "secret-key" not in call["url"]


async def test_only_the_supplied_text_is_in_the_request_body():
    client = _FakeClient(_response(_model_reply([])))
    parser = GeminiParser("k", "gemini-flash-latest", client=client)

    await parser.parse("two eggs")

    body = json.dumps(client.calls[0]["json"])
    assert "two eggs" in body
    assert str(UID) not in body
    # One user turn only: no history array, no prior meals.
    assert len(client.calls[0]["json"]["contents"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("dns"),
        httpx.HTTPStatusError(
            "429", request=httpx.Request("POST", "https://x.invalid"),
            response=httpx.Response(429, request=httpx.Request("POST", "https://x.invalid")),
        ),
        OSError("socket"),
    ],
)
async def test_transport_failures_return_no_items(failure):
    parser = GeminiParser("k", "m", client=_FakeClient(raises=failure))
    assert await parser.parse("oats") == []


async def test_a_quota_error_status_returns_no_items():
    client = _FakeClient(_response({"error": "quota"}, status=429))
    parser = GeminiParser("k", "m", client=client)
    assert await parser.parse("oats") == []


async def test_non_json_model_text_returns_no_items():
    reply = {"candidates": [{"content": {"parts": [{"text": "sorry, I can't"}]}}]}
    parser = GeminiParser("k", "m", client=_FakeClient(_response(reply)))
    assert await parser.parse("oats") == []


@pytest.mark.parametrize(
    "reply",
    [{}, {"candidates": []}, {"candidates": [{}]}, {"candidates": [{"content": {}}]}],
)
async def test_a_truncated_or_blocked_response_returns_no_items(reply):
    parser = GeminiParser("k", "m", client=_FakeClient(_response(reply)))
    assert await parser.parse("oats") == []


async def test_an_empty_key_or_blank_text_never_calls_out():
    client = _FakeClient(_response(_model_reply([])))
    assert await GeminiParser("", "m", client=client).parse("oats") == []
    assert await GeminiParser("k", "m", client=client).parse("   ") == []
    assert client.calls == []


def test_parsed_item_tokens_match_the_shared_quantity_shape():
    assert ParsedItem("oats", "100", "g").as_tokens() == ("100", "g")
    assert ParsedItem("eggs", "2", "").as_tokens() == ("2",)
    assert ParsedItem("coffee", "", "").as_tokens() == ()


def test_the_prompt_forbids_nutrition_and_invented_amounts():
    """The prompt is defence in depth; coercion is the real guarantee."""
    prompt = llm_parser._SYSTEM_PROMPT.lower()
    assert "never invent" in prompt
    assert "calories" in prompt


# ---------------------------------------------------------------------------
# Consent gating, at the handler boundary
# ---------------------------------------------------------------------------
from telegram.constants import ChatType  # noqa: E402

from bot import config  # noqa: E402
from bot.handlers import describe, settings as settings_handler  # noqa: E402


def _describe_update(text):
    message = SimpleNamespace(
        chat_id=UID,
        message_id=1,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=UID, first_name="T", username="t"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
        callback_query=None,
        update_id=1,
    ), SimpleNamespace(bot_data={}, user_data={}, args=text.split())


def _consent_db(enabled, *, foods=(), raises=None):
    db = _db(foods=foods)
    db.ensure_user = AsyncMock()
    db.log_diet_with_items = AsyncMock()
    if raises is not None:
        db.get_ai_parsing_enabled = AsyncMock(side_effect=raises)
    else:
        db.get_ai_parsing_enabled = AsyncMock(return_value=enabled)
    return db


async def _run_describe(monkeypatch, *, key, consent, sent_probe, raises=None):
    monkeypatch.setattr(config, "GEMINI_API_KEY", key)
    monkeypatch.setattr(config, "GEMINI_AVAILABLE", bool(key))
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.setattr(describe, "build_parser", lambda *a, **k: sent_probe)

    update, context = _describe_update("quinoa")
    context.bot_data["db"] = _consent_db(consent, raises=raises)
    await describe.describe_command(update, context)
    return update, context


async def test_without_consent_nothing_is_sent(monkeypatch):
    probe = _FakeParser([ParsedItem(food="oats", qty="1", unit="g")])

    await _run_describe(monkeypatch, key="k", consent=False, sent_probe=probe)

    assert probe.seen == []


async def test_without_a_key_nothing_is_sent_even_with_consent(monkeypatch):
    probe = _FakeParser([ParsedItem(food="oats", qty="1", unit="g")])

    await _run_describe(monkeypatch, key="", consent=True, sent_probe=probe)

    assert probe.seen == []


async def test_with_key_and_consent_the_leftover_is_sent(monkeypatch):
    probe = _FakeParser([])

    await _run_describe(monkeypatch, key="k", consent=True, sent_probe=probe)

    assert probe.seen == ["quinoa"]


async def test_an_unreadable_consent_flag_is_treated_as_no(monkeypatch):
    """Fail closed: a broken settings read must never authorize a send."""
    probe = _FakeParser([])

    await _run_describe(
        monkeypatch, key="k", consent=True, sent_probe=probe,
        raises=RuntimeError("settings unavailable"),
    )

    assert probe.seen == []


async def test_the_preview_says_when_ai_helped(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_AVAILABLE", True)
    probe = _FakeParser([ParsedItem(food="oats", qty="100", unit="g")])
    monkeypatch.setattr(describe, "build_parser", lambda *a, **k: probe)

    update, context = _describe_update("a bowl of oats")
    context.bot_data["db"] = _consent_db(True, foods=[_OATS])
    await describe.describe_command(update, context)

    assert "AI helped read this" in update.message.reply_text.call_args.args[0]


async def test_a_locally_resolved_meal_shows_no_ai_note(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_AVAILABLE", True)
    monkeypatch.setattr(describe, "build_parser", lambda *a, **k: _FakeParser([]))

    update, context = _describe_update("100g oats")
    context.bot_data["db"] = _consent_db(True, foods=[_OATS])
    await describe.describe_command(update, context)

    assert "AI helped" not in update.message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# The consent switch itself
# ---------------------------------------------------------------------------
def _settings_update(args):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=UID, first_name="T", username="t"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    ), SimpleNamespace(
        bot_data={
            "db": SimpleNamespace(
                ensure_user=AsyncMock(),
                set_ai_parsing_enabled=AsyncMock(),
                get_ai_parsing_enabled=AsyncMock(return_value=False),
            )
        },
        user_data={},
        args=args,
    )


async def test_aiparse_on_records_consent_and_states_what_is_sent(monkeypatch):
    monkeypatch.setattr(settings_handler, "GEMINI_AVAILABLE", True)
    update, context = _settings_update(["on"])

    await settings_handler.aiparse_command(update, context)

    context.bot_data["db"].set_ai_parsing_enabled.assert_awaited_once_with(UID, True)
    text = update.message.reply_text.call_args.args[0]
    assert "never your logs" in text
    assert "never supplies a nutrition number" in text


async def test_aiparse_off_revokes_consent(monkeypatch):
    monkeypatch.setattr(settings_handler, "GEMINI_AVAILABLE", True)
    update, context = _settings_update(["off"])

    await settings_handler.aiparse_command(update, context)

    context.bot_data["db"].set_ai_parsing_enabled.assert_awaited_once_with(UID, False)


async def test_aiparse_stores_nothing_when_the_bot_has_no_key(monkeypatch):
    """Do not record a preference that cannot take effect."""
    monkeypatch.setattr(settings_handler, "GEMINI_AVAILABLE", False)
    update, context = _settings_update(["on"])

    await settings_handler.aiparse_command(update, context)

    context.bot_data["db"].set_ai_parsing_enabled.assert_not_awaited()
    assert "isn't configured" in update.message.reply_text.call_args.args[0]


async def test_consent_defaults_to_off_and_round_trips(db):
    await db.ensure_user(UID, "t", "Test")

    assert await db.get_ai_parsing_enabled(UID) is False

    await db.set_ai_parsing_enabled(UID, True)
    assert await db.get_ai_parsing_enabled(UID) is True
    row = await db._query_one(
        "SELECT ai_parsing_consented_at FROM user_settings WHERE user_id = ?", (UID,)
    )
    assert row["ai_parsing_consented_at"] is not None

    await db.set_ai_parsing_enabled(UID, False)
    assert await db.get_ai_parsing_enabled(UID) is False
    row = await db._query_one(
        "SELECT ai_parsing_consented_at FROM user_settings WHERE user_id = ?", (UID,)
    )
    assert row["ai_parsing_consented_at"] is None


async def test_a_user_with_no_settings_row_is_opted_out(db):
    """Absence must never read as consent."""
    await db.ensure_user(UID, "t", "Test")
    await db._query_one("DELETE FROM user_settings WHERE user_id = ?", (UID,))

    assert await db.get_ai_parsing_enabled(UID) is False


async def test_consent_is_per_user(db):
    other = 987654321
    await db.ensure_user(UID, "t", "Test")
    await db.ensure_user(other, "o", "Other")

    await db.set_ai_parsing_enabled(UID, True)

    assert await db.get_ai_parsing_enabled(UID) is True
    assert await db.get_ai_parsing_enabled(other) is False
