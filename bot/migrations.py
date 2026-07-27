"""Versioned, non-destructive SQLite migrations for the Ledger bot.

Schema evolution is driven by ``PRAGMA user_version``. Version 0 is treated as an
*unknown legacy shape* — the pre-versioning databases never stamped a version, so
the baseline migration inspects existing columns and indexes (``IF NOT EXISTS`` and
column probes) rather than assuming an empty database.

Design guarantees:

* **Non-destructive.** Migrations never delete, merge, deactivate, or reassign a
  user's rows. When data needs an explicit human decision (e.g. two active habits
  that collapse to one normalized key under the new unique index), the migration
  stops with :class:`MigrationCollisionError` and makes no change.
* **Atomic per version.** Each migration runs inside one explicit
  ``BEGIN IMMEDIATE`` … ``COMMIT`` transaction and stamps ``user_version`` only on
  success, so a forced failure rolls the whole step back. ``executescript`` is
  avoided because it commits implicitly and would break that rollback.
* **Re-entrant.** After a crash the runner resumes from the last committed version.
* **Sanitized.** Diagnostics carry only counts — never a token, username, first
  name, or raw Telegram ID.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import aiosqlite

from .database import _habit_key

logger = logging.getLogger(__name__)

# Bump this (and register a new function in ``_MIGRATIONS``) for every schema
# change. Version N is produced by ``_MIGRATIONS[N]``.
LATEST_VERSION = 5


class MigrationCollisionError(RuntimeError):
    """A migration needs an explicit human decision before it can proceed.

    Raised — with a sanitized, count-only message — instead of silently mutating
    ambiguous data. The transaction is rolled back, leaving the prior schema
    version usable.
    """


class UnsupportedSchemaError(RuntimeError):
    """The database is at a schema version this binary does not understand.

    Raised when ``user_version`` exceeds :data:`LATEST_VERSION` — i.e. an older
    binary was started against a database a newer binary already migrated forward.
    Serving would run old SQL assumptions against an unknown schema, so startup
    aborts instead.
    """


# ---------------------------------------------------------------------------
# Baseline DDL (moved verbatim from database.py). Kept as individual statements
# so each runs inside the migration transaction; executescript would commit.
# ---------------------------------------------------------------------------
_BASE_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        first_name  TEXT,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_logs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        subject      TEXT NOT NULL,
        duration_min INTEGER NOT NULL,
        notes        TEXT,
        logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gym_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        exercise    TEXT NOT NULL,
        sets        INTEGER NOT NULL,
        reps        INTEGER NOT NULL,
        weight_kg   REAL,
        logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habits (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        habit_name  TEXT NOT NULL,
        name_key    TEXT,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active   INTEGER DEFAULT 1,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habit_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        habit_id    INTEGER NOT NULL,
        log_date    DATE NOT NULL,
        UNIQUE(user_id, habit_id, log_date),
        FOREIGN KEY (user_id) REFERENCES users(user_id),
        FOREIGN KEY (habit_id) REFERENCES habits(id)
    )
    """,
)

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_study_user_date ON study_logs(user_id, logged_at)",
    "CREATE INDEX IF NOT EXISTS idx_gym_user_date ON gym_logs(user_id, logged_at)",
    "CREATE INDEX IF NOT EXISTS idx_diet_user_date ON diet_logs(user_id, logged_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_foods_active_name "
    "ON foods(user_id, name_key) WHERE is_active = 1",
    "CREATE INDEX IF NOT EXISTS idx_food_portions_lookup "
    "ON food_portions(user_id, food_id, name_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_active_name "
    "ON recipes(user_id, name_key) WHERE is_active = 1",
    "CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_lookup "
    "ON recipe_ingredients(user_id, recipe_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_habit_logs_user_date ON habit_logs(user_id, log_date)",
)

# Partial unique index: only active habits must have a unique case-insensitive key
# per user. Keying on name_key (not the display name) means "Read" and "read"
# collide, so reactivation restores the original habit and its streak.
_PARTIAL_HABIT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_active_key "
    "ON habits(user_id, name_key) WHERE is_active = 1"
)

# Existing databases cannot gain a new foreign key without rebuilding the table.
# These triggers enforce the same ownership and active-habit invariant for both
# existing and newly-created databases, without invalidating historical rows.
_HABIT_LOG_TRIGGERS: tuple[str, ...] = (
    """
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
    END
    """,
    """
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
    END
    """,
)

