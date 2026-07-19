"""
Tests for DatabaseManager — schema, CRUD, edge cases.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


class TestSchema:
    """Schema creation and pragmas."""

    async def test_tables_created(self, db):
        """All expected tables exist."""
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        expected = {"users", "study_logs", "gym_logs", "diet_logs", "habits", "habit_logs"}
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    async def test_foreign_keys_on(self, db):
        """PRAGMA foreign_keys is enabled."""
        cursor = await db.conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row[0] == 1, "foreign_keys should be ON"

    async def test_journal_mode_wal(self, db):
        """PRAGMA journal_mode is WAL."""
        cursor = await db.conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        # In-memory databases may report 'memory' instead of 'wal'
        # This is expected behavior — WAL only applies to file-backed DBs
        assert row[0] in ("wal", "memory"), f"Unexpected journal_mode: {row[0]}"

    async def test_indexes_exist(self, db):
        """Composite indexes were created."""
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_%' ORDER BY name"
        )
        indexes = {row["name"] for row in await cursor.fetchall()}
        expected = {
            "idx_study_user_date",
            "idx_gym_user_date",
            "idx_diet_user_date",
            "idx_habit_logs_user_date",
            "idx_habits_active",
        }
        assert expected.issubset(indexes), f"Missing indexes: {expected - indexes}"

    async def test_habit_log_validation_triggers_exist(self, db):
        """Ownership validation is installed during normal schema initialization."""
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
        triggers = {row["name"] for row in await cursor.fetchall()}
        assert {
            "trg_habit_logs_validate_insert",
            "trg_habit_logs_validate_update",
        }.issubset(triggers)

    async def test_habit_logs_reference_users_and_habits(self, db):
        """Fresh databases have direct foreign keys for both parent records."""
        cursor = await db.conn.execute("PRAGMA foreign_key_list(habit_logs)")
        referenced_tables = {row["table"] for row in await cursor.fetchall()}
        assert {"users", "habits"}.issubset(referenced_tables)

    async def test_init_adds_triggers_without_rewriting_history(
        self, db_with_user, user_id
    ):
        """Re-initializing an existing database installs hardening safely."""
        log_date = date(2026, 7, 18)
        habit_id, _ = await db_with_user.add_habit(user_id, "Legacy habit")
        await db_with_user.check_habit(user_id, habit_id, log_date)
        await db_with_user.deactivate_habit(user_id, habit_id)
        await db_with_user.conn.executescript(
            """
            DROP TRIGGER trg_habit_logs_validate_insert;
            DROP TRIGGER trg_habit_logs_validate_update;
            """
        )
        await db_with_user.conn.commit()

        await db_with_user.init_db()

        rows = await db_with_user.get_habit_logs_range(user_id, log_date, log_date)
        assert [row["habit_id"] for row in rows] == [habit_id]
        cursor = await db_with_user.conn.execute(
            "SELECT COUNT(*) AS count FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE 'trg_habit_logs_validate_%'"
        )
        assert (await cursor.fetchone())["count"] == 2


class TestUsers:
    """User CRUD."""

    async def test_ensure_user_creates(self, db, user_id):
        """ensure_user inserts a new user."""
        await db.ensure_user(user_id, "alice", "Alice")
        cursor = await db.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["username"] == "alice"
        assert row["first_name"] == "Alice"

    async def test_ensure_user_updates(self, db, user_id):
        """ensure_user updates existing user."""
        await db.ensure_user(user_id, "alice", "Alice")
        await db.ensure_user(user_id, "alice2", "Alice2")
        cursor = await db.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        assert row["username"] == "alice2"
        assert row["first_name"] == "Alice2"


class TestStudy:
    """Study log CRUD."""

    async def test_log_and_retrieve(self, db_with_user, user_id):
        """Log a study session and retrieve it."""
        from datetime import date, timedelta

        row_id = await db_with_user.log_study(user_id, "Math", 45, "Chapter 3")
        assert row_id is not None

        today = date.today()
        logs = await db_with_user.get_study_logs(user_id, today, today)
        assert len(logs) >= 1
        found = [r for r in logs if r["id"] == row_id]
        assert len(found) == 1
        assert found[0]["subject"] == "Math"
        assert found[0]["duration_min"] == 45
        assert found[0]["notes"] == "Chapter 3"


@pytest.mark.parametrize(
    ("insert_sql", "values", "getter_name"),
    [
        (
            "INSERT INTO study_logs "
            "(user_id, subject, duration_min, logged_at) VALUES (?, ?, ?, ?)",
            ("Math", 30),
            "get_study_logs",
        ),
        (
            "INSERT INTO gym_logs "
            "(user_id, exercise, sets, reps, logged_at) VALUES (?, ?, ?, ?, ?)",
            ("Squat", 3, 5),
            "get_gym_logs",
        ),
        (
            "INSERT INTO diet_logs "
            "(user_id, meal_type, food_items, logged_at) VALUES (?, ?, ?, ?)",
            ("breakfast", "oats"),
            "get_diet_logs",
        ),
    ],
)
async def test_current_timestamp_style_rows_are_in_local_day_buffer(
    db_with_user, user_id, insert_sql, values, getter_name
):
    """Space-separated UTC timestamps near local midnight are not skipped."""
    logged_at = "2026-07-17 19:00:00"  # 2026-07-18 00:30 in Asia/Kolkata
    cursor = await db_with_user.conn.execute(
        insert_sql, (user_id, *values, logged_at)
    )
    await db_with_user.conn.commit()

    getter = getattr(db_with_user, getter_name)
    rows = await getter(user_id, date(2026, 7, 18), date(2026, 7, 18))

    assert [row["id"] for row in rows] == [cursor.lastrowid]


class TestGym:
    """Gym log CRUD."""

    async def test_log_with_weight(self, db_with_user, user_id):
        """Log a weighted exercise."""
        row_id = await db_with_user.log_gym(user_id, "Bench Press", 4, 10, 60.0)
        assert row_id is not None

    async def test_log_bodyweight(self, db_with_user, user_id):
        """Log a bodyweight exercise (NULL weight)."""
        row_id = await db_with_user.log_gym(user_id, "Push-ups", 3, 15, None)
        from datetime import date
        logs = await db_with_user.get_gym_logs(user_id, date.today(), date.today())
        found = [r for r in logs if r["id"] == row_id]
        assert len(found) == 1
        assert found[0]["weight_kg"] is None


class TestDiet:
    """Diet log CRUD."""

    async def test_log_with_calories(self, db_with_user, user_id):
        """Log a meal with calories."""
        row_id = await db_with_user.log_diet(user_id, "lunch", "dal, rice", 650)
        assert row_id is not None

    async def test_log_without_calories(self, db_with_user, user_id):
        """Log a meal without calories (skipped)."""
        row_id = await db_with_user.log_diet(user_id, "dinner", "pasta", None)
        from datetime import date
        logs = await db_with_user.get_diet_logs(user_id, date.today(), date.today())
        found = [r for r in logs if r["id"] == row_id]
        assert len(found) == 1
        assert found[0]["calories"] is None

    async def test_invalid_meal_type(self, db_with_user, user_id):
        """CHECK constraint rejects invalid meal types."""
        with pytest.raises(sqlite3.IntegrityError):
            await db_with_user.log_diet(user_id, "brunch", "eggs", 300)
        assert db_with_user.conn.in_transaction is False

        # The failed write must release the mutation lock and leave the
        # connection ready for the next operation.
        row_id = await db_with_user.log_diet(user_id, "breakfast", "eggs", 300)
        assert row_id is not None


class TestHabits:
    """Habit management."""

    async def test_add_habit(self, db_with_user, user_id):
        """Add a new habit."""
        habit_id, status = await db_with_user.add_habit(user_id, "Meditate")
        assert habit_id is not None
        assert status == "added"

    async def test_add_active_habit_returns_existing(self, db_with_user, user_id):
        """Adding an active name is graceful and does not create a duplicate."""
        habit_id, status = await db_with_user.add_habit(user_id, "Meditate")
        same_id, duplicate_status = await db_with_user.add_habit(user_id, "Meditate")

        assert status == "added"
        assert same_id == habit_id
        assert duplicate_status == "already_active"
        cursor = await db_with_user.conn.execute(
            "SELECT COUNT(*) AS count FROM habits "
            "WHERE user_id = ? AND habit_name = ?",
            (user_id, "Meditate"),
        )
        assert (await cursor.fetchone())["count"] == 1

    async def test_deactivate_and_reactivate(self, db_with_user, user_id):
        """Deactivate a habit, then re-add it — should reactivate, not create new."""
        habit_id_1, _ = await db_with_user.add_habit(user_id, "Read")
        await db_with_user.deactivate_habit(user_id, habit_id_1)

        # Re-add
        habit_id_2, status = await db_with_user.add_habit(user_id, "Read")
        assert habit_id_2 == habit_id_1, "Should reactivate, not create new"
        assert status == "reactivated"

        # Should be active again
        habits = await db_with_user.get_active_habits(user_id)
        names = [h["habit_name"] for h in habits]
        assert "Read" in names

    async def test_partial_unique_allows_inactive_duplicate(self, db_with_user, user_id):
        """Two inactive habits with the same name shouldn't conflict."""
        habit_id_1, _ = await db_with_user.add_habit(user_id, "Yoga")
        await db_with_user.deactivate_habit(user_id, habit_id_1)

        # This should work — reactivates
        habit_id_2, _ = await db_with_user.add_habit(user_id, "Yoga")
        assert habit_id_2 == habit_id_1

    async def test_check_and_uncheck(self, db_with_user, user_id):
        """Check and uncheck a habit for a specific date."""
        from datetime import date

        habit_id, _ = await db_with_user.add_habit(user_id, "Exercise")
        today = date.today()

        # Check
        result = await db_with_user.check_habit(user_id, habit_id, today)
        assert result is True

        checked = await db_with_user.get_checked_habits(user_id, today)
        assert habit_id in checked

        # Duplicate check
        result = await db_with_user.check_habit(user_id, habit_id, today)
        assert result is False  # already checked
        assert db_with_user.conn.in_transaction is False

        # Uncheck
        result = await db_with_user.uncheck_habit(user_id, habit_id, today)
        assert result is True

        checked = await db_with_user.get_checked_habits(user_id, today)
        assert habit_id not in checked

    async def test_cannot_check_another_users_habit(self, db_with_user, user_id):
        """A callback cannot create a check-off for another user's habit ID."""
        other_user_id = user_id + 1
        await db_with_user.ensure_user(other_user_id, "other", "Other")
        habit_id, _ = await db_with_user.add_habit(user_id, "Private habit")

        inserted = await db_with_user.check_habit(
            other_user_id, habit_id, date(2026, 7, 18)
        )

        assert inserted is False
        assert await db_with_user.get_checked_habits(
            other_user_id, date(2026, 7, 18)
        ) == set()

    async def test_cannot_check_inactive_habit(self, db_with_user, user_id):
        """Stale buttons cannot add new rows for inactive habits."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Retired habit")
        await db_with_user.deactivate_habit(user_id, habit_id)

        inserted = await db_with_user.check_habit(
            user_id, habit_id, date(2026, 7, 18)
        )

        assert inserted is False

    async def test_inactive_habit_history_remains_readable_and_removable(
        self, db_with_user, user_id
    ):
        """Hardening new writes does not hide or strand historical check-offs."""
        log_date = date(2026, 7, 18)
        habit_id, _ = await db_with_user.add_habit(user_id, "Archived habit")
        assert await db_with_user.check_habit(user_id, habit_id, log_date) is True
        await db_with_user.deactivate_habit(user_id, habit_id)

        assert habit_id in await db_with_user.get_checked_habits(user_id, log_date)
        rows = await db_with_user.get_habit_logs_range(user_id, log_date, log_date)
        assert [row["habit_id"] for row in rows] == [habit_id]
        assert await db_with_user.uncheck_habit(user_id, habit_id, log_date) is True

    async def test_schema_trigger_rejects_mismatched_direct_insert(
        self, db_with_user, user_id
    ):
        """The database itself rejects cross-user habit-log rows."""
        other_user_id = user_id + 1
        await db_with_user.ensure_user(other_user_id, "other", "Other")
        habit_id, _ = await db_with_user.add_habit(user_id, "Owned habit")

        with pytest.raises(sqlite3.IntegrityError, match="active and belong"):
            await db_with_user.conn.execute(
                "INSERT INTO habit_logs (user_id, habit_id, log_date) VALUES (?, ?, ?)",
                (other_user_id, habit_id, "2026-07-18"),
            )
        await db_with_user.conn.rollback()

    async def test_check_failure_rolls_back_and_propagates(
        self, db_with_user, user_id
    ):
        """Real database failures are not mistaken for duplicate checks."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Trigger failure")
        await db_with_user.conn.executescript(
            """
            CREATE TRIGGER force_habit_check_failure
            BEFORE INSERT ON habit_logs
            BEGIN
                SELECT RAISE(ABORT, 'forced failure');
            END;
            """
        )
        await db_with_user.conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
            await db_with_user.check_habit(user_id, habit_id, date(2026, 7, 18))

        assert db_with_user.conn.in_transaction is False


