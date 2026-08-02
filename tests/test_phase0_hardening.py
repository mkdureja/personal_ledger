"""Phase 0 foundation-hardening regressions.

Each test pins a defect the Codex reviews reproduced against the v8 baseline so a
later change cannot silently reintroduce it.
"""

from __future__ import annotations

import pytest

from bot import migrations
from bot.database import DatabaseManager
from bot.nutrition import MAX_MEAL_ITEMS, NutritionError

pytestmark = pytest.mark.asyncio


async def _fresh_manager() -> DatabaseManager:
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    return mgr


def _item(name, cal, p, c, f, *, source_type="food", source_id=1):
    return {
        "source_type": source_type,
        "source_id": source_id,
        "display_name": name,
        "entered_amount": 1.0,
        "entered_unit": "serving",
        "resolved_base_amount": 100.0,
        "resolved_base_unit": "g",
        "calories": cal,
        "protein_g": p,
        "carbs_g": c,
        "fat_g": f,
    }


# --- Item 5: aggregate bounds + item cap at the DB/service boundary -----------
async def test_aggregate_calories_over_limit_is_rejected(db_with_user, user_id):
    """Two individually valid items must not sum past the per-meal calorie bound."""
    items = [
        _item("huge a", 60000, 1.0, 1.0, 1.0, source_id=1),
        _item("huge b", 60000, 1.0, 1.0, 1.0, source_id=2),
    ]
    with pytest.raises(NutritionError):
        await db_with_user.log_diet_with_items(user_id, "lunch", items)
    # Nothing partial is persisted.
    from bot.config import today_local

    rows = await db_with_user.get_diet_logs(user_id, today_local(), today_local())
    assert rows == []


async def test_aggregate_macro_over_limit_is_rejected(db_with_user, user_id):
    items = [
        _item("p a", 10, 600.0, 1.0, 1.0, source_id=1),
        _item("p b", 10, 600.0, 1.0, 1.0, source_id=2),
    ]
    with pytest.raises(NutritionError):
        await db_with_user.log_diet_with_items(user_id, "lunch", items)


async def test_meal_item_count_is_capped(db_with_user, user_id):
    items = [_item(f"f{i}", 10, 1.0, 1.0, 1.0, source_id=i) for i in range(MAX_MEAL_ITEMS + 1)]
    with pytest.raises(ValueError):
        await db_with_user.log_diet_with_items(user_id, "lunch", items)


async def test_meal_at_item_cap_is_accepted(db_with_user, user_id):
    items = [_item(f"f{i}", 10, 1.0, 1.0, 1.0, source_id=i) for i in range(MAX_MEAL_ITEMS)]
    header_id = await db_with_user.log_diet_with_items(user_id, "lunch", items)
    children = await db_with_user.get_diet_log_items(user_id, header_id)
    assert len(children) == MAX_MEAL_ITEMS


# --- Item 4a: column-aware rebuild preserves provenance on recovery replay ----
async def test_version_reset_replay_preserves_item_provenance():
    """A current-shaped DB re-stamped to v0 must keep source_provider/revision
    when the v8 rebuild replays (the fields existed before the replay)."""
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        await mgr.ensure_user(1001, "a", "A")
        await mgr.conn.execute(
            "INSERT INTO diet_logs (user_id, meal_type, food_items, calories) "
            "VALUES (1001, 'lunch', 'Apple', 95)"
        )
        row = await mgr._query_one(
            "SELECT id FROM diet_logs WHERE user_id = 1001"
        )
        dlid = row["id"]
        await mgr.conn.execute(
            "INSERT INTO diet_log_items "
            "(user_id, diet_log_id, item_order, source_type, source_id, "
            " source_provider, source_revision, display_name, calories) "
            "VALUES (1001, ?, 0, 'catalog', 5, 'curated', '2026.1', 'Apple', 95)",
            (dlid,),
        )
        await mgr.conn.commit()

        # Pretend this current-shaped DB predates versioning and replay migrations.
        await mgr.conn.execute("PRAGMA user_version = 0")
        await mgr.conn.commit()
        await mgr.init_db()

        item = await mgr._query_one(
            "SELECT source_provider, source_revision FROM diet_log_items "
            "WHERE user_id = 1001 AND diet_log_id = ?",
            (dlid,),
        )
        assert item["source_provider"] == "curated"
        assert item["source_revision"] == "2026.1"
    finally:
        await mgr.close()