# ``CREATE TABLE IF NOT EXISTS`` does not add columns to an existing table, so a
# pre-macro diet_logs needs these added explicitly. Kept simple so SQLite adds
# them without rebuilding the table or touching existing rows.
_DIET_MACRO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("protein_g", "REAL"),
    ("carbs_g", "REAL"),
    ("fat_g", "REAL"),
)

# Columns the runtime unconditionally depends on. ``CREATE TABLE IF NOT EXISTS``
# only creates *absent* tables — it never adds a missing column to a legacy table
# — so a version-0 database with an under-specified core table would otherwise be
# stamped current and then crash at runtime (e.g. /start needs users.first_name).
# The baseline verifies these are present and fails closed on any unknown shape.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("user_id", "username", "first_name"),
    "study_logs": ("user_id", "subject", "duration_min", "notes", "logged_at"),
    "gym_logs": ("user_id", "exercise", "sets", "reps", "weight_kg", "logged_at"),
    "diet_logs": (
        "user_id", "meal_type", "food_items", "calories",
        "protein_g", "carbs_g", "fat_g", "logged_at",
    ),
    "habits": ("user_id", "habit_name", "name_key", "is_active", "created_at"),
    "habit_logs": ("user_id", "habit_id", "log_date"),
}


# ---------------------------------------------------------------------------
# Reusable schema steps (also called by targeted tests via DatabaseManager)
# ---------------------------------------------------------------------------
async def add_missing_diet_macro_columns(conn: aiosqlite.Connection) -> None:
    """Add nullable macro columns to a pre-macro ``diet_logs`` table."""
    cursor = await conn.execute("PRAGMA table_info(diet_logs)")
    existing = {row["name"] for row in await cursor.fetchall()}
    for column_name, column_type in _DIET_MACRO_COLUMNS:
        if column_name not in existing:
            await conn.execute(
                f"ALTER TABLE diet_logs ADD COLUMN {column_name} {column_type}"  # noqa: S608
            )


async def backfill_habit_name_keys(conn: aiosqlite.Connection) -> None:
    """Add and backfill ``habits.name_key`` and retire the legacy name index.

    Existing databases keyed active-habit uniqueness on the exact display name;
    this switches to a case/format-insensitive key so reactivation restores the
    original habit. Unlike the old startup path, this does **not** collapse active
    duplicates — that decision is surfaced by :func:`detect_active_habit_collisions`.
    """
    cursor = await conn.execute("PRAGMA table_info(habits)")
    columns = {row["name"] for row in await cursor.fetchall()}
    if "name_key" not in columns:
        await conn.execute("ALTER TABLE habits ADD COLUMN name_key TEXT")

    cursor = await conn.execute(
        "SELECT id, habit_name FROM habits WHERE name_key IS NULL OR name_key = ''"
    )
    for row in await cursor.fetchall():
        await conn.execute(
            "UPDATE habits SET name_key = ? WHERE id = ?",
            (_habit_key(row["habit_name"]), row["id"]),
        )

    # Retire the legacy exact-name partial index before the keyed one is created.
    await conn.execute("DROP INDEX IF EXISTS idx_habits_active")


