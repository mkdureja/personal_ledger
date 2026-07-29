"""Phase 1b unit B1: exact Repeat, targeted Undo, and durable receipts.

Covers the repository contracts (plan §7.1-§7.3) and the handler behavior
(§10.1-§10.3): a repeat is a verbatim copy that never re-resolves, a redelivered
update replays instead of duplicating, and an Undo button always removes the
exact meal it was rendered for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.database import MutationSource
from bot.handlers import diet, home, receipts
from bot.handlers.common import activate_conversation, active_conversation_flow
from bot.meal_models import DietEntryMode, RepeatStatus, UndoStatus

UID = next(iter(ALLOWED_USER_IDS))
OTHER_UID = UID + 1

pytestmark = pytest.mark.asyncio


def _item(name: str, **overrides):
    item = {
        "source_type": "food",
        "source_id": 7,
        "source_provider": None,
        "source_revision": None,
        "display_name": name,
        "entered_amount": 2.0,
        "entered_unit": "bowl",
        "resolved_base_amount": 300.0,
        "resolved_base_unit": "g",
        "calories": 250,
        "protein_g": 9.0,
        "carbs_g": 40.0,
        "fat_g": 4.0,
    }
    item.update(overrides)
    return item


async def _log_meal(db, user_id=UID, meal_type="lunch", items=None):
    return await db.log_diet_with_items(
        user_id, meal_type, items or [_item("Dal")]
    )


# ---------------------------------------------------------------------------
# repeat_last_meal — exactness
# ---------------------------------------------------------------------------
async def test_repeat_copies_header_and_items_verbatim(db_with_user):
    db = db_with_user
    original_id = await _log_meal(
        db, items=[_item("Dal"), _item("Rice", source_id=8, calories=180)]
    )

    result = await db.repeat_last_meal(UID)

    assert result.status is RepeatStatus.CREATED
    receipt = result.receipt
    assert receipt.header.meal_id != original_id
    original = await db._get_meal_receipt_locked(UID, original_id)
    assert receipt.header.meal_type == original.header.meal_type
    assert receipt.header.food_items == original.header.food_items
    assert receipt.header.nutrients == original.header.nutrients
    assert [i.display_name for i in receipt.items] == ["Dal", "Rice"]
    for copied, source in zip(receipt.items, original.items, strict=True):
        assert copied.child_id != source.child_id
        assert copied.meal_id == receipt.header.meal_id
        assert copied.item_order == source.item_order
        assert copied.source_type == source.source_type
        assert copied.source_id == source.source_id
        assert copied.entered_amount == source.entered_amount
        assert copied.entered_unit == source.entered_unit
        assert copied.resolved_base_amount == source.resolved_base_amount
        assert copied.calories == source.calories
        assert copied.protein_g == source.protein_g


async def test_repeat_keeps_original_meal_type_not_the_current_hour(db_with_user):
    db = db_with_user
    await _log_meal(db, meal_type="breakfast")
    result = await db.repeat_last_meal(UID)
    assert result.receipt.header.meal_type == "breakfast"


async def test_repeat_does_not_reresolve_after_the_food_changes(db_with_user):
    """The whole point of "exact": editing the source cannot rewrite history."""
    db = db_with_user
    saved = await db.save_food(UID, "Dal", "g", 100, calories=120)
    food_id = saved["food"]["id"]
    await db.log_diet_with_items(
        UID, "lunch", [_item("Dal", source_id=food_id, calories=250)]
    )
    await db.save_food(UID, "Dal", "g", 100, calories=999)

    result = await db.repeat_last_meal(UID)

    assert result.receipt.items[0].calories == 250
    assert result.receipt.header.nutrients.calories == 250


async def test_repeat_of_a_headerless_meal_copies_the_description(db_with_user):
    db = db_with_user
    await db.log_diet(UID, "snack", "Two rotis", 300)
    result = await db.repeat_last_meal(UID)
    assert result.status is RepeatStatus.CREATED
    assert result.receipt.items == ()
    assert result.receipt.header.food_items == "Two rotis"
    assert result.receipt.header.nutrients.calories == 300


async def test_repeat_picks_the_most_recent_meal(db_with_user):
    db = db_with_user
    await _log_meal(db, meal_type="breakfast", items=[_item("Poha")])
    await _log_meal(db, meal_type="dinner", items=[_item("Khichdi")])
    result = await db.repeat_last_meal(UID)
    assert result.receipt.header.meal_type == "dinner"
    assert result.receipt.items[0].display_name == "Khichdi"


async def test_repeat_with_no_history_is_empty(db_with_user):
    result = await db_with_user.repeat_last_meal(UID)
    assert result.status is RepeatStatus.EMPTY
    assert result.receipt is None


async def test_repeat_never_sees_another_users_meal(db):
    await db.ensure_user(UID, "a", "A")
    await db.ensure_user(OTHER_UID, "b", "B")
    await _log_meal(db, user_id=OTHER_UID)
    result = await db.repeat_last_meal(UID)
    assert result.status is RepeatStatus.EMPTY


# ---------------------------------------------------------------------------
# repeat_last_meal — replay idempotency
# ---------------------------------------------------------------------------
async def test_redelivered_update_replays_instead_of_duplicating(db_with_user):
    db = db_with_user
    await _log_meal(db)
    source = MutationSource(update_id=555, chat_id=UID, message_id=1)

    first = await db.repeat_last_meal(UID, source)
    second = await db.repeat_last_meal(UID, source)

    assert first.status is RepeatStatus.CREATED
    assert second.status is RepeatStatus.REPLAYED
    assert second.receipt.header.meal_id == first.receipt.header.meal_id
    rows = await db.get_diet_logs(UID, *_today_range())
    assert len(rows) == 2  # the original plus exactly one repeat


async def test_two_distinct_updates_create_two_repeats(db_with_user):
    db = db_with_user
    await _log_meal(db)
    first = await db.repeat_last_meal(UID, MutationSource(update_id=1))
    second = await db.repeat_last_meal(UID, MutationSource(update_id=2))
    assert first.receipt.header.meal_id != second.receipt.header.meal_id


async def test_replay_after_undo_reports_removed_and_never_recreates(db_with_user):
    db = db_with_user
    await _log_meal(db)
    source = MutationSource(update_id=99)
    created = await db.repeat_last_meal(UID, source)
    await db.delete_meal_if_recent(UID, created.receipt.header.meal_id)

    replayed = await db.repeat_last_meal(UID, source)

    assert replayed.status is RepeatStatus.REPLAYED_REMOVED
    assert replayed.receipt is None
    rows = await db.get_diet_logs(UID, *_today_range())
    assert len(rows) == 1  # only the original meal is left


async def test_empty_repeat_stays_empty_on_replay(db_with_user):
    """The tombstone: a later meal must not change what an old update did."""
    db = db_with_user
    source = MutationSource(update_id=7)
    assert (await db.repeat_last_meal(UID, source)).status is RepeatStatus.EMPTY
    await _log_meal(db)
    assert (await db.repeat_last_meal(UID, source)).status is RepeatStatus.EMPTY
    rows = await db.get_diet_logs(UID, *_today_range())
    assert len(rows) == 1


async def test_repeat_receipt_is_owner_scoped(db):
    await db.ensure_user(UID, "a", "A")
    await db.ensure_user(OTHER_UID, "b", "B")
    await _log_meal(db, user_id=UID)
    source = MutationSource(update_id=42)
    await db.repeat_last_meal(UID, source)
    with pytest.raises(RuntimeError, match="owner mismatch"):
        await db.repeat_last_meal(OTHER_UID, source)


async def test_repeat_leaves_nothing_behind_when_the_child_insert_fails(
    db_with_user, monkeypatch
):
    db = db_with_user
    await _log_meal(db, items=[_item("Dal"), _item("Rice")])
    before = await db.get_diet_logs(UID, *_today_range())

    real_execute = db.conn.execute
    calls = {"n": 0}

    async def flaky(sql, *args, **kwargs):
        if "INSERT INTO diet_log_items" in sql:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn, "execute", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        await db.repeat_last_meal(UID, MutationSource(update_id=3))
    monkeypatch.undo()

    assert len(await db.get_diet_logs(UID, *_today_range())) == len(before)
    receipt_row = await db._query_one(
        "SELECT COUNT(*) AS n FROM mutation_receipts WHERE telegram_update_id = 3"
    )
    assert receipt_row["n"] == 0


# ---------------------------------------------------------------------------
# delete_meal_if_recent — targeted Undo
# ---------------------------------------------------------------------------
async def test_undo_deletes_the_exact_meal_and_cascades_items(db_with_user):
    db = db_with_user
    first = await _log_meal(db, items=[_item("Dal")])
    second = await _log_meal(db, items=[_item("Rice")])

    result = await db.delete_meal_if_recent(UID, first)

    assert result.status is UndoStatus.DELETED
    assert result.deleted_header.meal_id == first
    assert await db.get_diet_log_items(UID, first) == []
    # The newer meal is untouched — an intervening log never becomes the target.
    assert await db._get_meal_receipt_locked(UID, second) is not None


async def test_undo_is_idempotent(db_with_user):
    db = db_with_user
    meal_id = await _log_meal(db)
    assert (await db.delete_meal_if_recent(UID, meal_id)).status is UndoStatus.DELETED
    repeated = await db.delete_meal_if_recent(UID, meal_id)
    assert repeated.status is UndoStatus.ALREADY_REMOVED
    assert repeated.deleted_header is None


@pytest.mark.parametrize("meal_id", [999999, 0, -1])
async def test_undo_of_a_fabricated_id_is_already_removed(db_with_user, meal_id):
    result = await db_with_user.delete_meal_if_recent(UID, meal_id)
    assert result.status is UndoStatus.ALREADY_REMOVED


async def test_undo_cannot_delete_another_users_meal(db):
    await db.ensure_user(UID, "a", "A")
    await db.ensure_user(OTHER_UID, "b", "B")
    meal_id = await _log_meal(db, user_id=OTHER_UID)

    result = await db.delete_meal_if_recent(UID, meal_id)

    assert result.status is UndoStatus.ALREADY_REMOVED
    assert await db._get_meal_receipt_locked(OTHER_UID, meal_id) is not None


async def test_undo_expires_after_24h_but_not_at_exactly_24h(db_with_user):
    db = db_with_user
    meal_id = await _log_meal(db)
    logged = (await db._get_meal_receipt_locked(UID, meal_id)).header.logged_at_utc

    boundary = await db.delete_meal_if_recent(
        UID, meal_id, now_utc=logged + timedelta(hours=24)
    )
    assert boundary.status is UndoStatus.DELETED

    meal_id = await _log_meal(db)
    logged = (await db._get_meal_receipt_locked(UID, meal_id)).header.logged_at_utc
    expired = await db.delete_meal_if_recent(
        UID, meal_id, now_utc=logged + timedelta(hours=24, seconds=1)
    )
    assert expired.status is UndoStatus.EXPIRED
    assert await db._get_meal_receipt_locked(UID, meal_id) is not None


async def test_undo_refuses_a_meal_with_an_unreadable_timestamp(db_with_user):
    db = db_with_user
    meal_id = await _log_meal(db)
    async with db._write_operation():
        await db.conn.execute(
            "UPDATE diet_logs SET logged_at = NULL WHERE id = ?", (meal_id,)
        )
    result = await db.delete_meal_if_recent(UID, meal_id)
    assert result.status is UndoStatus.EXPIRED
    assert await db._get_meal_receipt_locked(UID, meal_id) is not None


async def test_undo_keeps_the_mutation_receipt(db_with_user):
    db = db_with_user
    await _log_meal(db)
    source = MutationSource(update_id=17)
    created = await db.repeat_last_meal(UID, source)
    await db.delete_meal_if_recent(UID, created.receipt.header.meal_id)
    row = await db._query_one(
        "SELECT COUNT(*) AS n FROM mutation_receipts WHERE telegram_update_id = 17"
    )
    assert row["n"] == 1


async def test_naive_now_utc_is_treated_as_utc(db_with_user):
    db = db_with_user
    meal_id = await _log_meal(db)
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.delete_meal_if_recent(UID, meal_id, now_utc=naive)
    assert result.status is UndoStatus.DELETED


# ---------------------------------------------------------------------------
# _write_operation(begin_immediate=True)
# ---------------------------------------------------------------------------
async def test_begin_immediate_refuses_an_already_open_transaction(db):
    """Fail closed rather than nest: an open transaction is a caller bug."""
    await db.ensure_user(UID, "a", "A")
    # Leave a transaction open outside the context manager (a stray DML).
    await db.conn.execute("UPDATE users SET first_name = 'x' WHERE user_id = ?", (UID,))
    assert db.conn.in_transaction

    with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"):
        async with db._write_operation(begin_immediate=True):
            pass  # pragma: no cover - the body must not run

    # The abort path rolled the stray transaction back rather than committing it.
    assert not db.conn.in_transaction
    row = await db._query_one("SELECT first_name FROM users WHERE user_id = ?", (UID,))
    assert row["first_name"] == "A"


def _today_range():
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=1), today + timedelta(days=1)


# ---------------------------------------------------------------------------
# Handlers: Home Repeat
# ---------------------------------------------------------------------------
def _update(text: str | None = None, update_id: int | None = 1):
    message = SimpleNamespace(text=text, message_id=10, reply_text=AsyncMock())
    return SimpleNamespace(
        update_id=update_id,
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _context(db=None, user_data=None):
    return SimpleNamespace(
        bot_data={"db": db}, user_data=user_data if user_data is not None else {}
    )


def _enable(monkeypatch):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "off")


async def test_home_repeat_logs_and_renders_a_receipt(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    await _log_meal(db, items=[_item("Dal"), _item("Rice", source_id=8)])
    update = _update("repeat", update_id=1001)

    await home.home_text_router(update, _context(db))

    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    text = reply.call_args.args[0]
    assert "Repeated" in text
    assert "Dal" in text and "Rice" in text
    markup = reply.call_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"mr_more_{to_base36(UID)}" in data
    assert any(d.startswith(f"mr_undo_{to_base36(UID)}_") for d in data)
    assert len(await db.get_diet_logs(UID, *_today_range())) == 2


async def test_home_repeat_with_no_history_says_so(db_with_user, monkeypatch):
    _enable(monkeypatch)
    update = _update("repeat", update_id=1002)
    await home.home_text_router(update, _context(db_with_user))
    assert "Nothing to repeat" in update.effective_message.reply_text.call_args.args[0]


async def test_home_repeat_is_replay_safe(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    await _log_meal(db)
    first, second = _update("repeat", update_id=77), _update("repeat", update_id=77)

    await home.home_text_router(first, _context(db))
    await home.home_text_router(second, _context(db))

    assert "Already repeated" in second.effective_message.reply_text.call_args.args[0]
    assert len(await db.get_diet_logs(UID, *_today_range())) == 2


async def test_home_repeat_is_blocked_by_an_active_flow(db_with_user, monkeypatch):
    _enable(monkeypatch)
    update = _update("repeat", update_id=1003)
    context = _context(db_with_user)
    activate_conversation(update, context, "gym")
    await home.home_text_router(update, context)
    assert "Finish this flow" in update.effective_message.reply_text.call_args.args[0]
    assert await db_with_user.get_diet_logs(UID, *_today_range()) == []


async def test_home_repeat_reports_a_failure_without_crashing(monkeypatch):
    _enable(monkeypatch)
    db = SimpleNamespace(
        ensure_user=AsyncMock(),
        repeat_last_meal=AsyncMock(side_effect=RuntimeError("db gone")),
    )
    update = _update("repeat", update_id=1004)
    await home.home_text_router(update, _context(db))
    assert "Couldn't repeat" in update.effective_message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# Handlers: receipt Undo
# ---------------------------------------------------------------------------
def _callback(data: str):
    message = SimpleNamespace(message_id=10, reply_text=AsyncMock())
    query = SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        update_id=2,
        callback_query=query,
        effective_message=message,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _undo_data(meal_id: int, owner: int = UID) -> str:
    return f"mr_undo_{to_base36(owner)}_{to_base36(meal_id)}"


async def test_receipt_undo_removes_the_targeted_meal(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_meal(db)
    newer = await _log_meal(db, items=[_item("Rice")])
    update = _callback(_undo_data(meal_id))

    await receipts.undo_from_receipt(update, _context(db))

    assert await db._get_meal_receipt_locked(UID, meal_id) is None
    assert await db._get_meal_receipt_locked(UID, newer) is not None
    update.callback_query.edit_message_reply_markup.assert_awaited_once()
    assert "Undone" in update.effective_message.reply_text.call_args.args[0]


async def test_receipt_undo_twice_is_a_no_op(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_meal(db)
    context = _context(db)
    await receipts.undo_from_receipt(_callback(_undo_data(meal_id)), context)
    second = _callback(_undo_data(meal_id))
    await receipts.undo_from_receipt(second, context)
    assert "Already removed" in second.effective_message.reply_text.call_args.args[0]


async def test_receipt_undo_works_during_another_flow(db_with_user, monkeypatch):
    """It targets a completed meal, so it never waits on conversation state."""
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_meal(db)
    update = _callback(_undo_data(meal_id))
    context = _context(db)
    activate_conversation(update, context, "study")

    await receipts.undo_from_receipt(update, context)

    assert await db._get_meal_receipt_locked(UID, meal_id) is None


async def test_receipt_undo_rejects_another_users_token(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_meal(db)
    update = _callback(_undo_data(meal_id, owner=OTHER_UID))

    await receipts.undo_from_receipt(update, _context(db))

    assert await db._get_meal_receipt_locked(UID, meal_id) is not None
    assert "another user" in update.callback_query.answer.call_args.args[0]


@pytest.mark.parametrize("data", ["mr_undo_ZZ_1", "mr_undo_01_1", "mr_undo__1"])
async def test_receipt_undo_fails_closed_on_a_malformed_token(
    db_with_user, monkeypatch, data
):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_meal(db)
    await receipts.undo_from_receipt(_callback(data), _context(db))
    assert await db._get_meal_receipt_locked(UID, meal_id) is not None


async def test_receipt_undo_makes_no_mutation_after_flag_off(db_with_user):
    db = db_with_user
    meal_id = await _log_meal(db)
    update = _callback(_undo_data(meal_id))

    await receipts.undo_from_receipt(update, _context(db))

    assert await db._get_meal_receipt_locked(UID, meal_id) is not None
    update.callback_query.edit_message_reply_markup.assert_awaited_once()


async def test_receipt_undo_of_an_expired_meal_keeps_it(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_meal(db)
    old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    async with db._write_operation():
        await db.conn.execute(
            "UPDATE diet_logs SET logged_at = ? WHERE id = ?", (old, meal_id)
        )
    update = _callback(_undo_data(meal_id))

    await receipts.undo_from_receipt(update, _context(db))

    assert await db._get_meal_receipt_locked(UID, meal_id) is not None
    assert "24h" in update.effective_message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# Handlers: receipt "Log another"
# ---------------------------------------------------------------------------
def _more_callback(owner: int = UID):
    update = _callback(f"mr_more_{to_base36(owner)}")
    update.effective_message.reply_text = AsyncMock(
        return_value=SimpleNamespace(message_id=11)
    )
    return update


async def test_log_another_opens_a_new_quick_meal(db_with_user, monkeypatch):
    _enable(monkeypatch)
    update = _more_callback()
    context = _context(db_with_user)

    state = await diet.diet_receipt_more_entry(update, context)

    assert state == diet.FOOD_CHOICE
    assert active_conversation_flow(context) == "diet"
    assert context.user_data["diet_entry_mode"] == DietEntryMode.QUICK


async def test_log_another_yields_to_an_active_flow(db_with_user, monkeypatch):
    _enable(monkeypatch)
    update = _more_callback()
    context = _context(db_with_user)
    activate_conversation(update, context, "gym")

    state = await diet.diet_receipt_more_entry(update, context)

    assert state == ConversationHandler.END
    assert active_conversation_flow(context) == "gym"
    assert "already in" in update.effective_message.reply_text.call_args.args[0]


async def test_log_another_after_flag_off_gives_command_guidance(db_with_user):
    update = _more_callback()
    context = _context(db_with_user)

    state = await diet.diet_receipt_more_entry(update, context)

    assert state == ConversationHandler.END
    assert active_conversation_flow(context) is None
    assert "/diet" in update.effective_message.reply_text.call_args.args[0]


async def test_log_another_rejects_another_users_token(db_with_user, monkeypatch):
    _enable(monkeypatch)
    update = _more_callback(owner=OTHER_UID)
    context = _context(db_with_user)

    state = await diet.diet_receipt_more_entry(update, context)

    assert state == ConversationHandler.END
    assert active_conversation_flow(context) is None
    assert "another user" in update.callback_query.answer.call_args.args[0]


# ---------------------------------------------------------------------------
# Receipt rendering
# ---------------------------------------------------------------------------
async def test_receipt_text_escapes_and_bounds_item_names(db_with_user):
    db = db_with_user
    await _log_meal(db, items=[_item("<b>Dal</b> " + "x" * 200)])
    receipt = (await db.repeat_last_meal(UID)).receipt
    text = receipts.format_meal_receipt(receipt, "🔁 <b>Repeated</b>")
    assert "&lt;b&gt;Dal&lt;/b&gt;" in text
    assert "…" in text


async def test_receipt_text_collapses_a_long_item_list(db_with_user):
    db = db_with_user
    await _log_meal(
        db, items=[_item(f"Item {n}", source_id=n) for n in range(1, 10)]
    )
    receipt = (await db.repeat_last_meal(UID)).receipt
    text = receipts.format_meal_receipt(receipt, "🔁 <b>Repeated</b>")
    assert "…and 3 more" in text


async def test_receipt_text_omits_unknown_nutrients(db_with_user):
    db = db_with_user
    await _log_meal(
        db, items=[_item("Mystery", calories=None, protein_g=None)]
    )
    receipt = (await db.repeat_last_meal(UID)).receipt
    text = receipts.format_meal_receipt(receipt, "🔁 <b>Repeated</b>")
    assert "cal" not in text
    assert "C 40 g" in text
