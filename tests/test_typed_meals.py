"""Release 3.2 — deterministic typed meals.

The feature's value is entirely in what it *refuses* to do. Parsing a meal line
is easy; the hard part is never turning a half-understood sentence into a
confident log. So the weight here sits on:

1. **Nothing is invented.** Every calorie traces to a stored definition. An
   unknown food, a missing amount, and an unsupported unit each stay out of the
   meal with a distinct reason.
2. **Ambiguity is not broken by ranking.** Two plausible matches leaves the item
   unresolved and names the candidates.
3. **Nothing is written before the confirm tap**, and a stale preview cannot
   write at all.

Release 4 will feed model output through this same path, so these are the
guarantees that keep a model from ever supplying a nutrient number.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot.handlers import describe
from bot.meal_text import MAX_SEGMENTS, parse_meal_text
from bot.services.typed_meal import plan_typed_meal

UID = 123456789
OTHER = 987654321


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------
def _shape(text):
    return [(s.name, s.quantity_tokens) for s in parse_meal_text(text)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("100g oats", [("oats", ("100", "g"))]),
        ("100 g oats", [("oats", ("100", "g"))]),
        ("oats 100g", [("oats", ("100", "g"))]),
        ("2 eggs", [("eggs", ("2",))]),
        ("coffee", [("coffee", ())]),
        ("2.5 kg rice", [("rice", ("2.5", "kg"))]),
        # Separators
        ("2 eggs, 100g oats", [("eggs", ("2",)), ("oats", ("100", "g"))]),
        ("2 eggs and 100g oats", [("eggs", ("2",)), ("oats", ("100", "g"))]),
        ("2 eggs + 100g oats", [("eggs", ("2",)), ("oats", ("100", "g"))]),
        ("2 eggs\n100g oats", [("eggs", ("2",)), ("oats", ("100", "g"))]),
        # Multi-word names survive intact.
        ("200g greek yogurt", [("greek yogurt", ("200", "g"))]),
        ("chicken curry 1 serving", [("chicken curry", ("1", "serving"))]),
    ],
)
def test_parse_meal_text_reads_the_shapes_people_type(text, expected):
    assert _shape(text) == expected


def test_whitespace_and_empty_segments_are_dropped():
    assert _shape("  ,  , 2 eggs ,, ") == [("eggs", ("2",))]
    assert parse_meal_text("") == []
    assert parse_meal_text("   ") == []
    assert parse_meal_text(None) == []


@pytest.mark.parametrize(
    "text, expected_name",
    [
        ("200g salmon.", "salmon"),
        ("100g rice!", "rice"),
        ("oats,", "oats"),
        ("2 eggs?", "eggs"),
        ('100g "oats"', "oats"),
        # Punctuation inside a name is meaningful and must survive.
        ("100g half-fat milk", "half-fat milk"),
        ("1 serving shepherd's pie", "shepherd's pie"),
    ],
)
def test_sentence_punctuation_is_trimmed_from_food_names(text, expected_name):
    """Dictated speech arrives punctuated.

    A voice note transcribed as "200g salmon." produced the name "salmon.",
    which cannot match a catalog entry called "salmon" — the trailing full stop
    silently cost an exact match. Only the edges are trimmed, so hyphens and
    apostrophes inside a name are untouched.
    """
    segments = parse_meal_text(text)
    assert len(segments) == 1
    assert segments[0].name == expected_name


def test_a_bare_quantity_never_becomes_a_confident_item():
    """"100g" names no food, so it must not resolve to anything.

    The parser does not know which words are units, so it cannot tell "100g"
    (a unit, no food) from "2 eggs" (a count of a food) by shape alone. It
    therefore refuses to invent a quantity here; the segment is carried through
    as an unrecognizable name and reported as unknown downstream.
    """
    segments = parse_meal_text("100g")
    assert len(segments) == 1
    assert segments[0].has_quantity is False


def test_segment_count_is_bounded():
    segments = parse_meal_text(", ".join(f"{n} eggs" for n in range(1, 40)))
    assert len(segments) == MAX_SEGMENTS


def test_a_very_long_segment_is_truncated_not_rejected():
    segments = parse_meal_text("x" * 500)
    assert len(segments) == 1
    assert len(segments[0].name) <= 120


# ---------------------------------------------------------------------------
# Resolution planning
# ---------------------------------------------------------------------------
_OATS = {
    "id": 1, "name": "oats", "base_unit": "g", "basis_amount": 100.0,
    "calories": 380, "protein_g": 13.0, "carbs_g": 67.0, "fat_g": 7.0,
}
_CATALOG_BANANA = {
    "id": 50, "name": "banana", "base_unit": "g", "basis_amount": 100.0,
    "calories": 89, "protein_g": 1.1, "carbs_g": 23.0, "fat_g": 0.3,
    "provider": "usda", "provider_revision": "r1",
}


def _db(*, foods=(), recipes=(), catalog=(), portions=(), ingredients=()):
    return SimpleNamespace(
        list_foods=AsyncMock(return_value=list(foods)),
        list_recipes=AsyncMock(return_value=list(recipes)),
        search_catalog=AsyncMock(return_value=list(catalog)),
        get_food_portions=AsyncMock(return_value=list(portions)),
        get_catalog_portions=AsyncMock(return_value=list(portions)),
        get_recipe_ingredients=AsyncMock(return_value=list(ingredients)),
    )


async def test_a_private_food_resolves_with_real_nutrition():
    plan = await plan_typed_meal(_db(foods=[_OATS]), UID, parse_meal_text("50g oats"))

    assert len(plan.resolved) == 1
    assert plan.resolved[0].source_type == "food"
    assert plan.resolved[0].calories == 190
    assert plan.unresolved == ()


async def test_a_private_food_wins_over_a_catalog_entry_of_the_same_name():
    """The user's own definition is what they meant; the catalog is a fallback."""
    private_banana = dict(_OATS, id=9, name="banana", calories=100)
    db = _db(foods=[private_banana], catalog=[_CATALOG_BANANA])

    plan = await plan_typed_meal(db, UID, parse_meal_text("100g banana"))

    assert plan.resolved[0].source_type == "food"
    assert plan.resolved[0].calories == 100
    db.search_catalog.assert_not_awaited()


