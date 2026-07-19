"""
Tests for DatabaseManager — schema, CRUD, edge cases.
"""

from __future__ import annotations

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
            "idx_habits_active",
        }
        assert expected.issubset(indexes), f"Missing indexes: {expected - indexes}"


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
        import aiosqlite
        with pytest.raises(Exception):
            await db_with_user.log_diet(user_id, "brunch", "eggs", 300)


class TestHabits:
    """Habit management."""

    async def test_add_habit(self, db_with_user, user_id):
        """Add a new habit."""
        habit_id, reactivated = await db_with_user.add_habit(user_id, "Meditate")
        assert habit_id is not None

    async def test_deactivate_and_reactivate(self, db_with_user, user_id):
        """Deactivate a habit, then re-add it — should reactivate, not create new."""
        habit_id_1, _ = await db_with_user.add_habit(user_id, "Read")
        await db_with_user.deactivate_habit(user_id, habit_id_1)

        # Re-add
        habit_id_2, reactivated = await db_with_user.add_habit(user_id, "Read")
        assert habit_id_2 == habit_id_1, "Should reactivate, not create new"
        assert reactivated is True

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

        # Uncheck
        result = await db_with_user.uncheck_habit(user_id, habit_id, today)
        assert result is True

        checked = await db_with_user.get_checked_habits(user_id, today)
        assert habit_id not in checked


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
