"""Phase 1b unit B3: "Log again at today's values".

The distinction from Repeat is the whole feature: Repeat copies the old numbers
verbatim, this re-prices the same items from the sources as they are *now*. So
these tests care about three things — that live edits are picked up, that an
item which can no longer be resolved forces an explicit human decision instead
of a silent fallback, and that a change between preview and Save is caught.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.database import MutationSource
from bot.handlers import diet, receipts
from bot.meal_models import (
    CurrentCommitStatus,
    CurrentValueDecision,
    CurrentValueIssueCode,
)

UID = next(iter(ALLOWED_USER_IDS))
OTHER_UID = UID + 1

pytestmark = pytest.mark.asyncio


def _today_range():
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=1), today + timedelta(days=1)


@pytest.fixture
async def food(db_with_user):
    saved = await db_with_user.save_food(
        UID, "Dal", "g", 100, calories=100, protein_g=5, carbs_g=15, fat_g=1
    )
    return saved["food"]["id"]


async def _log_food_meal(db, food_id, amount="200", unit="g", meal_type="lunch"):
    entry = await db.resolve_quantity(UID, "food", food_id, [amount, unit])
    return await db.log_diet_with_items(UID, meal_type, [entry.as_item()])


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
async def test_preview_reprices_from_the_edited_food(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)  # 200 g at 100 kcal/100 g = 200
    await db.save_food(UID, "Dal", "g", 100, calories=150, protein_g=1, carbs_g=2, fat_g=3)

    preview = await db.get_current_value_preview(UID, meal_id)

    assert preview.original_totals.calories == 200
    assert preview.proposed_totals.calories == 300
    assert preview.delta.calories == 100
    assert preview.can_save is True
    assert preview.items[0].decision is CurrentValueDecision.RESOLVE_CURRENT


async def test_preview_keeps_item_order_and_duplicates(db_with_user, food):
    db = db_with_user
    entry = await db.resolve_quantity(UID, "food", food, ["100", "g"])
    meal_id = await db.log_diet_with_items(
        UID,
        "lunch",
        [entry.as_item(), {"display_name": "Note", "source_type": "freetext", "calories": 100, "protein_g": 1, "carbs_g": 2, "fat_g": 3},
         entry.as_item()],
    )
    preview = await db.get_current_value_preview(UID, meal_id)
    assert [p.original.display_name for p in preview.items] == [
        "100 g Dal",
        "Note",
        "100 g Dal",
    ]
    assert [p.persisted_item_order for p in preview.items] == [0, 1, 2]


async def test_freetext_items_carry_through_unchanged(db_with_user):
    db = db_with_user
    meal_id = await db.log_diet_with_items(
        UID,
        "snack",
        [{"display_name": "Two rotis", "source_type": "freetext", "calories": 180, "protein_g": 1, "carbs_g": 2, "fat_g": 3}],
    )
    preview = await db.get_current_value_preview(UID, meal_id)
    assert preview.items[0].issue is None
    assert preview.items[0].proposed.calories == 180
    assert preview.can_save is True


async def test_a_deleted_food_becomes_an_unresolved_issue(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    await db.archive_food(UID, food)

    preview = await db.get_current_value_preview(UID, meal_id)

    assert preview.items[0].issue.code is CurrentValueIssueCode.SOURCE_MISSING
    assert preview.items[0].decision is CurrentValueDecision.UNRESOLVED
    assert preview.can_save is False


async def test_keep_original_resolves_the_issue_with_the_old_snapshot(
    db_with_user, food
):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    child_id = (await db.get_current_value_preview(UID, meal_id)).items[
        0
    ].source_child_id
    await db.archive_food(UID, food)

    preview = await db.get_current_value_preview(
        UID, meal_id, {child_id: CurrentValueDecision.KEEP_ORIGINAL}
    )

    assert preview.can_save is True
    assert preview.items[0].proposed.calories == 200
    # The issue code is retained for display, but it is no longer a blocker.
    assert preview.items[0].issue.code is CurrentValueIssueCode.SOURCE_MISSING


async def test_removing_the_only_item_disables_saving(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    child_id = (await db.get_current_value_preview(UID, meal_id)).items[
        0
    ].source_child_id
    await db.archive_food(UID, food)

    preview = await db.get_current_value_preview(
        UID, meal_id, {child_id: CurrentValueDecision.REMOVE}
    )

    assert preview.can_save is False
    assert preview.proposed_totals.calories is None


async def test_an_item_with_no_recorded_amount_is_an_issue(db_with_user, food):
    db = db_with_user
    meal_id = await db.log_diet_with_items(
        UID,
        "lunch",
        [{
            "display_name": "Dal",
            "source_type": "food",
            "source_id": food,
            "calories": 90,
            "protein_g": 1,
            "carbs_g": 2,
            "fat_g": 3,
        }],
    )
    preview = await db.get_current_value_preview(UID, meal_id)
    assert preview.items[0].issue.code is CurrentValueIssueCode.QUANTITY_MISSING


async def test_preview_totals_cover_every_macro(db_with_user):
    """A replay preview totals all four nutrients, not just calories.

    This replaced ``test_unknown_nutrients_stay_unknown``, whose subject — a
    stored ``None`` propagating into the proposed totals — can no longer exist
    now that nutrition is mandatory. What is worth pinning instead is that the
    numbers the user is asked to accept are complete.
    """
    db = db_with_user
    meal_id = await db.log_diet_with_items(
        UID,
        "snack",
        [{
            "display_name": "Mystery",
            "source_type": "freetext",
            "calories": 100,
            "protein_g": 1,
            "carbs_g": 2,
            "fat_g": 3,
        }],
    )
    preview = await db.get_current_value_preview(UID, meal_id)
    assert preview.proposed_totals.calories == 100
    assert preview.proposed_totals.protein_g == pytest.approx(1)
    assert preview.proposed_totals.carbs_g == pytest.approx(2)
    assert preview.proposed_totals.fat_g == pytest.approx(3)
    # Nothing was re-resolved, so the replay is a no-op against the snapshot.
    assert preview.delta.calories == 0


async def test_preview_of_another_users_meal_is_none(db, food):
    await db.ensure_user(OTHER_UID, "b", "B")
    meal_id = await _log_food_meal(db, food)
    assert await db.get_current_value_preview(OTHER_UID, meal_id) is None


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------
async def test_commit_writes_the_repriced_meal(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    await db.save_food(UID, "Dal", "g", 100, calories=150, protein_g=1, carbs_g=2, fat_g=3)
    preview = await db.get_current_value_preview(UID, meal_id)

    result = await db.commit_current_value_meal(UID, meal_id, {}, preview.digest)

    assert result.status is CurrentCommitStatus.CREATED
    assert result.receipt.header.nutrients.calories == 300
    assert result.receipt.header.meal_type == "lunch"
    assert len(await db.get_diet_logs(UID, *_today_range())) == 2


async def test_commit_refuses_a_preview_that_no_longer_matches(db_with_user, food):
    """The value changed between preview and Save — ask, do not just log it."""
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    preview = await db.get_current_value_preview(UID, meal_id)
    await db.save_food(UID, "Dal", "g", 100, calories=999, protein_g=1, carbs_g=2, fat_g=3)

    result = await db.commit_current_value_meal(UID, meal_id, {}, preview.digest)

    assert result.status is CurrentCommitStatus.REVIEW_REQUIRED
    assert result.preview.proposed_totals.calories == 1998
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1


async def test_commit_refuses_while_an_issue_is_unresolved(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    preview = await db.get_current_value_preview(UID, meal_id)
    await db.archive_food(UID, food)

    result = await db.commit_current_value_meal(UID, meal_id, {}, preview.digest)

    assert result.status is CurrentCommitStatus.REVIEW_REQUIRED
    assert result.preview.can_save is False


async def test_commit_applies_keep_and_remove_decisions(db_with_user, food):
    db = db_with_user
    entry = await db.resolve_quantity(UID, "food", food, ["100", "g"])
    other = await db.save_food(UID, "Rice", "g", 100, calories=130, protein_g=1, carbs_g=2, fat_g=3)
    other_entry = await db.resolve_quantity(
        UID, "food", other["food"]["id"], ["100", "g"]
    )
    meal_id = await db.log_diet_with_items(
        UID, "dinner", [entry.as_item(), other_entry.as_item()]
    )
    children = [
        p.source_child_id
        for p in (await db.get_current_value_preview(UID, meal_id)).items
    ]
    await db.archive_food(UID, food)
    decisions = {children[0]: CurrentValueDecision.REMOVE}
    preview = await db.get_current_value_preview(UID, meal_id, decisions)

    result = await db.commit_current_value_meal(
        UID, meal_id, decisions, preview.digest
    )

    assert result.status is CurrentCommitStatus.CREATED
    assert [i.display_name for i in result.receipt.items] == ["100 g Rice"]
    assert result.receipt.items[0].item_order == 0


async def test_commit_of_a_deleted_source_meal_reports_it(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    preview = await db.get_current_value_preview(UID, meal_id)
    await db.delete_meal_if_recent(UID, meal_id)

    result = await db.commit_current_value_meal(UID, meal_id, {}, preview.digest)

    assert result.status is CurrentCommitStatus.SOURCE_MEAL_REMOVED


async def test_commit_replay_does_not_duplicate(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    preview = await db.get_current_value_preview(UID, meal_id)
    source = MutationSource(update_id=7777)

    first = await db.commit_current_value_meal(
        UID, meal_id, {}, preview.digest, source=source
    )
    second = await db.commit_current_value_meal(
        UID, meal_id, {}, preview.digest, source=source
    )

    assert first.status is CurrentCommitStatus.CREATED
    assert second.status is CurrentCommitStatus.REPLAYED
    assert second.receipt.header.meal_id == first.receipt.header.meal_id
    assert len(await db.get_diet_logs(UID, *_today_range())) == 2


async def test_commit_replay_after_undo_is_not_recreated(db_with_user, food):
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    preview = await db.get_current_value_preview(UID, meal_id)
    source = MutationSource(update_id=7788)
    created = await db.commit_current_value_meal(
        UID, meal_id, {}, preview.digest, source=source
    )
    await db.delete_meal_if_recent(UID, created.receipt.header.meal_id)

    replayed = await db.commit_current_value_meal(
        UID, meal_id, {}, preview.digest, source=source
    )

    assert replayed.status is CurrentCommitStatus.REPLAYED_REMOVED
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _enable(monkeypatch):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "off")


def _callback(data: str, message_id: int = 20):
    sent = SimpleNamespace(message_id=message_id + 1)
    message = SimpleNamespace(
        message_id=message_id, reply_text=AsyncMock(return_value=sent)
    )
    query = SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        update_id=800,
        callback_query=query,
        effective_message=message,
        message=None,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _context(db, **extra):
    user_data = {}
    user_data.update(extra)
    return SimpleNamespace(bot_data={"db": db}, user_data=user_data, args=[])


def _last_text(update):
    return update.effective_message.reply_text.call_args.args[0]


def _last_markup(update):
    return update.effective_message.reply_text.call_args.kwargs["reply_markup"]


async def test_receipt_offers_current_values_only_for_structured_meals(
    db_with_user, food
):
    db = db_with_user
    structured = await db._get_meal_receipt_locked(UID, await _log_food_meal(db, food))
    freetext_id = await db.log_diet_with_items(
        UID, "snack", [{"display_name": "Toast", "source_type": "freetext", "calories": 100, "protein_g": 1, "carbs_g": 2, "fat_g": 3}]
    )
    freetext = await db._get_meal_receipt_locked(UID, freetext_id)

    assert receipts.can_use_current_values(structured) is True
    assert receipts.can_use_current_values(freetext) is False


async def test_entry_opens_the_review_with_the_delta(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    await db.save_food(UID, "Dal", "g", 100, calories=150, protein_g=1, carbs_g=2, fat_g=3)
    update = _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}")
    context = _context(db)

    state = await diet.current_values_entry(update, context)

    assert state == diet.CURRENT_VALUES_REVIEW
    text = _last_text(update)
    assert "200 → <b>300</b> kcal" in text
    assert context.user_data["diet_current_source_meal_id"] == meal_id


async def test_entry_rejects_another_users_receipt(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    meal_id = await _log_food_meal(db_with_user, food)
    update = _callback(f"mr_current_{to_base36(OTHER_UID)}_{to_base36(meal_id)}")

    state = await diet.current_values_entry(update, _context(db_with_user))

    assert state == ConversationHandler.END
    assert "another user" in update.callback_query.answer.call_args.args[0]


async def test_entry_after_flag_off_makes_no_review(db_with_user, food):
    meal_id = await _log_food_meal(db_with_user, food)
    update = _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}")
    context = _context(db_with_user)

    state = await diet.current_values_entry(update, context)

    assert state == ConversationHandler.END
    assert "/diet" in _last_text(update)
    assert "diet_current_source_meal_id" not in context.user_data


async def test_review_blocks_save_until_each_issue_is_answered(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    await db.archive_food(UID, food)
    entry_update = _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}")
    context = _context(db)
    await diet.current_values_entry(entry_update, context)

    data = [
        b.callback_data
        for row in _last_markup(entry_update).inline_keyboard
        for b in row
    ]
    assert not any(d.startswith("cv_save_") for d in data)
    assert any(d.startswith("cv_keep_") for d in data)
    assert "gone" in _last_text(entry_update)


async def test_keeping_an_item_enables_save_and_logs_the_snapshot(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    await db.archive_food(UID, food)
    context = _context(db)
    entry_update = _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}")
    await diet.current_values_entry(entry_update, context)
    child_id = context.user_data["diet_current_child_ids"][0]

    revision = context.user_data["diet_ui_revision"]
    context.user_data["diet_ui_message_id"] = 21
    keep = _callback(
        f"cv_keep_{to_base36(UID)}_{to_base36(revision)}_{to_base36(child_id)}",
        message_id=21,
    )
    state = await diet.current_values_keep(keep, context)
    assert state == diet.CURRENT_VALUES_REVIEW
    assert "kept as originally logged" in _last_text(keep)

    revision = context.user_data["diet_ui_revision"]
    context.user_data["diet_ui_message_id"] = 23
    save = _callback(
        f"cv_save_{to_base36(UID)}_{to_base36(revision)}", message_id=23
    )
    state = await diet.current_values_save(save, context)

    assert state == ConversationHandler.END
    assert len(await db.get_diet_logs(UID, *_today_range())) == 2
    assert "today's values" in _last_text(save)


async def test_save_reopens_the_review_when_values_moved(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    context = _context(db)
    entry_update = _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}")
    await diet.current_values_entry(entry_update, context)

    # Somebody edits the food after the preview was rendered.
    await db.save_food(UID, "Dal", "g", 100, calories=400, protein_g=1, carbs_g=2, fat_g=3)

    revision = context.user_data["diet_ui_revision"]
    context.user_data["diet_ui_message_id"] = 21
    save = _callback(
        f"cv_save_{to_base36(UID)}_{to_base36(revision)}", message_id=21
    )
    state = await diet.current_values_save(save, context)

    assert state == diet.CURRENT_VALUES_REVIEW
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1
    assert "800" in _last_text(save)


async def test_a_stale_review_tap_changes_nothing(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    context = _context(db)
    await diet.current_values_entry(
        _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}"), context
    )
    stale_rev = context.user_data["diet_ui_revision"] - 1
    context.user_data["diet_ui_message_id"] = 21
    save = _callback(
        f"cv_save_{to_base36(UID)}_{to_base36(stale_rev)}", message_id=21
    )

    state = await diet.current_values_save(save, context)

    assert state == diet.CURRENT_VALUES_REVIEW
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1


async def test_cancel_logs_nothing(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    context = _context(db)
    await diet.current_values_entry(
        _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}"), context
    )
    revision = context.user_data["diet_ui_revision"]
    context.user_data["diet_ui_message_id"] = 21
    cancel = _callback(
        f"cv_cancel_{to_base36(UID)}_{to_base36(revision)}", message_id=21
    )

    state = await diet.current_values_cancel(cancel, context)

    assert state == ConversationHandler.END
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1


async def test_a_foreign_child_id_is_ignored(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    meal_id = await _log_food_meal(db, food)
    await db.archive_food(UID, food)
    context = _context(db)
    await diet.current_values_entry(
        _callback(f"mr_current_{to_base36(UID)}_{to_base36(meal_id)}"), context
    )
    revision = context.user_data["diet_ui_revision"]
    context.user_data["diet_ui_message_id"] = 21
    bogus = _callback(
        f"cv_keep_{to_base36(UID)}_{to_base36(revision)}_{to_base36(99999)}",
        message_id=21,
    )

    state = await diet.current_values_keep(bogus, context)

    assert state == diet.CURRENT_VALUES_REVIEW
    assert context.user_data["diet_current_decisions"] == {}