async def test_the_catalog_is_used_when_nothing_private_matches():
    db = _db(catalog=[_CATALOG_BANANA])

    plan = await plan_typed_meal(db, UID, parse_meal_text("100g banana"))

    assert plan.resolved[0].source_type == "catalog"
    assert plan.resolved[0].source_provider == "usda"
    assert plan.resolved[0].calories == 89


async def test_an_unknown_food_is_reported_not_guessed():
    plan = await plan_typed_meal(_db(), UID, parse_meal_text("100g quinoa"))

    assert plan.resolved == ()
    assert plan.unresolved[0].reason == "unknown"
    assert "not in your foods" in plan.unresolved[0].explanation


async def test_a_missing_amount_is_its_own_reason():
    plan = await plan_typed_meal(_db(foods=[_OATS]), UID, parse_meal_text("oats"))

    assert plan.resolved == ()
    assert plan.unresolved[0].reason == "no_quantity"


async def test_an_unsupported_unit_is_reported_rather_than_converted():
    plan = await plan_typed_meal(
        _db(foods=[_OATS]), UID, parse_meal_text("3 handfuls oats")
    )

    assert plan.resolved == ()
    assert plan.unresolved[0].reason == "bad_quantity"


async def test_several_catalog_matches_stay_unresolved_and_name_the_options():
    matches = [
        dict(_CATALOG_BANANA, id=1, name="banana raw"),
        dict(_CATALOG_BANANA, id=2, name="banana dried"),
    ]
    plan = await plan_typed_meal(_db(catalog=matches), UID, parse_meal_text("100g banana"))

    assert plan.resolved == ()
    assert plan.unresolved[0].reason == "ambiguous"
    assert set(plan.unresolved[0].candidates) == {"banana raw", "banana dried"}