class TestUndo:
    """Undo functionality."""

    async def test_undo_study(self, db_with_user, user_id):
        """Undo deletes the most recent study log."""
        await db_with_user.log_study(user_id, "Physics", 30, None)
        entry = await db_with_user.undo_last(user_id)
        assert entry is not None
        assert entry["subject"] == "Physics"

    async def test_undo_empty(self, db_with_user, user_id):
        """Undo with no entries returns None."""
        entry = await db_with_user.undo_last(user_id)
        assert entry is None

    async def test_concurrent_undos_delete_distinct_entries(
        self, db_with_user, user_id
    ):
        """Concurrent calls serialize and never report one deletion twice."""
        first_id = await db_with_user.log_study(user_id, "First", 10)
        second_id = await db_with_user.log_study(user_id, "Second", 20)

        results = await asyncio.gather(
            db_with_user.undo_last(user_id),
            db_with_user.undo_last(user_id),
            db_with_user.undo_last(user_id),
        )

        deleted = [entry for entry in results if entry is not None]
        assert {entry["id"] for entry in deleted} == {first_id, second_id}
        assert len(deleted) == 2
        assert results.count(None) == 1
        cursor = await db_with_user.conn.execute(
            "SELECT COUNT(*) AS count FROM study_logs WHERE user_id = ?", (user_id,)
        )
        assert (await cursor.fetchone())["count"] == 0

    async def test_undo_uses_microseconds_across_categories(
        self, db_with_user, user_id, monkeypatch
    ):
        """The latest cross-category insert wins even within one second."""
        base = datetime.now(timezone.utc).replace(microsecond=100_000)
        timestamps = [
            (base + timedelta(microseconds=offset)).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )
            for offset in range(3)
        ]
        timestamp_iter = iter(timestamps)
        monkeypatch.setattr(
            "bot.database._utc_timestamp_now", timestamp_iter.__next__
        )

        await db_with_user.log_study(user_id, "First", 10)
        await db_with_user.log_gym(user_id, "Second", 3, 5)
        diet_id = await db_with_user.log_diet(user_id, "snack", "Third", 100)

        entry = await db_with_user.undo_last(user_id)

        assert entry is not None
        assert entry["id"] == diet_id
        assert entry["food_items"] == "Third"
        assert entry["logged_at"] == timestamps[-1]

    async def test_undo_uses_id_to_break_same_table_timestamp_ties(
        self, db_with_user, user_id
    ):
        """Same-second rows in one table undo in reverse insertion order."""
        logged_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        first = await db_with_user.conn.execute(
            "INSERT INTO study_logs "
            "(user_id, subject, duration_min, logged_at) VALUES (?, ?, ?, ?)",
            (user_id, "First", 10, logged_at),
        )
        second = await db_with_user.conn.execute(
            "INSERT INTO study_logs "
            "(user_id, subject, duration_min, logged_at) VALUES (?, ?, ?, ?)",
            (user_id, "Second", 20, logged_at),
        )
        await db_with_user.conn.commit()

        entry = await db_with_user.undo_last(user_id)

        assert first.lastrowid < second.lastrowid
        assert entry is not None
        assert entry["id"] == second.lastrowid
        assert entry["subject"] == "Second"
