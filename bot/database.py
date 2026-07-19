"""
Async SQLite database manager for Ledger bot.

Single shared connection with WAL mode, foreign keys, and composite indexes.
Row-presence semantics for habit_logs (no 'completed' column).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import aiosqlite

from .nutrition import (
    FOOD_BASE_UNITS,
    MAX_CATALOG_AMOUNT as NUTRITION_MAX_CATALOG_AMOUNT,
    MAX_CATALOG_NAME_LENGTH,
    MAX_NUTRIENT_VALUE as NUTRITION_MAX_NUTRIENT_VALUE,
    MAX_PORTION_NAME_LENGTH,
    RECIPE_YIELD_UNITS,
    canonical_unit_alias,
    normalize_catalog_name,
)

logger = logging.getLogger(__name__)

HabitAddStatus = Literal["added", "reactivated", "already_active"]

MAX_DISPLAY_UNIT_LENGTH = MAX_PORTION_NAME_LENGTH
MAX_ACTIVE_FOODS = 500
MAX_PORTIONS_PER_FOOD = 50
MAX_ACTIVE_RECIPES = 200
MAX_INGREDIENTS_PER_RECIPE = 100
MAX_CATALOG_AMOUNT = float(NUTRITION_MAX_CATALOG_AMOUNT)
MAX_NUTRIENT_VALUE = float(NUTRITION_MAX_NUTRIENT_VALUE)

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
    protein_g   REAL,
    carbs_g     REAL,
    fat_g       REAL,
    logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS foods (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
    name_key     TEXT NOT NULL CHECK(length(name_key) BETWEEN 1 AND 100),
    base_unit    TEXT NOT NULL CHECK(base_unit IN ('g','ml','piece')),
    basis_amount REAL NOT NULL CHECK(basis_amount > 0 AND basis_amount <= 1000000),
    calories     REAL CHECK(calories IS NULL OR (calories >= 0 AND calories <= 1000000)),
    protein_g    REAL CHECK(protein_g IS NULL OR (protein_g >= 0 AND protein_g <= 1000000)),
    carbs_g      REAL CHECK(carbs_g IS NULL OR (carbs_g >= 0 AND carbs_g <= 1000000)),
    fat_g        REAL CHECK(fat_g IS NULL OR (fat_g >= 0 AND fat_g <= 1000000)),
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS food_portions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    food_id      INTEGER NOT NULL,
    name         TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 50),
    name_key     TEXT NOT NULL CHECK(length(name_key) BETWEEN 1 AND 50),
    base_amount  REAL NOT NULL CHECK(base_amount > 0 AND base_amount <= 1000000),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(food_id, name_key),
    FOREIGN KEY (food_id, user_id)
        REFERENCES foods(id, user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recipes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
    name_key     TEXT NOT NULL CHECK(length(name_key) BETWEEN 1 AND 100),
    yield_amount REAL NOT NULL CHECK(yield_amount > 0 AND yield_amount <= 1000000),
    yield_unit   TEXT NOT NULL CHECK(yield_unit IN ('g','ml','piece','serving')),
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    recipe_id      INTEGER NOT NULL,
    food_id        INTEGER NOT NULL,
    base_amount    REAL NOT NULL CHECK(base_amount > 0 AND base_amount <= 1000000),
    display_amount REAL NOT NULL CHECK(display_amount > 0 AND display_amount <= 1000000),
    display_unit   TEXT NOT NULL CHECK(length(display_unit) BETWEEN 1 AND 50),
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recipe_id, food_id),
    FOREIGN KEY (recipe_id, user_id)
        REFERENCES recipes(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (food_id, user_id)
        REFERENCES foods(id, user_id) ON DELETE RESTRICT
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
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (habit_id) REFERENCES habits(id)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_study_user_date ON study_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_gym_user_date ON gym_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_diet_user_date ON diet_logs(user_id, logged_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_foods_active_name
    ON foods(user_id, name_key) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_food_portions_lookup
    ON food_portions(user_id, food_id, name_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_active_name
    ON recipes(user_id, name_key) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_lookup
    ON recipe_ingredients(user_id, recipe_id, id);
CREATE INDEX IF NOT EXISTS idx_habit_logs_user_date ON habit_logs(user_id, log_date);
"""

# Partial unique index: only active habits must have unique names per user.
# This allows deactivate → re-add without collision.
_PARTIAL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_active
    ON habits(user_id, habit_name) WHERE is_active = 1;
