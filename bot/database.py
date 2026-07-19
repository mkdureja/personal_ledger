"""
Async SQLite database manager for Ledger bot.

Single shared connection with WAL mode, foreign keys, and composite indexes.
Row-presence semantics for habit_logs (no 'completed' column).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS study_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    subject      TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    notes        TEXT,
    logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS gym_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    exercise    TEXT NOT NULL,
    sets        INTEGER NOT NULL,
    reps        INTEGER NOT NULL,
    weight_kg   REAL,
    logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS diet_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    meal_type   TEXT NOT NULL CHECK(meal_type IN ('breakfast','lunch','dinner','snack')),
    food_items  TEXT NOT NULL,
    calories    INTEGER,
    logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS habits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    habit_name  TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active   INTEGER DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS habit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    habit_id    INTEGER NOT NULL,
    log_date    DATE NOT NULL,
    UNIQUE(user_id, habit_id, log_date),
    FOREIGN KEY (habit_id) REFERENCES habits(id)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_study_user_date ON study_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_gym_user_date ON gym_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_diet_user_date ON diet_logs(user_id, logged_at);
"""

# Partial unique index: only active habits must have unique names per user.
# This allows deactivate → re-add without collision.
_PARTIAL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_active
    ON habits(user_id, habit_name) WHERE is_active = 1;