# --- Item 4b: startup verifier fails closed on a mis-stamped schema -----------
async def test_empty_db_stamped_latest_fails_verification():
    """A blank database wrongly stamped at the latest version must fail closed
    at startup rather than serve traffic against a missing schema."""
    mgr = await _fresh_manager()
    try:
        await mgr.conn.execute(f"PRAGMA user_version = {migrations.LATEST_VERSION}")
        await mgr.conn.commit()
        with pytest.raises(migrations.SchemaVerificationError):
            await mgr.init_db()
    finally:
        await mgr.close()


async def test_latest_db_missing_a_table_fails_verification():
    """A correctly-migrated DB that later loses a required table fails closed."""
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        await mgr.conn.execute("DROP TABLE catalog_portions")
        await mgr.conn.commit()
        with pytest.raises(migrations.SchemaVerificationError):
            await migrations.verify_current_schema(mgr.conn)
    finally:
        await mgr.close()


# --- Item 8: cross-owner source references fail closed at the DB boundary -----
async def test_cross_owner_food_reference_is_rejected(db, user_id):
    from bot.config import today_local

    other = user_id + 1
    await db.ensure_user(user_id, "a", "A")
    await db.ensure_user(other, "b", "B")
    food = (await db.save_food(other, "apple", "g", 100, calories=52, protein_g=1, carbs_g=2, fat_g=3))["food"]
    item = _item("apple", 52, 0.3, 14.0, 0.2, source_type="food", source_id=food["id"])

    with pytest.raises(ValueError):
        await db.log_diet_with_items(user_id, "lunch", [item])
    # No partial meal was written for the acting user.
    assert await db.get_diet_logs(user_id, today_local(), today_local()) == []


async def test_cross_owner_preference_is_rejected(db, user_id):
    other = user_id + 1
    await db.ensure_user(user_id, "a", "A")
    await db.ensure_user(other, "b", "B")
    food = (await db.save_food(other, "apple", "g", 100, calories=52, protein_g=1, carbs_g=2, fat_g=3))["food"]

    with pytest.raises(ValueError):
        await db.set_food_preference(user_id, "food", food["id"], is_pinned=True)


async def test_own_food_reference_is_allowed(db_with_user, user_id):
    food = (
        await db_with_user.save_food(user_id, "apple", "g", 100, calories=52, protein_g=1, carbs_g=2, fat_g=3)
    )["food"]
    item = _item("apple", 52, 0.3, 14.0, 0.2, source_type="food", source_id=food["id"])
    header_id = await db_with_user.log_diet_with_items(user_id, "lunch", [item])
    children = await db_with_user.get_diet_log_items(user_id, header_id)
    assert children[0]["source_id"] == food["id"]


# --- Item 10: /suggestions reset clears pins/hides but is not a forget --------
async def test_suggestions_reset_clears_prefs_but_keeps_learned_history(
    db_with_user, user_id
):
    food = (
        await db_with_user.save_food(user_id, "apple", "g", 100, calories=52, protein_g=1, carbs_g=2, fat_g=3)
    )["food"]
    await db_with_user.set_food_preference(user_id, "food", food["id"], is_pinned=True)
    item = _item("apple", 52, 0.3, 14.0, 0.2, source_type="food", source_id=food["id"])
    await db_with_user.log_diet_with_items(user_id, "lunch", [item])

    cleared = await db_with_user.reset_food_preferences(user_id)

    # The pin is cleared...
    assert cleared == 1
    assert await db_with_user.get_food_preference(user_id, "food", food["id"]) is None
    # ...but learned history survives: reset is not a learning "forget" (that is
    # the deferred Phase 2 watermark).
    stats = await db_with_user.get_diet_item_stats(user_id, "lunch")
    assert ("food", food["id"]) in stats
    assert stats[("food", food["id"])]["total_uses"] == 1