"""

# ``CREATE TABLE IF NOT EXISTS`` does not add columns to an existing table.
# Keep these declarations simple so SQLite can add them without rebuilding the
# table or changing existing rows.
_DIET_MACRO_COLUMNS = (
    ("protein_g", "REAL"),
    ("carbs_g", "REAL"),
    ("fat_g", "REAL"),
)

# Existing databases cannot gain a new foreign key without rebuilding the table.
# These triggers enforce the same ownership and active-habit invariant for both
# existing and newly-created databases, without invalidating historical rows.
_HABIT_LOG_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_habit_logs_validate_insert
BEFORE INSERT ON habit_logs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM habits AS h
    JOIN users AS u ON u.user_id = h.user_id
    WHERE h.id = NEW.habit_id
      AND h.user_id = NEW.user_id
      AND h.is_active = 1
)
BEGIN
    SELECT RAISE(ABORT, 'habit must be active and belong to user');
END;

CREATE TRIGGER IF NOT EXISTS trg_habit_logs_validate_update
BEFORE UPDATE OF user_id, habit_id ON habit_logs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM habits AS h
    JOIN users AS u ON u.user_id = h.user_id
    WHERE h.id = NEW.habit_id
      AND h.user_id = NEW.user_id
      AND h.is_active = 1
)
BEGIN
    SELECT RAISE(ABORT, 'habit must be active and belong to user');
END;
"""


def _normalize_catalog_text(value: str, field_name: str, max_length: int) -> str:
    """Normalize bounded user-facing catalog text without changing its case."""
    display, _key = normalize_catalog_name(value, field_name, max_length)
    return display


def _catalog_key(value: str, field_name: str, max_length: int) -> str:
    """Return a Unicode-normalized, case-insensitive catalog lookup key."""
    _display, key = normalize_catalog_name(value, field_name, max_length)
    return key


def _catalog_unit(value: str, allowed: frozenset[str], field_name: str) -> str:
    """Validate one of the deliberately small canonical unit vocabularies."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    unit = value.strip().lower()
    if unit not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {choices}")
    return unit


def _positive_catalog_amount(value: float, field_name: str) -> float:
    """Validate a positive finite catalog quantity and return it as a float."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(amount):
        raise ValueError(f"{field_name} must be finite")
    if amount <= 0 or amount > MAX_CATALOG_AMOUNT:
        raise ValueError(
            f"{field_name} must be greater than 0 and at most {MAX_CATALOG_AMOUNT:g}"
        )
    return amount


def _optional_nutrient(value: float | None, field_name: str) -> float | None:
    """Validate an optional finite, non-negative nutrition value."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        nutrient = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(nutrient):
        raise ValueError(f"{field_name} must be finite")
    if nutrient < 0 or nutrient > MAX_NUTRIENT_VALUE:
        raise ValueError(
            f"{field_name} must be between 0 and {MAX_NUTRIENT_VALUE:g}"
        )
    return nutrient


def _sqlite_timestamp(value: datetime) -> str:
    """Format a timestamp like SQLite's ``CURRENT_TIMESTAMP``.

    SQLite compares the TIMESTAMP values in this schema as text.  A space must
    separate the date and time; ``datetime.isoformat()`` uses ``T`` and sorts
    after same-day ``CURRENT_TIMESTAMP`` values.
    """
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _utc_timestamp_now() -> str:
    """Return the current UTC time in SQLite-sortable microsecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