async def verify_baseline_integrity(conn: aiosqlite.Connection) -> None:
    """Fail closed on a legacy shape the migrator cannot safely certify.

    Run at the end of the baseline migration, inside its transaction, so any
    failure rolls the whole step back and leaves ``user_version`` unchanged.
    Checks three invariants, all with sanitized (schema-name / count-only)
    diagnostics that never echo a Telegram ID, username, or first name:

    * every core table has the columns the runtime requires;
    * ``foreign_key_check`` finds no dangling reference;
    * every ``habit_logs`` row belongs to a habit owned by the same user
      (triggers only guard *future* writes, so pre-existing cross-owner or
      orphan rows must be caught here).
    """
    for table, required in _REQUIRED_COLUMNS.items():
        cursor = await conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
        present = {row["name"] for row in await cursor.fetchall()}
        missing = sorted(column for column in required if column not in present)
        if missing:
            raise MigrationCollisionError(
                f"table {table!r} is missing required column(s) {missing}; this "
                "legacy schema is not one the migrator can safely upgrade. No "
                "changes were made."
            )

    cursor = await conn.execute("PRAGMA foreign_key_check")
    fk_problems = await cursor.fetchall()
    if fk_problems:
        raise MigrationCollisionError(
            f"foreign_key_check found {len(fk_problems)} violating row(s); refusing "
            "to certify a schema with dangling references. No changes were made."
        )

    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM habit_logs AS hl
        LEFT JOIN habits AS h ON h.id = hl.habit_id
        WHERE h.id IS NULL OR h.user_id != hl.user_id
        """
    )
    row = await cursor.fetchone()
    cross_owner = row["n"] if row else 0
    if cross_owner:
        raise MigrationCollisionError(
            f"{cross_owner} habit_log row(s) reference a missing or cross-owner "
            "habit; resolve these manually before migrating. No changes were made."
        )


async def detect_active_habit_collisions(conn: aiosqlite.Connection) -> list[int]:
    """Return the size of each active-habit group that shares a normalized key.

    A non-empty result means the new unique index cannot be created without
    deactivating rows — a decision the migration refuses to make on its own. Only
    counts are returned so callers can log without leaking identifiers.
    """
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM habits
        WHERE is_active = 1
        GROUP BY user_id, name_key
        HAVING n > 1
        """
    )
    return [row["n"] for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
async def _migration_0001_baseline(conn: aiosqlite.Connection) -> None:
    """Bring any legacy or fresh database up to the current baseline schema.

    Supports a fresh database, the current production-shaped schema, the
    pre-diet-macro schema, and the pre-habit-key schema.
    """
    # 1. Base tables (no-op where they already exist).
    for statement in _BASE_TABLES:
        await conn.execute(statement)

    # 2. Pre-macro diet_logs: add nullable macro columns.
    await add_missing_diet_macro_columns(conn)

    # 3. Pre-habit-key habits: add + backfill name_key, retire the legacy index.
    await backfill_habit_name_keys(conn)

    # 4. Refuse to proceed if the unique index would have to collapse duplicates.
    collisions = await detect_active_habit_collisions(conn)
    if collisions:
        extra = sum(size - 1 for size in collisions)
        raise MigrationCollisionError(
            f"{len(collisions)} normalized habit-name group(s) contain {extra} "
            "extra active row(s) that the unique index cannot hold. Resolve these "
            "manually before migrating; no changes were made."
        )

    # 5. Ordinary and partial-unique indexes.
    for statement in _INDEXES:
        await conn.execute(statement)
    await conn.execute(_PARTIAL_HABIT_INDEX)

    # 6. Ownership / active-habit enforcement triggers.
    for statement in _HABIT_LOG_TRIGGERS:
        await conn.execute(statement)

    # 7. Fail closed on any legacy shape or ownership violation we cannot certify
    #    (missing required column, dangling FK, cross-owner habit log). Runs last,
    #    inside this transaction, so a failure rolls the whole baseline back.
    await verify_baseline_integrity(conn)


async def _migration_0002_mutation_receipts(conn: aiosqlite.Connection) -> None:
    """Add ``mutation_receipts`` for at-least-once → exactly-once idempotency.

    Keyed on ``(telegram_update_id, operation_key)``. Telegram message IDs are
    only unique within a chat, so idempotency never keys on ``message_id`` alone;
    the owner ``user_id`` is stored and verified on replay so a receipt can never
    cross a tenant boundary.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mutation_receipts (
            telegram_update_id INTEGER NOT NULL,
            operation_key      TEXT NOT NULL,
            user_id            INTEGER NOT NULL,
            chat_id            INTEGER,
            message_id         INTEGER,
            entity_type        TEXT NOT NULL,
            entity_id          INTEGER NOT NULL,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (telegram_update_id, operation_key),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mutation_receipts_user "
        "ON mutation_receipts(user_id, created_at)"
    )


async def _migration_0003_user_settings(conn: aiosqlite.Connection) -> None:
    """Add per-user ``user_settings`` (reminder opt-in, optional routine profile).

    Existing users are backfilled with ``reminders_enabled = 1`` to preserve the
    current global behavior; new users default to opt-in (disabled) at the
    application layer. A settings row is never a grant of access — authentication
    stays in ``.env``.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id           INTEGER PRIMARY KEY,
            reminders_enabled INTEGER NOT NULL DEFAULT 1 CHECK(reminders_enabled IN (0, 1)),
            routine_profile   TEXT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    # Preserve current behavior: every existing user keeps receiving reminders.
    await conn.execute(
        """
        INSERT INTO user_settings (user_id, reminders_enabled)
        SELECT user_id, 1 FROM users
        WHERE user_id NOT IN (SELECT user_id FROM user_settings)
        """
    )


def _local_start_date(created_at: str | None) -> str:
    """Local ISO date for a habit's stored UTC ``created_at``.

    Falls back to today's local date when the timestamp is missing or
    unparseable — matching the old ``date('now')`` fallback but in local time.
    """
    from datetime import datetime, timezone

    from .config import local_date_from_utc, today_local

    if created_at:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(created_at, fmt).replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                continue
            return local_date_from_utc(parsed).isoformat()
    return today_local().isoformat()


async def _migration_0004_habit_activity_periods(conn: aiosqlite.Connection) -> None:
    """Add ``habit_activity_periods`` so adherence reflects real activation spans.

    Backfills one *open* period per currently-active habit, starting on its
    creation date. Historical inactive periods cannot be recovered, so they are
    not invented — a habit inactive at migration time simply has no period until
    it is next activated.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS habit_activity_periods (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            habit_id   INTEGER NOT NULL,
            started_on DATE NOT NULL,
            ended_on   DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (ended_on IS NULL OR ended_on >= started_on),
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (habit_id) REFERENCES habits(id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_habit_periods_lookup "
        "ON habit_activity_periods(user_id, habit_id, started_on)"
    )
    # Compute the activation start in the *configured local* timezone, not UTC.
    # ``created_at`` is stored as UTC; truncating it with substr() would shift the
    # start back a local day for habits created late in the UTC day (e.g. 20:00
    # UTC is already the next day in Asia/Kolkata), inflating the adherence
    # denominator. Do the conversion in Python with the same helper the runtime
    # uses so the boundary is handled identically.
    cursor = await conn.execute(
        """
        SELECT user_id, id, created_at
        FROM habits
        WHERE is_active = 1
          AND id NOT IN (SELECT habit_id FROM habit_activity_periods)
        """
    )
    for row in await cursor.fetchall():
        started_on = _local_start_date(row["created_at"])
        await conn.execute(
            "INSERT INTO habit_activity_periods (user_id, habit_id, started_on) "
            "VALUES (?, ?, ?)",
            (row["user_id"], row["id"], started_on),
        )


async def _migration_0005_reminder_deliveries(conn: aiosqlite.Connection) -> None:
    """Add ``reminder_deliveries`` for durable, resumable reminder chunks.

    Keyed on ``(user_id, job_key, local_date, chunk_index)`` so a restart or a
    duplicate job run skips already-delivered chunks and resumes at the first
    undelivered one. Stores only a sanitized error *category* — never the token
    or a raw exception/URL.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_deliveries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            job_key         TEXT NOT NULL,
            local_date      DATE NOT NULL,
            chunk_index     INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'delivered', 'failed')),
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMP,
            delivered_at    TIMESTAMP,
            error_category  TEXT,
            UNIQUE(user_id, job_key, local_date, chunk_index),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_lookup "
        "ON reminder_deliveries(user_id, job_key, local_date)"
    )