async def test_an_exact_catalog_name_wins_over_looser_matches():
    matches = [
        dict(_CATALOG_BANANA, id=1, name="banana bread"),
        dict(_CATALOG_BANANA, id=2, name="banana"),
    ]
    plan = await plan_typed_meal(_db(catalog=matches), UID, parse_meal_text("100g banana"))

    assert len(plan.resolved) == 1
    assert plan.resolved[0].source_id == 2


async def test_a_private_food_and_recipe_sharing_a_name_is_ambiguous():
    recipe = {"id": 4, "name": "oats", "yield_unit": "serving", "yield_amount": 1}
    plan = await plan_typed_meal(
        _db(foods=[_OATS], recipes=[recipe]), UID, parse_meal_text("50g oats")
    )

    assert plan.resolved == ()
    assert plan.unresolved[0].reason == "ambiguous"


async def test_a_mixed_line_keeps_the_good_items_and_reports_the_rest():
    db = _db(foods=[_OATS])

    plan = await plan_typed_meal(db, UID, parse_meal_text("50g oats, quinoa, coffee"))

    assert len(plan.resolved) == 1
    assert {u.name for u in plan.unresolved} == {"quinoa", "coffee"}


async def test_totals_treat_unknown_calories_as_unknown_not_zero():
    unknown = dict(_OATS, id=2, name="tea", calories=None)
    db = _db(foods=[_OATS, unknown])

    plan = await plan_typed_meal(db, UID, parse_meal_text("100g oats, 100g tea"))

    assert plan.total_calories == 380
    assert plan.has_unknown_calories is True


async def test_the_planner_reads_the_users_lists_once_for_the_whole_line():
    db = _db(foods=[_OATS])

    await plan_typed_meal(db, UID, parse_meal_text("10g oats, 20g oats, 30g oats"))

    assert db.list_foods.await_count == 1
    assert db.list_recipes.await_count == 1


async def test_the_planner_never_writes():
    db = _db(foods=[_OATS])
    db.log_diet_with_items = AsyncMock()

    await plan_typed_meal(db, UID, parse_meal_text("50g oats"))

    db.log_diet_with_items.assert_not_awaited()


# ---------------------------------------------------------------------------
# The confirm screen and its save path
# ---------------------------------------------------------------------------
def _message():
    return SimpleNamespace(
        chat_id=UID,
        message_id=1,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )


def _update(args, user_id=UID):
    message = _message()
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="T", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        callback_query=None,
        update_id=1,
    ), SimpleNamespace(bot_data={}, user_data={}, args=args)


def _query(data):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=_message(),
        edit_message_reply_markup=AsyncMock(),
    )


def _callback_update(query, user_id=UID):
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id, first_name="T", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        effective_message=query.message,
        update_id=2,
    )


def _describe_db(**kwargs):
    db = _db(**kwargs)
    db.ensure_user = AsyncMock()
    db.log_diet_with_items = AsyncMock(return_value=1)
    return db


async def _preview(text, **kwargs):
    """Run /describe and return (context, db, sent_kwargs)."""
    update, context = _update(text.split())
    db = _describe_db(**kwargs)
    context.bot_data["db"] = db
    await describe.describe_command(update, context)
    return context, db, update.message.reply_text.call_args


async def test_describe_previews_without_writing():
    context, db, call = await _preview("50g oats", foods=[_OATS])

    db.log_diet_with_items.assert_not_awaited()
    assert "Ready to log" in call.args[0]
    assert "190 cal" in call.args[0]
    assert context.user_data["describe_pending"]["items"]


async def test_the_preview_lists_unresolved_items_and_says_they_are_not_logged():
    _context, _db_, call = await _preview("50g oats, quinoa", foods=[_OATS])

    text = call.args[0]
    assert "Not logged" in text
    assert "quinoa" in text


async def test_nothing_resolvable_offers_a_route_forward_and_stores_no_draft():
    context, db, call = await _preview("quinoa")

    assert "couldn't match" in call.args[0]
    assert "/food add" in call.args[0]
    assert "describe_pending" not in context.user_data
    db.log_diet_with_items.assert_not_awaited()