# --- Item 9: seed is a reconciled snapshot, not an upsert-only refresh --------
async def test_reseed_deactivates_removed_foods_and_stale_children(db_with_user):
    from bot.catalog_seed import CATALOG_FOODS

    await db_with_user.seed_catalog(CATALOG_FOODS)

    # A newer manifest that keeps only one food and strips its children.
    kept = next(e for e in CATALOG_FOODS if e.get("portions"))
    reduced = {**kept, "portions": (), "aliases": ()}
    await db_with_user.seed_catalog([reduced])

    # Exactly one food stays active; the rest are deactivated, not deleted.
    active = await db_with_user.conn.execute(
        "SELECT COUNT(*) AS n FROM catalog_foods WHERE is_active = 1"
    )
    assert (await active.fetchone())["n"] == 1
    total = await db_with_user.conn.execute(
        "SELECT COUNT(*) AS n FROM catalog_foods"
    )
    assert (await total.fetchone())["n"] == len(CATALOG_FOODS)

    # The retained food's stale portions were replaced away.
    row = await db_with_user._query_one(
        "SELECT id FROM catalog_foods WHERE provider_food_id = ?",
        (kept["provider_food_id"],),
    )
    portions = await db_with_user.conn.execute(
        "SELECT COUNT(*) AS n FROM catalog_portions WHERE catalog_food_id = ?",
        (row["id"],),
    )
    assert (await portions.fetchone())["n"] == 0

    # A deactivated food is no longer searchable.
    removed = next(e for e in CATALOG_FOODS if e is not kept)
    hits = await db_with_user.search_catalog(removed["display_name"])
    assert all(
        removed["display_name"].lower() not in h["name"].lower() for h in hits
    )


# --- Callback-handler harness (mirrors tests/test_diet_tap.py) ----------------
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from telegram.constants import ChatType  # noqa: E402
from telegram.ext import ConversationHandler  # noqa: E402

from bot.handlers import diet  # noqa: E402
from bot.handlers.common import (  # noqa: E402
    activate_conversation,
    active_conversation_flow,
)

USER = 123456789  # matches conftest ALLOWED_USER_IDS / user_id fixture


def _query(data: str, *, message_id: int = 100, sent_id: int = 777):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=sent_id)),
        ),
        edit_message_reply_markup=AsyncMock(),
    )


def _cb_update(query, *, user_id: int = USER):
    return SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
    )


def _msg_update(text: str, *, user_id: int = USER):
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=777)),
    )
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
    )


def _context(db, user_data=None):
    return SimpleNamespace(bot_data={"db": db}, user_data=user_data or {})


# --- Item 3: "Add another" -> "Type it" must not drop the draft ---------------
async def test_typed_item_joins_existing_draft_instead_of_replacing_it():
    db = SimpleNamespace(
        log_diet=AsyncMock(),
        log_diet_with_items=AsyncMock(return_value=1),
    )
    existing = _item("apple", 95, 0.5, 25.0, 0.3)
    context = _context(
        db,
        {
            "diet_meal_type": "lunch",
            "diet_items": [existing],
            "diet_food_items": "toast",
            "diet_calories": 100,
        },
    )
    update = _msg_update("25 80 15")  # protein carbs fat

    result = await diet.receive_macros(update, context)

    assert result == diet.LOG_ANOTHER
    db.log_diet.assert_not_awaited()  # legacy single-save path not taken
    db.log_diet_with_items.assert_awaited_once()
    saved = db.log_diet_with_items.await_args.args[2]
    assert len(saved) == 2
    assert saved[0] == existing  # previously tapped item preserved
    assert saved[1]["source_type"] == "freetext"
    assert saved[1]["display_name"] == "toast"
    assert saved[1]["calories"] == 100
    assert (saved[1]["protein_g"], saved[1]["carbs_g"], saved[1]["fat_g"]) == (
        25.0,
        80.0,
        15.0,
    )