class DatabaseManager:
    """Async SQLite manager holding a single shared connection."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

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
        async with self._write_operation():
            await self._conn.executescript(_SCHEMA)
            await self._add_missing_diet_macro_columns()
            # Indexes must be created individually (executescript doesn't return
            # cursors, but these are safe as IF NOT EXISTS).
            for stmt in _INDEXES.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await self._conn.execute(stmt)
            # Partial unique index
            await self._conn.execute(_PARTIAL_INDEX.strip())
            await self._conn.executescript(_HABIT_LOG_TRIGGERS)
        logger.info("Database schema initialized")

    async def _add_missing_diet_macro_columns(self) -> None:
        """Add nullable macro columns to a pre-macro ``diet_logs`` table."""
        cursor = await self.conn.execute("PRAGMA table_info(diet_logs)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}

        for column_name, column_type in _DIET_MACRO_COLUMNS:
            if column_name not in existing_columns:
                await self.conn.execute(
                    f"ALTER TABLE diet_logs ADD COLUMN {column_name} {column_type}"  # noqa: S608
                )

    async def close(self) -> None:
        """Close the connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    @asynccontextmanager
    async def _write_operation(self) -> AsyncIterator[None]:
        """Serialize a complete mutation and close its transaction safely.

        SQLite WAL mode with a single connection already serializes writes at
        the database level.  This lock is a belt-and-suspenders measure that
        makes the serialization explicit in application code and guarantees
        that the commit/rollback lifecycle is never interleaved even if a
        future change introduces concurrent connections.
        """
        async with self._write_lock:
            try:
                yield
                await self.conn.commit()
            except BaseException:
                if self.conn.in_transaction:
                    await self.conn.rollback()
                raise

    # -------------------------------------------------------------------
    # Users
    # -------------------------------------------------------------------
    async def ensure_user(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> None:
        """Insert or update user record."""
        async with self._write_operation():
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
        async with self._write_operation():
            cursor = await self.conn.execute(
                "INSERT INTO study_logs "
                "(user_id, subject, duration_min, notes, logged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, subject, duration_min, notes, _utc_timestamp_now()),
            )
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
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
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
        async with self._write_operation():
            cursor = await self.conn.execute(
                "INSERT INTO gym_logs "
                "(user_id, exercise, sets, reps, weight_kg, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, exercise, sets, reps, weight_kg, _utc_timestamp_now()),
            )
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
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
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
        protein_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
    ) -> int:
        """Log a diet entry. Returns the row ID."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "INSERT INTO diet_logs "
                "(user_id, meal_type, food_items, calories, protein_g, carbs_g, "
                "fat_g, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    meal_type,
                    food_items,
                    calories,
                    protein_g,
                    carbs_g,
                    fat_g,
                    _utc_timestamp_now(),
                ),
            )
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
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Food catalog
    # -------------------------------------------------------------------
    async def save_food(
        self,
        user_id: int,
        name: str,
        base_unit: str,
        basis_amount: float,
        calories: float | None = None,
        protein_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
    ) -> dict[str, Any]:
        """Add or update an active food, keyed by normalized name."""
        normalized_name = _normalize_catalog_text(
            name, "Food name", MAX_CATALOG_NAME_LENGTH
        )
        name_key = _catalog_key(name, "Food name", MAX_CATALOG_NAME_LENGTH)
        normalized_unit = _catalog_unit(base_unit, FOOD_BASE_UNITS, "Base unit")
        normalized_basis = _positive_catalog_amount(basis_amount, "Basis amount")
        nutrients = (
            _optional_nutrient(calories, "Calories"),
            _optional_nutrient(protein_g, "Protein"),
            _optional_nutrient(carbs_g, "Carbs"),
            _optional_nutrient(fat_g, "Fat"),
        )

        async with self._write_operation():
            cursor = await self.conn.execute(
                "SELECT * FROM foods "
                "WHERE user_id = ? AND name_key = ? AND is_active = 1",
                (user_id, name_key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if existing["base_unit"] != normalized_unit:
                    return {
                        "status": "unit_mismatch",
                        "food": None,
                        "expected_unit": existing["base_unit"],
                        "provided_unit": normalized_unit,
                    }
                await self.conn.execute(
                    "UPDATE foods SET name = ?, basis_amount = ?, calories = ?, "
                    "protein_g = ?, carbs_g = ?, fat_g = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND is_active = 1",
                    (
                        normalized_name,
                        normalized_basis,
                        *nutrients,
                        _utc_timestamp_now(),
                        existing["id"],
                        user_id,
                    ),
                )
                food = await self._get_food_by_id_locked(user_id, existing["id"])
                return {"status": "updated", "food": food}

            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS count FROM foods "
                "WHERE user_id = ? AND is_active = 1",
                (user_id,),
            )
            if (await cursor.fetchone())["count"] >= MAX_ACTIVE_FOODS:
                return {
                    "status": "limit",
                    "food": None,
                    "limit": MAX_ACTIVE_FOODS,
                }

            cursor = await self.conn.execute(
                "INSERT INTO foods "
                "(user_id, name, name_key, base_unit, basis_amount, calories, "
                "protein_g, carbs_g, fat_g, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    normalized_name,
                    name_key,
                    normalized_unit,
                    normalized_basis,
                    *nutrients,
                    _utc_timestamp_now(),
                ),
            )
            food = await self._get_food_by_id_locked(user_id, cursor.lastrowid)
            return {"status": "added", "food": food}

    async def _get_food_by_id_locked(
        self, user_id: int, food_id: int
    ) -> dict[str, Any] | None:
        """Return one active food while the caller owns any required lock."""
        cursor = await self.conn.execute(
            "SELECT * FROM foods "
            "WHERE id = ? AND user_id = ? AND is_active = 1",
            (food_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_food_by_key(
        self, user_id: int, key: str
    ) -> dict[str, Any] | None:
        """Return one active food by its normalized name key."""
        name_key = _catalog_key(key, "Food name", MAX_CATALOG_NAME_LENGTH)
        cursor = await self.conn.execute(
            "SELECT * FROM foods "
            "WHERE user_id = ? AND name_key = ? AND is_active = 1",
            (user_id, name_key),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_foods(self, user_id: int) -> list[dict[str, Any]]:
        """List a user's active foods in normalized-name order."""
        cursor = await self.conn.execute(
            "SELECT * FROM foods WHERE user_id = ? AND is_active = 1 "
            "ORDER BY name_key, id",
            (user_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_food_portions(
        self, user_id: int, food_id: int
    ) -> list[dict[str, Any]]:
        """List named portions for an active food owned by the user."""
        cursor = await self.conn.execute(
            "SELECT fp.*, f.base_unit AS food_base_unit "
            "FROM food_portions AS fp "
            "JOIN foods AS f ON f.id = fp.food_id AND f.user_id = fp.user_id "
            "WHERE fp.user_id = ? AND fp.food_id = ? AND f.is_active = 1 "
            "ORDER BY fp.name_key, fp.id",
            (user_id, food_id),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def _get_food_portion_locked(
        self, user_id: int, food_id: int, portion_id: int
    ) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT fp.*, f.base_unit AS food_base_unit "
            "FROM food_portions AS fp "
            "JOIN foods AS f ON f.id = fp.food_id AND f.user_id = fp.user_id "
            "WHERE fp.id = ? AND fp.user_id = ? AND fp.food_id = ?",
            (portion_id, user_id, food_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def save_food_portion(
        self,
        user_id: int,
        food_id: int,
        name: str,
        base_amount: float,
        base_unit: str,
    ) -> dict[str, Any]:
        """Add or update a named portion resolved to the food's base unit."""
        normalized_name = _normalize_catalog_text(
            name, "Portion name", MAX_PORTION_NAME_LENGTH
        )
        name_key = _catalog_key(name, "Portion name", MAX_PORTION_NAME_LENGTH)
        normalized_amount = _positive_catalog_amount(base_amount, "Base amount")
        normalized_unit = _catalog_unit(base_unit, FOOD_BASE_UNITS, "Base unit")

        async with self._write_operation():
            food = await self._get_food_by_id_locked(user_id, food_id)
            if food is None:
                return {"status": "not_found", "portion": None}
            if food["base_unit"] != normalized_unit:
                return {
                    "status": "unit_mismatch",
                    "portion": None,
                    "expected_unit": food["base_unit"],
                    "provided_unit": normalized_unit,
                }

            standard_portion = canonical_unit_alias(normalized_name)
            if standard_portion is not None:
                portion_unit, multiplier = standard_portion
                if portion_unit == food["base_unit"]:
                    raise ValueError(
                        "Portion name duplicates the food's standard base unit"
                    )
                # Store one canonical key for aliases such as piece/pieces/pcs.
                normalized_name = portion_unit
                name_key = portion_unit
                # ``base_amount`` was supplied per entered alias. Store the
                # mapping per one canonical unit so, for example, kg=1000ml
                # becomes g=1ml and 1kg later resolves to 1000ml.
                normalized_amount /= float(multiplier)
                normalized_amount = _positive_catalog_amount(
                    normalized_amount, "Canonical portion amount"
                )

            cursor = await self.conn.execute(
                "SELECT id FROM food_portions "
                "WHERE user_id = ? AND food_id = ? AND name_key = ?",
                (user_id, food_id, name_key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await self.conn.execute(
                    "UPDATE food_portions SET name = ?, base_amount = ?, "
                    "updated_at = ? WHERE id = ? AND user_id = ? AND food_id = ?",
                    (
                        normalized_name,
                        normalized_amount,
                        _utc_timestamp_now(),
                        existing["id"],
                        user_id,
                        food_id,
                    ),
                )
                portion = await self._get_food_portion_locked(
                    user_id, food_id, existing["id"]
                )
                return {"status": "updated", "portion": portion}

            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS count FROM food_portions "
                "WHERE user_id = ? AND food_id = ?",
                (user_id, food_id),
            )
            if (await cursor.fetchone())["count"] >= MAX_PORTIONS_PER_FOOD:
                return {
                    "status": "limit",
                    "portion": None,
                    "limit": MAX_PORTIONS_PER_FOOD,
                }

            cursor = await self.conn.execute(
                "INSERT INTO food_portions "
                "(user_id, food_id, name, name_key, base_amount, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    food_id,
                    normalized_name,
                    name_key,
                    normalized_amount,
                    _utc_timestamp_now(),
                ),
            )
            portion = await self._get_food_portion_locked(
                user_id, food_id, cursor.lastrowid
            )
            return {"status": "added", "portion": portion}

    async def remove_food_portion(
        self, user_id: int, food_id: int, portion_id: int
    ) -> dict[str, Any]:
        """Remove an owned named portion."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "DELETE FROM food_portions "
                "WHERE id = ? AND food_id = ? AND user_id = ?",
                (portion_id, food_id, user_id),
            )
            if cursor.rowcount <= 0:
                return {"status": "not_found", "portion_id": None}
            return {
                "status": "updated",
                "action": "removed",
                "portion_id": portion_id,
            }

    async def archive_food(self, user_id: int, food_id: int) -> dict[str, Any]:
        """Soft-delete an active food without invalidating recipe history.

        Orphaned ``food_portions`` are removed so that re-creating the food
        under a new ID starts with a clean portion count.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                "UPDATE foods SET is_active = 0, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND is_active = 1",
                (_utc_timestamp_now(), food_id, user_id),
            )
            if cursor.rowcount <= 0:
                return {"status": "not_found", "food_id": None}
            await self.conn.execute(
                "DELETE FROM food_portions WHERE food_id = ? AND user_id = ?",
                (food_id, user_id),
            )
            return {
                "status": "updated",
                "action": "archived",
                "food_id": food_id,
            }

    # -------------------------------------------------------------------
    # Recipe catalog
    # -------------------------------------------------------------------
    async def save_recipe(
        self,
        user_id: int,
        name: str,
        yield_amount: float,
        yield_unit: str,
    ) -> dict[str, Any]:
        """Add or update an active recipe, keyed by normalized name."""
        normalized_name = _normalize_catalog_text(
            name, "Recipe name", MAX_CATALOG_NAME_LENGTH
        )
        name_key = _catalog_key(name, "Recipe name", MAX_CATALOG_NAME_LENGTH)
        normalized_amount = _positive_catalog_amount(yield_amount, "Yield amount")
        normalized_unit = _catalog_unit(
            yield_unit, RECIPE_YIELD_UNITS, "Yield unit"
        )

        async with self._write_operation():
            cursor = await self.conn.execute(
                "SELECT id FROM recipes "
                "WHERE user_id = ? AND name_key = ? AND is_active = 1",
                (user_id, name_key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await self.conn.execute(
                    "UPDATE recipes SET name = ?, yield_amount = ?, "
                    "yield_unit = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND is_active = 1",
                    (
                        normalized_name,
                        normalized_amount,
                        normalized_unit,
                        _utc_timestamp_now(),
                        existing["id"],
                        user_id,
                    ),
                )
                recipe = await self._get_recipe_by_id_locked(
                    user_id, existing["id"]
                )
                return {"status": "updated", "recipe": recipe}

            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS count FROM recipes "
                "WHERE user_id = ? AND is_active = 1",
                (user_id,),
            )
            if (await cursor.fetchone())["count"] >= MAX_ACTIVE_RECIPES:
                return {
                    "status": "limit",
                    "recipe": None,
                    "limit": MAX_ACTIVE_RECIPES,
                }

            cursor = await self.conn.execute(
                "INSERT INTO recipes "
                "(user_id, name, name_key, yield_amount, yield_unit, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    normalized_name,
                    name_key,
                    normalized_amount,
                    normalized_unit,
                    _utc_timestamp_now(),
                ),
            )
            recipe = await self._get_recipe_by_id_locked(user_id, cursor.lastrowid)
            return {"status": "added", "recipe": recipe}

    async def _get_recipe_by_id_locked(
        self, user_id: int, recipe_id: int
    ) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT * FROM recipes "
            "WHERE id = ? AND user_id = ? AND is_active = 1",
            (recipe_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_recipe_by_key(
        self, user_id: int, key: str
    ) -> dict[str, Any] | None:
        """Return one active recipe by its normalized name key."""
        name_key = _catalog_key(key, "Recipe name", MAX_CATALOG_NAME_LENGTH)
        cursor = await self.conn.execute(
            "SELECT * FROM recipes "
            "WHERE user_id = ? AND name_key = ? AND is_active = 1",
            (user_id, name_key),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_recipes(self, user_id: int) -> list[dict[str, Any]]:
        """List a user's active recipes in normalized-name order."""
        cursor = await self.conn.execute(
            "SELECT * FROM recipes WHERE user_id = ? AND is_active = 1 "
            "ORDER BY name_key, id",
            (user_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_recipe_ingredients(
        self, user_id: int, recipe_id: int
    ) -> list[dict[str, Any]]:
        """Return active-recipe ingredients with food nutrition fields."""
        cursor = await self.conn.execute(
            "SELECT ri.*, f.name AS food_name, f.name_key AS food_key, "
            "f.base_unit AS food_base_unit, "
            "f.basis_amount AS food_basis_amount, "
            "f.calories AS food_calories, f.protein_g AS food_protein_g, "
            "f.carbs_g AS food_carbs_g, f.fat_g AS food_fat_g, "
            "f.is_active AS food_is_active "
            "FROM recipe_ingredients AS ri "
            "JOIN recipes AS r "
            "ON r.id = ri.recipe_id AND r.user_id = ri.user_id "
            "JOIN foods AS f ON f.id = ri.food_id AND f.user_id = ri.user_id "
            "WHERE ri.user_id = ? AND ri.recipe_id = ? AND r.is_active = 1 "
            "ORDER BY ri.id",
            (user_id, recipe_id),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def _get_recipe_ingredient_locked(
        self, user_id: int, recipe_id: int, ingredient_id: int
    ) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT ri.*, f.name AS food_name, f.name_key AS food_key, "
            "f.base_unit AS food_base_unit, "
            "f.basis_amount AS food_basis_amount, "
            "f.calories AS food_calories, f.protein_g AS food_protein_g, "
            "f.carbs_g AS food_carbs_g, f.fat_g AS food_fat_g, "
            "f.is_active AS food_is_active "
            "FROM recipe_ingredients AS ri "
            "JOIN foods AS f ON f.id = ri.food_id AND f.user_id = ri.user_id "
            "WHERE ri.id = ? AND ri.user_id = ? AND ri.recipe_id = ?",
            (ingredient_id, user_id, recipe_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def save_recipe_ingredient(
        self,
        user_id: int,
        recipe_id: int,
        food_id: int,
        base_amount: float,
        base_unit: str,
        display_amount: float,
        display_unit: str,
    ) -> dict[str, Any]:
        """Add or update one food in a recipe using its resolved base amount."""
        normalized_base_amount = _positive_catalog_amount(
            base_amount, "Base amount"
        )
        normalized_base_unit = _catalog_unit(
            base_unit, FOOD_BASE_UNITS, "Base unit"
        )
        normalized_display_amount = _positive_catalog_amount(
            display_amount, "Display amount"
        )
        normalized_display_unit = _normalize_catalog_text(
            display_unit, "Display unit", MAX_DISPLAY_UNIT_LENGTH
        )

        async with self._write_operation():
            recipe = await self._get_recipe_by_id_locked(user_id, recipe_id)
            food = await self._get_food_by_id_locked(user_id, food_id)
            if recipe is None or food is None:
                return {"status": "not_found", "ingredient": None}
            if food["base_unit"] != normalized_base_unit:
                return {
                    "status": "unit_mismatch",
                    "ingredient": None,
                    "expected_unit": food["base_unit"],
                    "provided_unit": normalized_base_unit,
                }

            # An archived food name may be recreated with a new stable ID.
            # Treat that as the same logical recipe slot so re-adding it
            # replaces the archived reference instead of double-counting it.
            cursor = await self.conn.execute(
                "DELETE FROM recipe_ingredients "
                "WHERE user_id = ? AND recipe_id = ? AND food_id <> ? "
                "AND food_id IN ("
                "SELECT id FROM foods WHERE user_id = ? AND name_key = ?"
                ")",
                (
                    user_id,
                    recipe_id,
                    food_id,
                    user_id,
                    food["name_key"],
                ),
            )
            replaced_archived_reference = cursor.rowcount > 0

            cursor = await self.conn.execute(
                "SELECT id FROM recipe_ingredients "
                "WHERE user_id = ? AND recipe_id = ? AND food_id = ?",
                (user_id, recipe_id, food_id),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await self.conn.execute(
                    "UPDATE recipe_ingredients SET base_amount = ?, "
                    "display_amount = ?, display_unit = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND recipe_id = ?",
                    (
                        normalized_base_amount,
                        normalized_display_amount,
                        normalized_display_unit,
                        _utc_timestamp_now(),
                        existing["id"],
                        user_id,
                        recipe_id,
                    ),
                )
                ingredient = await self._get_recipe_ingredient_locked(
                    user_id, recipe_id, existing["id"]
                )
                return {"status": "updated", "ingredient": ingredient}

            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS count FROM recipe_ingredients "
                "WHERE user_id = ? AND recipe_id = ?",
                (user_id, recipe_id),
            )
            if (await cursor.fetchone())["count"] >= MAX_INGREDIENTS_PER_RECIPE:
                return {
                    "status": "limit",
                    "ingredient": None,
                    "limit": MAX_INGREDIENTS_PER_RECIPE,
                }

            cursor = await self.conn.execute(
                "INSERT INTO recipe_ingredients "
                "(user_id, recipe_id, food_id, base_amount, display_amount, "
                "display_unit, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    recipe_id,
                    food_id,
                    normalized_base_amount,
                    normalized_display_amount,
                    normalized_display_unit,
                    _utc_timestamp_now(),
                ),
            )
            ingredient = await self._get_recipe_ingredient_locked(
                user_id, recipe_id, cursor.lastrowid
            )
            status = "updated" if replaced_archived_reference else "added"
            return {"status": status, "ingredient": ingredient}

    async def remove_recipe_ingredient(
        self, user_id: int, recipe_id: int, ingredient_id: int
    ) -> dict[str, Any]:
        """Remove an owned ingredient from an owned recipe."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "DELETE FROM recipe_ingredients "
                "WHERE id = ? AND recipe_id = ? AND user_id = ?",
                (ingredient_id, recipe_id, user_id),
            )
            if cursor.rowcount <= 0:
                return {"status": "not_found", "ingredient_id": None}
            return {
                "status": "updated",
                "action": "removed",
                "ingredient_id": ingredient_id,
            }

    async def archive_recipe(
        self, user_id: int, recipe_id: int
    ) -> dict[str, Any]:
        """Soft-delete an active recipe."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "UPDATE recipes SET is_active = 0, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND is_active = 1",
                (_utc_timestamp_now(), recipe_id, user_id),
            )
            if cursor.rowcount <= 0:
                return {"status": "not_found", "recipe_id": None}
            return {
                "status": "updated",
                "action": "archived",
                "recipe_id": recipe_id,
            }

    # -------------------------------------------------------------------
    # Habits
    # -------------------------------------------------------------------
    async def add_habit(
        self, user_id: int, habit_name: str
    ) -> tuple[int, HabitAddStatus]:
        """Add, reactivate, or find an active habit.

        Returns ``(habit_id, status)`` where status is ``"added"``,
        ``"reactivated"``, or ``"already_active"``.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                "SELECT id, is_active FROM habits "
                "WHERE user_id = ? AND habit_name = ? "
                "ORDER BY is_active DESC, id LIMIT 1",
                (user_id, habit_name),
            )
            row = await cursor.fetchone()
            if row is not None and row["is_active"]:
                return row["id"], "already_active"

            if row is not None:
                cursor = await self.conn.execute(
                    """
                    UPDATE habits
                    SET is_active = 1
                    WHERE id = ?
                      AND is_active = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM habits
                          WHERE user_id = ? AND habit_name = ? AND is_active = 1
                      )
                    """,
                    (row["id"], user_id, habit_name),
                )
                if cursor.rowcount > 0:
                    return row["id"], "reactivated"

            cursor = await self.conn.execute(
                """
                INSERT INTO habits (user_id, habit_name)
                VALUES (?, ?)
                ON CONFLICT(user_id, habit_name) WHERE is_active = 1 DO NOTHING
                RETURNING id
                """,
                (user_id, habit_name),
            )
            inserted = await cursor.fetchone()
            if inserted is not None:
                return inserted["id"], "added"

            # This can occur if another database connection made the habit
            # active between this operation's read and write.
            cursor = await self.conn.execute(
                "SELECT id FROM habits "
                "WHERE user_id = ? AND habit_name = ? AND is_active = 1",
                (user_id, habit_name),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("Habit add completed without an active habit")
            return active["id"], "already_active"

    async def deactivate_habit(self, user_id: int, habit_id: int) -> bool:
        """Soft-delete a habit. Returns True if a row was affected."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "UPDATE habits SET is_active = 0 "
                "WHERE id = ? AND user_id = ? AND is_active = 1",
                (habit_id, user_id),
            )
            return cursor.rowcount > 0

    async def get_active_habits(self, user_id: int) -> list[dict[str, Any]]:
        """Get all active habits for a user."""
        cursor = await self.conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def check_habit(self, user_id: int, habit_id: int, log_date: date) -> bool:
        """Mark a habit as done for a specific local date.

        Returns True if inserted. Returns False if the row already exists or if
        the habit is inactive, missing, or owned by another user.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                """
                INSERT INTO habit_logs (user_id, habit_id, log_date)
                SELECT ?, h.id, ?
                FROM habits AS h
                JOIN users AS u ON u.user_id = h.user_id
                WHERE h.id = ? AND h.user_id = ? AND h.is_active = 1
                ON CONFLICT(user_id, habit_id, log_date) DO NOTHING
                """,
                (user_id, log_date.isoformat(), habit_id, user_id),
            )
            return cursor.rowcount > 0

    async def uncheck_habit(self, user_id: int, habit_id: int, log_date: date) -> bool:
        """Remove a habit check for a specific local date.

        Returns True if a row was deleted.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                "DELETE FROM habit_logs "
                "WHERE user_id = ? AND habit_id = ? AND log_date = ?",
                (user_id, habit_id, log_date.isoformat()),
            )
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
    ) -> list[dict[str, Any]]:
        """Get habit logs in a date range (inclusive)."""
        cursor = await self.conn.execute(
            "SELECT * FROM habit_logs WHERE user_id = ? AND log_date >= ? AND log_date <= ? "
            "ORDER BY log_date",
            (user_id, start_date.isoformat(), end_date.isoformat()),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_streak(self, user_id: int, habit_id: int, today: date) -> int:
        """Calculate current streak for a habit.

        Streak = number of consecutive days with a row in habit_logs,
        counting backward from today.  If today is not checked, streak is 0.
        The query is bounded to 366 rows so that long-running habits don't
        load unbounded history into memory.
        """
        cursor = await self.conn.execute(
            "SELECT log_date FROM habit_logs "
            "WHERE user_id = ? AND habit_id = ? AND log_date <= ? "
            "ORDER BY log_date DESC LIMIT 366",
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
    _UNDO_TABLES: frozenset[str] = frozenset(
        {"study_logs", "gym_logs", "diet_logs"}
    )

    async def undo_last(self, user_id: int) -> dict[str, Any] | None:
        """Delete the most recent log entry for this user (within 24h).

        Checks study_logs, gym_logs, diet_logs. habit_logs use a different
        undo path (uncheck_habit).

        Returns a dict with the deleted entry info, or None if nothing found.
        """
        async with self._write_operation():
            return await self._undo_last_locked(user_id)

    async def _undo_last_locked(self, user_id: int) -> dict[str, Any] | None:
        """Select and delete the latest entry while the write lock is held."""
        tables = [
            ("study_logs", "📖 Study"),
            ("gym_logs", "🏋️ Gym"),
            ("diet_logs", "🍽️ Diet"),
        ]
        latest: dict[str, Any] | None = None
        latest_table: str | None = None
        latest_dt: datetime | None = None

        for table_name, label in tables:
            assert table_name in self._UNDO_TABLES  # guard against injection
            cursor = await self.conn.execute(
                f"SELECT *, '{label}' as category FROM {table_name} "  # noqa: S608
                f"WHERE user_id = ? ORDER BY logged_at DESC, id DESC LIMIT 1",
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

            if latest_dt is None or logged_at > latest_dt:
                latest = dict(row)
                latest_table = table_name
                latest_dt = logged_at

        if latest is None or latest_table is None:
            return None

        assert latest_table in self._UNDO_TABLES  # guard against injection
        cursor = await self.conn.execute(
            f"DELETE FROM {latest_table} WHERE id = ? AND user_id = ?",  # noqa: S608
            (latest["id"], user_id),
        )
        if cursor.rowcount <= 0:
            return None
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