async def test_tapping_a_meal_type_saves_once_through_the_atomic_path():
    context, db, _call = await _preview("50g oats", foods=[_OATS])
    token = context.user_data["describe_pending"]["token"]

    from bot.callback_data import to_base36

    query = _query(f"desc_save_{to_base36(UID)}_{to_base36(token)}_lunch")
    await describe.describe_save_callback(_callback_update(query), context)

    db.log_diet_with_items.assert_awaited_once()
    args = db.log_diet_with_items.await_args.args
    assert args[0] == UID and args[1] == "lunch"
    assert len(args[2]) == 1
    assert "describe_pending" not in context.user_data


async def test_a_stale_preview_cannot_save():
    context, db, _call = await _preview("50g oats", foods=[_OATS])
    from bot.callback_data import to_base36

    stale = context.user_data["describe_pending"]["token"]
    # A newer preview supersedes the first.
    context.user_data["describe_pending"]["token"] = stale + 1

    query = _query(f"desc_save_{to_base36(UID)}_{to_base36(stale)}_lunch")
    await describe.describe_save_callback(_callback_update(query), context)

    db.log_diet_with_items.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_another_users_tap_cannot_save_this_draft():
    context, db, _call = await _preview("50g oats", foods=[_OATS])
    from bot.callback_data import to_base36

    token = context.user_data["describe_pending"]["token"]
    query = _query(f"desc_save_{to_base36(OTHER)}_{to_base36(token)}_lunch")
    await describe.describe_save_callback(_callback_update(query, OTHER), context)

    db.log_diet_with_items.assert_not_awaited()


async def test_an_invalid_meal_type_is_refused():
    context, db, _call = await _preview("50g oats", foods=[_OATS])
    from bot.callback_data import to_base36

    token = context.user_data["describe_pending"]["token"]
    query = _query(f"desc_save_{to_base36(UID)}_{to_base36(token)}_brunch")
    await describe.describe_save_callback(_callback_update(query), context)

    db.log_diet_with_items.assert_not_awaited()


async def test_cancel_discards_without_writing():
    context, db, _call = await _preview("50g oats", foods=[_OATS])
    from bot.callback_data import to_base36

    token = context.user_data["describe_pending"]["token"]
    query = _query(f"desc_cancel_{to_base36(UID)}_{to_base36(token)}")
    await describe.describe_cancel_callback(_callback_update(query), context)

    db.log_diet_with_items.assert_not_awaited()
    assert "describe_pending" not in context.user_data


async def test_a_failed_save_keeps_the_draft_for_a_retry():
    context, db, _call = await _preview("50g oats", foods=[_OATS])
    db.log_diet_with_items = AsyncMock(side_effect=RuntimeError("db down"))
    from bot.callback_data import to_base36

    token = context.user_data["describe_pending"]["token"]
    query = _query(f"desc_save_{to_base36(UID)}_{to_base36(token)}_lunch")
    await describe.describe_save_callback(_callback_update(query), context)

    assert context.user_data["describe_pending"]["items"]
    query.edit_message_reply_markup.assert_not_awaited()


async def test_describe_with_no_text_explains_itself():
    _context, db, call = await _preview("")

    assert "Describe a meal" in call.args[0]
    db.log_diet_with_items.assert_not_awaited()


async def test_describe_refuses_while_a_guided_flow_is_active():
    update, context = _update(["50g", "oats"])
    context.user_data["_ledger_active_conversation"] = ("diet", UID)
    context.bot_data["db"] = _describe_db(foods=[_OATS])

    await describe.describe_command(update, context)

    assert "/cancel" in update.message.reply_text.call_args.args[0]


def test_describe_meal_types_match_the_diet_conversation():
    """One vocabulary: a type saved here must be one /diet also accepts."""
    from bot.handlers.diet import VALID_MEALS

    assert set(describe.MEAL_TYPES) == VALID_MEALS