"""


class DatabaseManager:
    """Async SQLite manager holding a single shared connection."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection and set pragmas."""
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = aiosqlite.Row
        logger.info("Database connected: %s", self.db_path)

    async def init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        assert self._conn is not None, "Call connect() first"
        await self._conn.executescript(_SCHEMA)
        # Indexes must be created individually (executescript doesn't return
        # cursors, but these are safe as IF NOT EXISTS).
        for stmt in _INDEXES.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        # Partial unique index
        await self._conn.execute(_PARTIAL_INDEX.strip())
        await self._conn.commit()
        logger.info("Database schema initialized")

    async def close(self) -> None:
        """Close the connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # -------------------------------------------------------------------
    # Users
    # -------------------------------------------------------------------
    async def ensure_user(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> None:
        """Insert or update user record."""
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
        )
        await self.conn.commit()

    # -------------------------------------------------------------------
    # Study
    # -------------------------------------------------------------------
    async def log_study(
        self,
        user_id: int,
        subject: str,
        duration_min: int,
        notes: str | None = None,
    ) -> int:
        """Log a study session. Returns the row ID."""
        cursor = await self.conn.execute(
            "INSERT INTO study_logs (user_id, subject, duration_min, notes) "
            "VALUES (?, ?, ?, ?)",
            (user_id, subject, duration_min, notes),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_study_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get study logs for a user within a local-date range.

        Dates are compared by converting logged_at to the local timezone
        at the application layer, but here we do a rough UTC filter and
        let the caller bucket precisely.  For simplicity we fetch a
        slightly wider window (±1 day) and let the caller filter.
        """
        # Widen by 1 day on each side to handle TZ offset
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        cursor = await self.conn.execute(
            "SELECT * FROM study_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, start_utc.isoformat(), end_utc.isoformat()),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Gym
    # -------------------------------------------------------------------
    async def log_gym(
        self,
        user_id: int,
        exercise: str,
        sets: int,
        reps: int,
        weight_kg: float | None = None,
    ) -> int:
        """Log a single gym exercise. Returns the row ID."""
        cursor = await self.conn.execute(
            "INSERT INTO gym_logs (user_id, exercise, sets, reps, weight_kg) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, exercise, sets, reps, weight_kg),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_gym_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get gym logs within a local-date range (with ±1 day buffer)."""
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        cursor = await self.conn.execute(
            "SELECT * FROM gym_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, start_utc.isoformat(), end_utc.isoformat()),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Diet
    # -------------------------------------------------------------------
    async def log_diet(
        self,
        user_id: int,
        meal_type: str,
        food_items: str,
        calories: int | None = None,
    ) -> int:
        """Log a diet entry. Returns the row ID."""
        cursor = await self.conn.execute(
            "INSERT INTO diet_logs (user_id, meal_type, food_items, calories) "
            "VALUES (?, ?, ?, ?)",
            (user_id, meal_type, food_items, calories),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_diet_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get diet logs within a local-date range (with ±1 day buffer)."""
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        cursor = await self.conn.execute(
            "SELECT * FROM diet_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, start_utc.isoformat(), end_utc.isoformat()),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Habits
    # -------------------------------------------------------------------
    async def add_habit(self, user_id: int, habit_name: str) -> tuple[int, bool]:
        """Add a habit or reactivate a deactivated one.

        Returns (habit_id, reactivated: bool).
        """
        # Check if an inactive habit with the same name exists
        cursor = await self.conn.execute(
            "SELECT id FROM habits WHERE user_id = ? AND habit_name = ? AND is_active = 0",
            (user_id, habit_name),
        )
        row = await cursor.fetchone()
        if row:
            habit_id = row["id"]
            await self.conn.execute(
                "UPDATE habits SET is_active = 1 WHERE id = ?", (habit_id,)
            )
            await self.conn.commit()
            return habit_id, True

        # Insert new
        cursor = await self.conn.execute(
            "INSERT INTO habits (user_id, habit_name) VALUES (?, ?)",
            (user_id, habit_name),
        )
        await self.conn.commit()
        return cursor.lastrowid, True  # type: ignore[return-value]

    async def deactivate_habit(self, user_id: int, habit_id: int) -> bool:
        """Soft-delete a habit. Returns True if a row was affected."""
        cursor = await self.conn.execute(
            "UPDATE habits SET is_active = 0 WHERE id = ? AND user_id = ? AND is_active = 1",
            (habit_id, user_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_active_habits(self, user_id: int) -> list[aiosqlite.Row]:
        """Get all active habits for a user."""
        cursor = await self.conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,),
        )
        return await cursor.fetchall()

    async def check_habit(self, user_id: int, habit_id: int, log_date: date) -> bool:
        """Mark a habit as done for a specific local date.

        Returns True if inserted, False if already exists.
        """
        try:
            await self.conn.execute(
                "INSERT INTO habit_logs (user_id, habit_id, log_date) VALUES (?, ?, ?)",
                (user_id, habit_id, log_date.isoformat()),
            )
            await self.conn.commit()
            return True
        except Exception:
            # UNIQUE constraint violation — already checked
            return False

    async def uncheck_habit(self, user_id: int, habit_id: int, log_date: date) -> bool:
        """Remove a habit check for a specific local date.

        Returns True if a row was deleted.
        """
        cursor = await self.conn.execute(
            "DELETE FROM habit_logs WHERE user_id = ? AND habit_id = ? AND log_date = ?",
            (user_id, habit_id, log_date.isoformat()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_checked_habits(
        self, user_id: int, log_date: date
    ) -> set[int]:
        """Get the set of habit_ids checked on a specific local date."""
        cursor = await self.conn.execute(
            "SELECT habit_id FROM habit_logs WHERE user_id = ? AND log_date = ?",
            (user_id, log_date.isoformat()),
        )
        rows = await cursor.fetchall()
        return {row["habit_id"] for row in rows}

    async def get_habit_logs_range(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get habit logs in a date range (inclusive)."""
        cursor = await self.conn.execute(
            "SELECT * FROM habit_logs WHERE user_id = ? AND log_date >= ? AND log_date <= ? "
            "ORDER BY log_date",
            (user_id, start_date.isoformat(), end_date.isoformat()),
        )
        return await cursor.fetchall()

    async def get_streak(self, user_id: int, habit_id: int, today: date) -> int:
        """Calculate current streak for a habit.

        Streak = number of consecutive days with a row in habit_logs,
        counting backward from today.  If today is not checked, streak is 0.
        """
        cursor = await self.conn.execute(
            "SELECT log_date FROM habit_logs "
            "WHERE user_id = ? AND habit_id = ? AND log_date <= ? "
            "ORDER BY log_date DESC",
            (user_id, habit_id, today.isoformat()),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        streak = 0
        expected = today
        for row in rows:
            row_date = date.fromisoformat(row["log_date"])
            if row_date == expected:
                streak += 1
                expected -= timedelta(days=1)
            else:
                break
        return streak

    # -------------------------------------------------------------------
    # Undo (delete most recent log across all tables)
    # -------------------------------------------------------------------
    async def undo_last(self, user_id: int) -> dict[str, Any] | None:
        """Delete the most recent log entry for this user (within 24h).

        Checks study_logs, gym_logs, diet_logs. habit_logs use a different
        undo path (uncheck_habit).

        Returns a dict with the deleted entry info, or None if nothing found.
        """
        tables = [
            ("study_logs", "📖 Study"),
            ("gym_logs", "🏋️ Gym"),
            ("diet_logs", "🍽️ Diet"),
        ]
        latest: dict[str, Any] | None = None
        latest_table: str | None = None

        for table_name, label in tables:
            cursor = await self.conn.execute(
                f"SELECT *, '{label}' as category FROM {table_name} "  # noqa: S608
                f"WHERE user_id = ? ORDER BY logged_at DESC LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                continue

            logged_at_str = row["logged_at"]
            if logged_at_str is None:
                continue

            logged_at = datetime.fromisoformat(logged_at_str)
            # SQLite CURRENT_TIMESTAMP is naive UTC — make it aware
            if logged_at.tzinfo is None:
                logged_at = logged_at.replace(tzinfo=timezone.utc)
            # Check if within 24h
            if (datetime.now(timezone.utc) - logged_at).total_seconds() > 86400:
                continue

            if latest is None or logged_at_str > latest["logged_at"]:
                latest = dict(row)
                latest_table = table_name

        if latest is None or latest_table is None:
            return None

        await self.conn.execute(
            f"DELETE FROM {latest_table} WHERE id = ?",  # noqa: S608
            (latest["id"],),
        )
        await self.conn.commit()
        return latest

    # -------------------------------------------------------------------
    # Summary helpers
    # -------------------------------------------------------------------
    async def get_today_study_total(self, user_id: int, local_today: date) -> int:
        """Total study minutes for today (local date)."""
        logs = await self.get_study_logs(user_id, local_today, local_today)
        from .config import local_date_from_utc

        total = 0
        for row in logs:
            if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == local_today:
                total += row["duration_min"]
        return total

    async def get_today_calories(self, user_id: int, local_today: date) -> tuple[int, bool]:
        """Total calories for today. Returns (total, has_incomplete).

        has_incomplete is True if any meal has NULL calories.
        """
        logs = await self.get_diet_logs(user_id, local_today, local_today)
        from .config import local_date_from_utc

        total = 0
        has_incomplete = False
        for row in logs:
            if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == local_today:
                if row["calories"] is not None:
                    total += row["calories"]
                else:
                    has_incomplete = True
        return total, has_incomplete

    async def get_users_with_unchecked_habits(
        self, allowed_ids: frozenset[int], local_today: date
    ) -> dict[int, list[str]]:
        """For each allowed user, get list of unchecked habit names for today.

        Returns {user_id: [habit_name, ...]} — only users with unchecked habits.
        """
        result: dict[int, list[str]] = {}
        for uid in allowed_ids:
            habits = await self.get_active_habits(uid)
            if not habits:
                continue
            checked = await self.get_checked_habits(uid, local_today)
            unchecked = [h["habit_name"] for h in habits if h["id"] not in checked]
            if unchecked:
                result[uid] = unchecked
        return result