_MIGRATIONS: dict[int, Callable[[aiosqlite.Connection], Awaitable[None]]] = {
    1: _migration_0001_baseline,
    2: _migration_0002_mutation_receipts,
    3: _migration_0003_user_settings,
    4: _migration_0004_habit_activity_periods,
    5: _migration_0005_reminder_deliveries,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def get_user_version(conn: aiosqlite.Connection) -> int:
    """Return the database's ``PRAGMA user_version``."""
    cursor = await conn.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    return int(row[0])


async def run_migrations(conn: aiosqlite.Connection) -> int:
    """Apply every pending migration in order and return the final version.

    Each migration runs in its own explicit transaction and stamps
    ``user_version`` only on success, so a failure rolls that step back and leaves
    the previous version usable. The caller must hold the connection lock.
    """
    if conn.in_transaction:
        await conn.commit()

    current = await get_user_version(conn)
    if current > LATEST_VERSION:
        # An older binary against a newer database: fail closed rather than serve
        # traffic against a schema whose invariants this code does not know.
        raise UnsupportedSchemaError(
            f"Database schema version {current} is newer than this binary supports "
            f"({LATEST_VERSION}). Deploy the matching (or newer) application version; "
            "refusing to run against an unknown schema."
        )
    if current == LATEST_VERSION:
        return current

    for target in range(current + 1, LATEST_VERSION + 1):
        migration = _MIGRATIONS[target]
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await migration(conn)
            # target comes from range() over ints, so this cannot be injected.
            await conn.execute(f"PRAGMA user_version = {int(target)}")  # noqa: S608
            await conn.commit()
        except BaseException:
            if conn.in_transaction:
                await conn.rollback()
            raise
        logger.info("Applied migration to schema version %d", target)

    return LATEST_VERSION
