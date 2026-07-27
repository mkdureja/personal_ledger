"""Versioned, non-destructive migration behavior (implementation_plan Phase 1).

Covers: fresh DB reaches the latest version; every legacy fixture converges on the
same final schema; a current-shaped version-0 DB is recognized without mutation; a
two-user fixture keeps every row with its owner; a forced mid-migration failure
rolls back; normalized habit collisions stop with no change; re-running init is a
no-op; and foreign_key_check passes after each fixture.
"""

from __future__ import annotations

import pytest

from bot import migrations
from bot.database import DatabaseManager
from bot.migrations import LATEST_VERSION, MigrationCollisionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _fresh_manager() -> DatabaseManager:
    """A connected, empty in-memory database at user_version 0 (pre-migration)."""
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    return mgr


async def _schema_objects(conn) -> set[tuple[str, str]]:
    """Named tables/indexes/triggers, excluding SQLite-internal objects."""
    cursor = await conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return {(row["type"], row["name"]) for row in await cursor.fetchall()}


async def _columns(conn, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in await cursor.fetchall()}


async def _seed_legacy_schema(conn) -> None:
    """Create a pre-macro / pre-habit-key legacy shape at user_version 0.

    ``diet_logs`` lacks the macro columns, ``habits`` lacks ``name_key`` and uses
    the old exact-name unique index ``idx_habits_active``.
    """
    await conn.execute(
        """
        CREATE TABLE users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE diet_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            meal_type   TEXT NOT NULL,
            food_items  TEXT NOT NULL,
            calories    INTEGER,
            logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE habits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            habit_name  TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active   INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE UNIQUE INDEX idx_habits_active "
        "ON habits(user_id, habit_name) WHERE is_active = 1"
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Fresh database
# ---------------------------------------------------------------------------
async def test_fresh_database_reaches_latest_version():
    mgr = await _fresh_manager()
    try:
        assert await migrations.get_user_version(mgr.conn) == 0
        await mgr.init_db()
        assert await migrations.get_user_version(mgr.conn) == LATEST_VERSION
        objects = await _schema_objects(mgr.conn)
        names = {name for _type, name in objects}
        assert "habits" in names and "habit_logs" in names
        assert "idx_habits_active_key" in names
        assert "trg_habit_logs_validate_insert" in names
        assert "trg_habit_logs_validate_update" in names
    finally:
        await mgr.close()


async def test_mutation_receipts_table_added_at_v2():
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        names = {name for _t, name in await _schema_objects(mgr.conn)}
        assert "mutation_receipts" in names
        assert LATEST_VERSION >= 2
        assert await migrations.get_user_version(mgr.conn) == LATEST_VERSION
    finally:
        await mgr.close()


async def test_habit_activity_periods_table_added_at_v4():
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        names = {name for _t, name in await _schema_objects(mgr.conn)}
        assert "habit_activity_periods" in names
        assert LATEST_VERSION >= 4
        assert await migrations.get_user_version(mgr.conn) == LATEST_VERSION
    finally:
        await mgr.close()


async def test_v4_backfills_open_period_for_active_habit_only():
    """Active habits get an open period from creation; inactive ones get none."""
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        await mgr.ensure_user(1001, "a", "A")
        active_id, _ = await mgr.add_habit(1001, "Active")
        inactive_id, _ = await mgr.add_habit(1001, "Inactive")
        await mgr.deactivate_habit(1001, inactive_id)
        # Wipe lifecycle-created periods and re-run the v4 backfill from scratch.
        await mgr.conn.execute("DELETE FROM habit_activity_periods")
        await mgr.conn.execute("PRAGMA user_version = 3")
        await mgr.conn.commit()

        await mgr.init_db()

        active_periods = await mgr._query_all(
            "SELECT ended_on FROM habit_activity_periods WHERE habit_id = ?",
            (active_id,),
        )
        inactive_periods = await mgr._query_all(
            "SELECT 1 FROM habit_activity_periods WHERE habit_id = ?", (inactive_id,)
        )
        assert len(active_periods) == 1 and active_periods[0]["ended_on"] is None
        assert inactive_periods == []
    finally:
        await mgr.close()


async def test_reminder_deliveries_table_added_at_v5():
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        names = {name for _t, name in await _schema_objects(mgr.conn)}
        assert "reminder_deliveries" in names
        assert LATEST_VERSION >= 5
        assert await migrations.get_user_version(mgr.conn) == LATEST_VERSION
    finally:
        await mgr.close()


async def test_running_init_twice_makes_no_further_changes():
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        first = await _schema_objects(mgr.conn)
        await mgr.init_db()
        assert await migrations.get_user_version(mgr.conn) == LATEST_VERSION
        assert await _schema_objects(mgr.conn) == first
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Legacy fixtures converge on the fresh schema
# ---------------------------------------------------------------------------
async def test_legacy_fixture_reaches_same_schema_as_fresh():
    fresh = await _fresh_manager()
    legacy = await _fresh_manager()
    try:
        await fresh.init_db()
        fresh_objects = await _schema_objects(fresh.conn)

        await _seed_legacy_schema(legacy.conn)
        await legacy.init_db()
        legacy_objects = await _schema_objects(legacy.conn)

        assert legacy_objects == fresh_objects
        assert await migrations.get_user_version(legacy.conn) == LATEST_VERSION
    finally:
        await fresh.close()
        await legacy.close()


async def test_legacy_diet_gains_macros_without_losing_data():
    mgr = await _fresh_manager()
    try:
        await _seed_legacy_schema(mgr.conn)
        await mgr.conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (1001, 'a', 'A')"
        )
        await mgr.conn.execute(
            "INSERT INTO diet_logs (user_id, meal_type, food_items, calories, logged_at) "
            "VALUES (1001, 'lunch', 'Legacy meal', 725, '2026-07-18 06:30:00')"
        )
        await mgr.conn.commit()

        await mgr.init_db()

        assert {"protein_g", "carbs_g", "fat_g"} <= await _columns(mgr.conn, "diet_logs")
        row = await mgr._query_one("SELECT * FROM diet_logs WHERE user_id = 1001")
        assert row["food_items"] == "Legacy meal"
        assert row["calories"] == 725
        assert row["logged_at"] == "2026-07-18 06:30:00"
        assert row["protein_g"] is None
    finally:
        await mgr.close()


async def test_legacy_habits_backfill_key_and_swap_index():
    mgr = await _fresh_manager()
    try:
        await _seed_legacy_schema(mgr.conn)
        await mgr.conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (1001, 'a', 'A')"
        )
        await mgr.conn.execute(
            "INSERT INTO habits (user_id, habit_name, is_active) VALUES (1001, 'Read Books', 1)"
        )
        await mgr.conn.commit()

        await mgr.init_db()

        assert "name_key" in await _columns(mgr.conn, "habits")
        row = await mgr._query_one("SELECT name_key FROM habits WHERE habit_name = 'Read Books'")
        assert row["name_key"] == "read books"
        names = {name for _t, name in await _schema_objects(mgr.conn)}
        assert "idx_habits_active" not in names  # legacy index retired
        assert "idx_habits_active_key" in names  # keyed index installed
    finally:
        await mgr.close()


async def test_foreign_key_check_passes_after_legacy_migration():
    mgr = await _fresh_manager()
    try:
        await _seed_legacy_schema(mgr.conn)
        await mgr.conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (1001, 'a', 'A')"
        )
        await mgr.conn.execute(
            "INSERT INTO habits (user_id, habit_name) VALUES (1001, 'Meditate')"
        )
        await mgr.conn.commit()
        await mgr.init_db()

        cursor = await mgr.conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Current-shaped version-0 database (already migrated once, version reset)
# ---------------------------------------------------------------------------
async def test_current_shaped_version_zero_is_not_mutated():
    mgr = await _fresh_manager()
    try:
        await mgr.init_db()
        await mgr.ensure_user(1001, "a", "A")
        hid, _ = await mgr.add_habit(1001, "Read")
        before = await mgr._query_one(
            "SELECT id, name_key, is_active FROM habits WHERE id = ?", (hid,)
        )

        # Pretend this current-shaped DB predates versioning.
        await mgr.conn.execute("PRAGMA user_version = 0")
        await mgr.conn.commit()
        await mgr.init_db()

        after = await mgr._query_one(
            "SELECT id, name_key, is_active FROM habits WHERE id = ?", (hid,)
        )
        assert tuple(after) == tuple(before)
        assert await migrations.get_user_version(mgr.conn) == LATEST_VERSION
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Two-user ownership preservation
# ---------------------------------------------------------------------------
async def test_two_user_fixture_keeps_rows_with_owner():
    mgr = await _fresh_manager()
    try:
        await _seed_legacy_schema(mgr.conn)
        for uid in (1001, 2002):
            await mgr.conn.execute(
                "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                (uid, f"u{uid}", "U"),
            )
            await mgr.conn.execute(
                "INSERT INTO habits (user_id, habit_name) VALUES (?, ?)",
                (uid, "Read"),
            )
            await mgr.conn.execute(
                "INSERT INTO diet_logs (user_id, meal_type, food_items, calories) "
                "VALUES (?, 'lunch', 'Meal', 500)",
                (uid,),
            )
        await mgr.conn.commit()

        await mgr.init_db()

        for table in ("habits", "diet_logs"):
            cursor = await mgr.conn.execute(
                f"SELECT user_id, COUNT(*) AS n FROM {table} GROUP BY user_id"  # noqa: S608
            )
            counts = {row["user_id"]: row["n"] for row in await cursor.fetchall()}
            assert counts == {1001: 1, 2002: 1}
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Atomic rollback on forced failure
# ---------------------------------------------------------------------------
async def test_forced_failure_rolls_back_and_keeps_version(monkeypatch):
    async def failing(conn):
        await conn.execute("CREATE TABLE _canary_rollback (x INTEGER)")
        raise RuntimeError("boom halfway through")

    monkeypatch.setattr(migrations, "LATEST_VERSION", 1)
    monkeypatch.setitem(migrations._MIGRATIONS, 1, failing)

    mgr = await _fresh_manager()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await migrations.run_migrations(mgr.conn)

        # Version untouched and the half-created table rolled back (DDL is
        # transactional in SQLite).
        assert await migrations.get_user_version(mgr.conn) == 0
        cursor = await mgr.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = '_canary_rollback'"
        )
        assert await cursor.fetchone() is None
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Normalized habit collision stops with no change
# ---------------------------------------------------------------------------
async def test_active_habit_collision_stops_without_mutation():
    mgr = await _fresh_manager()
    try:
        await _seed_legacy_schema(mgr.conn)
        await mgr.conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (1001, 'a', 'A')"
        )
        # Two active habits that only differ by case: the legacy exact-name index
        # allowed them, but they collapse to the same normalized key.
        await mgr.conn.execute(
            "INSERT INTO habits (user_id, habit_name, is_active) VALUES (1001, 'Read', 1)"
        )
        await mgr.conn.execute(
            "INSERT INTO habits (user_id, habit_name, is_active) VALUES (1001, 'read', 1)"
        )
        await mgr.conn.commit()

        with pytest.raises(MigrationCollisionError):
            await mgr.init_db()

        # No change: version still legacy, both habits still active, and even the
        # ALTER TABLE ADD COLUMN was rolled back (SQLite DDL is transactional),
        # so name_key does not exist yet.
        assert await migrations.get_user_version(mgr.conn) == 0
        assert "name_key" not in await _columns(mgr.conn, "habits")
        cursor = await mgr.conn.execute("SELECT is_active FROM habits ORDER BY id")
        assert [r["is_active"] for r in await cursor.fetchall()] == [1, 1]
    finally:
        await mgr.close()


async def test_collision_message_is_sanitized():
    """The stop diagnostic carries only counts — no raw id, name, or token."""
    mgr = await _fresh_manager()
    try:
        await _seed_legacy_schema(mgr.conn)
        await mgr.conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (555000111, 'sekret', 'Secret')"
        )
        await mgr.conn.execute(
            "INSERT INTO habits (user_id, habit_name, is_active) VALUES (555000111, 'Jog', 1)"
        )
        await mgr.conn.execute(
            "INSERT INTO habits (user_id, habit_name, is_active) VALUES (555000111, 'JOG', 1)"
        )
        await mgr.conn.commit()

        with pytest.raises(MigrationCollisionError) as exc_info:
            await mgr.init_db()

        message = str(exc_info.value)
        assert "555000111" not in message
        assert "jog" not in message.lower()
        assert "secret" not in message.lower()
    finally:
        await mgr.close()