# --- Items 1-2: guided food/recipe reference writes structured history --------
async def test_guided_food_reference_writes_structured_child(db_with_user, user_id):
    from bot.config import today_local

    food = (
        await db_with_user.save_food(user_id, "apple", "g", 100, calories=52, protein_g=1, carbs_g=2, fat_g=3)
    )["food"]
    context = _context(db_with_user, {"diet_meal_type": "snack"})
    update = _msg_update("food:apple 100g")
    activate_conversation(update, context, "diet")

    result = await diet.receive_food_items(update, context)

    assert result == diet.LOG_ANOTHER
    row = (
        await db_with_user.get_diet_logs(user_id, today_local(), today_local())
    )[-1]
    children = await db_with_user.get_diet_log_items(user_id, row["id"])
    assert len(children) == 1
    assert children[0]["source_type"] == "food"
    assert children[0]["source_id"] == food["id"]


# --- Item 6: a transient Save failure must remain retryable -------------------
async def test_save_retries_after_transient_write_failure():
    db = SimpleNamespace(
        log_diet_with_items=AsyncMock(side_effect=[RuntimeError("db down"), 1]),
    )
    item = _item("220 g apple", 114, 0.66, 30.8, 0.44)
    context = _context(
        db,
        {"diet_meal_type": "snack", "diet_items": [item], "diet_ui_message_id": 100},
    )
    update = _cb_update(_query(f"dsave_{USER}", message_id=100))
    activate_conversation(update, context, "diet")

    result = await diet.save_item(update, context)

    assert result == diet.CONFIRM_ITEM  # kept in preview state
    assert context.user_data["diet_items"] == [item]  # draft preserved
    assert active_conversation_flow(context) == "diet"  # flow not stranded
    assert context.user_data["diet_ui_message_id"] == 777  # controls re-rendered

    retry = _cb_update(_query(f"dsave_{USER}", message_id=777))
    result2 = await diet.save_item(retry, context)

    assert result2 == diet.LOG_ANOTHER
    assert db.log_diet_with_items.await_count == 2
    assert "diet_items" not in context.user_data


# --- Item 7: a stale inline Cancel must not end a newer draft ------------------
async def test_stale_inline_cancel_leaves_active_draft():
    db = SimpleNamespace()
    item = _item("apple", 95, 0.5, 25.0, 0.3)
    context = _context(
        db,
        {"diet_meal_type": "lunch", "diet_items": [item], "diet_ui_message_id": 100},
    )
    update = _cb_update(_query(f"dcancel_{USER}", message_id=55))  # stale, not 100
    activate_conversation(update, context, "diet")

    result = await diet.cancel_diet_callback(update, context)

    assert result is None  # fallback returns None -> keep current state
    assert active_conversation_flow(context) == "diet"  # NOT finished
    assert context.user_data["diet_items"] == [item]  # draft intact


async def test_current_inline_cancel_still_cancels():
    db = SimpleNamespace()
    context = _context(
        db,
        {"diet_meal_type": "lunch", "diet_items": [_item("a", 1, 1, 1, 1)],
         "diet_ui_message_id": 100},
    )
    update = _cb_update(_query(f"dcancel_{USER}", message_id=100))  # current UI
    activate_conversation(update, context, "diet")

    result = await diet.cancel_diet_callback(update, context)

    assert result == ConversationHandler.END
    assert active_conversation_flow(context) is None  # flow finished
