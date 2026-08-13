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

from ledger_schema import (
    LATEST_SCHEMA_VERSION,
    required_columns_for,
    required_tables_for,
)

from .database import _habit_key

logger = logging.getLogger(__name__)

# Compatibility alias for the one source of truth in ``ledger_schema``. Bump the
# version there and register the new function in ``_MIGRATIONS``; version N is
# produced by ``_MIGRATIONS[N]``. Tests monkeypatch this module attribute to pin
# an older effective latest, so it stays a module-level name.
LATEST_VERSION = LATEST_SCHEMA_VERSION


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


class SchemaVerificationError(RuntimeError):
    """A database's shape does not match its stamped version.

    Raised — with sanitized, count/schema-name-only diagnostics — when the
    post-migration verifier finds a required table/column missing or an
    integrity/foreign-key violation. A version number is an input to
    verification, not proof of correctness: a corrupt, partially restored, or
    mis-stamped database must fail closed at startup rather than crash on the
    first user interaction.
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


# The table-introduction mapping lives in the dependency-free root module
# ``ledger_schema`` so the standalone backup tool verifies a copy against exactly
# the table list the bot enforces at startup. ``required_tables_for(version)``
# requires a table only once the version being checked has reached its
# introduction, so an intermediate-version database (a pre-migration rollback
# point, or a test pinning an older effective latest) is never asked for objects
# a later migration will add.

# The per-version column manifest lives alongside the table map in
# ``ledger_schema`` so the standalone backup tool certifies a copy against
# exactly the columns the bot enforces at startup. It used to live here and
# stopped at v8, which let a v10 database that had lost both AI-consent columns
# verify as sound — a table list alone cannot detect a column-only migration.


async def verify_current_schema(conn: aiosqlite.Connection) -> None:
    """Fail closed if the database shape does not match the effective latest version.

    Runs on every startup — including when ``user_version`` already equals
    LATEST — because the version stamp alone does not prove the schema is intact.
    Only objects introduced at or before the effective ``LATEST_VERSION`` are
    required. Diagnostics are sanitized (schema names and counts only) and never
    echo row data.
    """
    target = LATEST_VERSION
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    present = {row["name"] for row in await cursor.fetchall()}
    missing = [table for table in required_tables_for(target) if table not in present]
    if missing:
        raise SchemaVerificationError(
            f"Database is missing {len(missing)} required table(s): "
            f"{', '.join(sorted(missing))}."
        )

    for table, columns in required_columns_for(target).items():
        cursor = await conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
        actual = {row["name"] for row in await cursor.fetchall()}
        absent = sorted(column for column in columns if column not in actual)
        if absent:
            raise SchemaVerificationError(
                f"Table '{table}' is missing column(s): {', '.join(absent)}."
            )

    cursor = await conn.execute("PRAGMA integrity_check")
    row = await cursor.fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise SchemaVerificationError("Database integrity_check did not return ok.")

    cursor = await conn.execute("PRAGMA foreign_key_check")
    violations = await cursor.fetchall()
    if violations:
        raise SchemaVerificationError(
            f"Database foreign_key_check found {len(violations)} violation(s)."
        )


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


async def _migration_0006_diet_log_items(conn: aiosqlite.Connection) -> None:
    """Add ``diet_log_items`` — structured, snapshotted items beneath one meal.

    A ``diet_logs`` row remains the meal header (one row = one meal, so analytics
    still count meals correctly). Tap-logged meals additionally get one child row
    per selected food/recipe, carrying its source identity, entered and resolved
    quantity, and a nutrient snapshot frozen at save time. Existing free-text and
    quick ``/diet`` rows stay valid with zero child items and are never parsed or
    auto-linked.

    The composite ``(diet_log_id, user_id)`` foreign key needs its parent key to
    be unique, so a unique index is added on ``diet_logs(id, user_id)`` first (the
    table is not rebuilt — no rows are touched). ``ON DELETE CASCADE`` means an
    Undo of the meal header removes its items atomically.
    """
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_diet_logs_id_user "
        "ON diet_logs(id, user_id)"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diet_log_items (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              INTEGER NOT NULL,
            diet_log_id          INTEGER NOT NULL,
            item_order           INTEGER NOT NULL,
            source_type          TEXT NOT NULL
                                    CHECK(source_type IN ('food', 'recipe', 'freetext')),
            source_id            INTEGER,
            display_name         TEXT NOT NULL,
            entered_amount       REAL,
            entered_unit         TEXT,
            resolved_base_amount REAL,
            resolved_base_unit   TEXT,
            calories             INTEGER,
            protein_g            REAL,
            carbs_g              REAL,
            fat_g                REAL,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (diet_log_id, user_id)
                REFERENCES diet_logs(id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_diet_log_items_lookup "
        "ON diet_log_items(user_id, diet_log_id, item_order)"
    )
    # A source-scoped index powers the Phase 4 suggestion queries (a user's
    # completed items for a given food/recipe) without a full-table scan.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_diet_log_items_source "
        "ON diet_log_items(user_id, source_type, source_id)"
    )


async def _migration_0007_food_preferences(conn: aiosqlite.Connection) -> None:
    """Add per-user food/recipe preferences and a personalization toggle.

    ``user_food_preferences`` stores explicit pins, hides, and default
    quantities keyed by ``(user_id, source_type, source_id)``. It is optional
    signal layered on top of completed ``diet_log_items`` history — suggestions
    still work with an empty table. ``user_settings.suggestions_enabled`` lets a
    user turn personalized ordering off entirely (history is still recorded, just
    not used to reorder). Both default to the current behavior.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_food_preferences (
            user_id      INTEGER NOT NULL,
            source_type  TEXT NOT NULL CHECK(source_type IN ('food', 'recipe')),
            source_id    INTEGER NOT NULL,
            is_pinned    INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0, 1)),
            hidden       INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0, 1)),
            default_amount REAL,
            default_unit   TEXT,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, source_type, source_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    # ``ADD COLUMN`` with a constant default is non-destructive and does not
    # rewrite existing rows; every current user keeps personalized ordering on.
    cursor = await conn.execute("PRAGMA table_info(user_settings)")
    columns = {row["name"] for row in await cursor.fetchall()}
    if "suggestions_enabled" not in columns:
        await conn.execute(
            "ALTER TABLE user_settings ADD COLUMN suggestions_enabled "
            "INTEGER NOT NULL DEFAULT 1 CHECK(suggestions_enabled IN (0, 1))"
        )


async def _migration_0008_shared_catalog(conn: aiosqlite.Connection) -> None:
    """Add a shared, curated nutrition catalog and let items cite it.

    ``catalog_foods`` mirrors the per-user ``foods`` shape (so the same nutrition
    resolver works) but is shared and carries provenance: ``provider`` +
    ``provider_food_id`` (unique) and ``provider_revision``, so a future data
    refresh is auditable and a completed log's snapshot is never rewritten.
    ``catalog_aliases`` supports regional names without duplicating profiles;
    ``catalog_portions`` gives named portions.

    ``diet_log_items`` is rebuilt (rows preserved) to (a) add
    ``source_provider``/``source_revision`` and (b) widen the ``source_type``
    check to include ``'catalog'``, so a catalog selection is a first-class,
    snapshotted item. The table has no children, so the rebuild cascades nothing;
    the composite FK still resolves through the ``diet_logs(id, user_id)`` unique
    index created at v6.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_foods (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            provider          TEXT NOT NULL,
            provider_food_id  TEXT NOT NULL,
            provider_revision TEXT,
            display_name      TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100),
            name_key          TEXT NOT NULL CHECK(length(name_key) BETWEEN 1 AND 100),
            brand             TEXT,
            category          TEXT,
            base_unit         TEXT NOT NULL CHECK(base_unit IN ('g', 'ml', 'piece')),
            basis_amount      REAL NOT NULL CHECK(basis_amount > 0 AND basis_amount <= 1000000),
            calories          REAL CHECK(calories IS NULL OR (calories >= 0 AND calories <= 1000000)),
            protein_g         REAL CHECK(protein_g IS NULL OR (protein_g >= 0 AND protein_g <= 1000000)),
            carbs_g           REAL CHECK(carbs_g IS NULL OR (carbs_g >= 0 AND carbs_g <= 1000000)),
            fat_g             REAL CHECK(fat_g IS NULL OR (fat_g >= 0 AND fat_g <= 1000000)),
            is_active         INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider, provider_food_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_foods_name "
        "ON catalog_foods(name_key) WHERE is_active = 1"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_aliases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_food_id INTEGER NOT NULL,
            alias           TEXT NOT NULL,
            alias_key       TEXT NOT NULL,
            UNIQUE(catalog_food_id, alias_key),
            FOREIGN KEY (catalog_food_id)
                REFERENCES catalog_foods(id) ON DELETE CASCADE
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_aliases_key "
        "ON catalog_aliases(alias_key)"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_portions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_food_id INTEGER NOT NULL,
            name            TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 50),
            name_key        TEXT NOT NULL CHECK(length(name_key) BETWEEN 1 AND 50),
            base_amount     REAL NOT NULL CHECK(base_amount > 0 AND base_amount <= 1000000),
            UNIQUE(catalog_food_id, name_key),
            FOREIGN KEY (catalog_food_id)
                REFERENCES catalog_foods(id) ON DELETE CASCADE
        )
        """
    )

    # Rebuild diet_log_items to widen source_type and add provenance columns.
    # Standard SQLite table-rebuild: create new, copy, drop, rename, reindex.
    await conn.execute(
        """
        CREATE TABLE diet_log_items_new (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              INTEGER NOT NULL,
            diet_log_id          INTEGER NOT NULL,
            item_order           INTEGER NOT NULL,
            source_type          TEXT NOT NULL
                                    CHECK(source_type IN ('food', 'recipe', 'freetext', 'catalog')),
            source_id            INTEGER,
            source_provider      TEXT,
            source_revision      TEXT,
            display_name         TEXT NOT NULL,
            entered_amount       REAL,
            entered_unit         TEXT,
            resolved_base_amount REAL,
            resolved_base_unit   TEXT,
            calories             INTEGER,
            protein_g            REAL,
            carbs_g              REAL,
            fat_g                REAL,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (diet_log_id, user_id)
                REFERENCES diet_logs(id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    # Copy rows into the widened table. Preserve the provenance columns when the
    # source table already has them. This matters for the supported recovery
    # scenario where an already-current schema is re-stamped to version 0 and
    # replays migrations: its diet_log_items already carries source_provider/
    # source_revision, and a fixed column list would silently null them.
    cursor = await conn.execute("PRAGMA table_info(diet_log_items)")
    old_columns = {row["name"] for row in await cursor.fetchall()}
    # Column names come only from this fixed allowlist, never from user input.
    copy_columns = [
        "id", "user_id", "diet_log_id", "item_order", "source_type", "source_id",
        "display_name", "entered_amount", "entered_unit", "resolved_base_amount",
        "resolved_base_unit", "calories", "protein_g", "carbs_g", "fat_g",
        "created_at",
    ]
    copy_columns.extend(
        column
        for column in ("source_provider", "source_revision")
        if column in old_columns
    )
    column_list = ", ".join(copy_columns)
    await conn.execute(
        f"INSERT INTO diet_log_items_new ({column_list}) "  # noqa: S608
        f"SELECT {column_list} FROM diet_log_items"
    )
    await conn.execute("DROP TABLE diet_log_items")
    await conn.execute("ALTER TABLE diet_log_items_new RENAME TO diet_log_items")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_diet_log_items_lookup "
        "ON diet_log_items(user_id, diet_log_id, item_order)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_diet_log_items_source "
        "ON diet_log_items(user_id, source_type, source_id)"
    )


async def _migration_0009_supplements(conn: aiosqlite.Connection) -> None:
    """Add ``supplements`` and ``supplement_logs`` — adherence, never nutrition.

    A supplement is deliberately *not* a food. It carries a dose and a timing
    label so "2 capsules, with dinner" is recorded as written, but it has no
    calorie or macro columns and no relationship to ``diet_logs``,
    ``diet_log_items``, or the catalog. Nothing here can reach nutrition
    resolution, so a supplement can never move a meal total.

    ``supplement_logs`` mirrors ``habit_logs``: one row per user/supplement/local
    date, uniquely keyed so a double tap is idempotent rather than a second
    adherence record. Deactivation is a soft archive, as with habits, so history
    survives and a name can be reused later.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supplements (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            name           TEXT NOT NULL,
            name_key       TEXT NOT NULL,
            dose_amount    REAL,
            dose_unit      TEXT,
            timing         TEXT,
            is_active      INTEGER NOT NULL DEFAULT 1,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (dose_amount IS NULL OR dose_amount > 0),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    # Enforce "one active supplement per name per user" in the database rather
    # than only in the handler, so a future script or alternate entry point
    # cannot create the duplicate that would split a streak across two ids.
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_supplements_active_name "
        "ON supplements(user_id, name_key) WHERE is_active = 1"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supplement_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            supplement_id INTEGER NOT NULL,
            log_date      DATE NOT NULL,
            UNIQUE(user_id, supplement_id, log_date),
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (supplement_id) REFERENCES supplements(id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_supplement_logs_lookup "
        "ON supplement_logs(user_id, log_date)"
    )


async def _migration_0010_ai_parsing_consent(conn: aiosqlite.Connection) -> None:
    """Add per-user consent for sending meal text to an external parser.

    Adds no table, so ``required_tables_for`` is unchanged; this is a column-only
    migration on ``user_settings``.

    ``DEFAULT 0`` is the whole point. Consent cannot be inherited, inferred from
    another setting, or granted by deploying a key — every existing and future
    user starts opted out and must turn it on themselves. ``consented_at`` records
    when, so the choice is auditable rather than merely asserted; it is cleared on
    opt-out so a revoked consent leaves no lingering "they agreed once" evidence.
    """
    cursor = await conn.execute("PRAGMA table_info(user_settings)")
    existing = {row["name"] for row in await cursor.fetchall()}

    if "ai_parsing_enabled" not in existing:
        await conn.execute(
            "ALTER TABLE user_settings ADD COLUMN ai_parsing_enabled "
            "INTEGER NOT NULL DEFAULT 0 CHECK(ai_parsing_enabled IN (0, 1))"
        )
    if "ai_parsing_consented_at" not in existing:
        await conn.execute(
            "ALTER TABLE user_settings ADD COLUMN ai_parsing_consented_at TIMESTAMP"
        )


async def _migration_0011_gym_sets_and_exercises(conn: aiosqlite.Connection) -> None:
    """Per-set gym logging, and a tappable exercise list.

    Two problems, one migration, because they only make sense together.

    **A set is not the same as every other set.** ``gym_logs`` stored one
    ``sets``/``reps``/``weight_kg`` triple, which can only describe a workout
    where every set was identical. Real sets vary — the last one is lighter, or
    you push an extra rep — and the old shape silently flattened that. ``gym_sets``
    holds one row per set; the header keeps ``sets`` as the count and carries
    ``reps``/``weight_kg`` **only when every set matched**, so the existing
    shortcut, ``/recent``, and the volume chart keep working unchanged and a
    varying exercise is honestly ``NULL`` rather than misleadingly uniform.
    ``total_volume_kg`` is stored on the header so a chart never has to fetch
    children to draw a bar.

    Making ``reps`` nullable needs a table rebuild — SQLite cannot relax NOT NULL
    in place. The rebuild copies every existing row and backfills its volume, so
    no history is lost.

    **Typing an exercise name is not tappable.** ``exercises`` is one table for
    both the shared starter list (``user_id IS NULL``, seeded at startup) and
    anything a user adds for themselves. One table rather than the food module's
    two, because unlike foods the two kinds have identical columns — there is no
    provider or revision to track for "Bench press".
    """
    # --- exercises: shared starter list + private additions -----------------
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exercises (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            group_key   TEXT NOT NULL,
            name        TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 50),
            name_key    TEXT NOT NULL CHECK(length(name_key) BETWEEN 1 AND 50),
            is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    # Two partial indexes rather than one: a user may add an exercise whose name
    # matches a shared one (their own variant), so uniqueness is per owner, and
    # NULL never equals NULL in a SQL unique index.
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exercises_shared_name "
        "ON exercises(name_key) WHERE user_id IS NULL AND is_active = 1"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exercises_private_name "
        "ON exercises(user_id, name_key) WHERE user_id IS NOT NULL AND is_active = 1"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exercises_group "
        "ON exercises(group_key, is_active)"
    )

    # --- gym_logs rebuild: nullable reps + stored volume --------------------
    cursor = await conn.execute("PRAGMA table_info(gym_logs)")
    columns = {row["name"] for row in await cursor.fetchall()}
    if "total_volume_kg" not in columns:
        await conn.execute(
            """
            CREATE TABLE gym_logs_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                exercise        TEXT NOT NULL,
                sets            INTEGER NOT NULL CHECK(sets > 0),
                reps            INTEGER,
                weight_kg       REAL,
                total_volume_kg REAL,
                total_reps      INTEGER,
                logged_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """
        )
        # Every pre-v11 row was uniform by construction, so its volume is exact.
        await conn.execute(
            """
            INSERT INTO gym_logs_new
                (id, user_id, exercise, sets, reps, weight_kg, total_volume_kg,
                 total_reps, logged_at)
            SELECT id, user_id, exercise, sets, reps, weight_kg,
                   CASE WHEN weight_kg IS NULL THEN NULL
                        ELSE sets * reps * weight_kg END,
                   sets * reps,
                   logged_at
            FROM gym_logs
            """
        )
        await conn.execute("DROP TABLE gym_logs")
        await conn.execute("ALTER TABLE gym_logs_new RENAME TO gym_logs")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gym_user_date "
            "ON gym_logs(user_id, logged_at)"
        )

    # --- gym_sets: one row per set ------------------------------------------
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gym_sets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            gym_log_id INTEGER NOT NULL,
            set_number INTEGER NOT NULL CHECK(set_number > 0),
            reps       INTEGER NOT NULL CHECK(reps > 0),
            weight_kg  REAL CHECK(weight_kg IS NULL OR weight_kg >= 0),
            UNIQUE(gym_log_id, set_number),
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (gym_log_id) REFERENCES gym_logs(id) ON DELETE CASCADE
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gym_sets_parent "
        "ON gym_sets(gym_log_id, set_number)"
    )


async def _migration_0012_meal_shortcuts(conn: aiosqlite.Connection) -> None:
    """Let a user say "this is a snack item" before any history exists.

    Suggestions were already meal-type aware — ``bot.suggestions`` weights
    same-meal-type frequency three times general use — but only by *learning*
    from completed meals. That leaves two gaps a new ledger feels immediately:
    a food you know you eat at snack time has to be logged through Search
    several times before it becomes tappable, and a shared-catalog item can
    never be personalised at all.

    A separate table rather than a ``meal_type`` column on
    ``user_food_preferences``: that table's ``is_pinned``/``hidden``/
    ``default_amount`` are properties of the *food* ("my usual is 150 g"), and
    widening its primary key would silently redefine all three as per-meal-type.
    A shortcut is a different statement — about a food's place in a meal — so it
    gets its own row.

    ``source_type`` accepts ``catalog`` here, which ``user_food_preferences``
    deliberately does not: a shortcut stores no nutrition and no amount, only a
    pointer, so pointing at shared reference data carries no snapshot risk.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meal_shortcuts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            meal_type   TEXT NOT NULL
                CHECK(meal_type IN ('breakfast', 'lunch', 'dinner', 'snack')),
            source_type TEXT NOT NULL
                CHECK(source_type IN ('food', 'recipe', 'catalog')),
            source_id   INTEGER NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, meal_type, source_type, source_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meal_shortcuts_lookup "
        "ON meal_shortcuts(user_id, meal_type)"
    )


async def _migration_0013_weight_logs(conn: aiosqlite.Connection) -> None:
    """Daily body weight — one number, at most one per day.

    Keyed on ``log_date`` (a local calendar date) rather than a UTC
    ``logged_at`` instant, like ``habit_logs`` and unlike ``diet_logs``. A meal
    happens at a moment; a body weight is a property of a *day*, and the
    question "what did I weigh on Tuesday" must have one answer regardless of
    what time the scale was read. ``UNIQUE(user_id, log_date)`` is what makes
    that true in the schema rather than merely in the handler: re-weighing
    replaces the day's number instead of accumulating rows that a chart would
    then have to pick between.

    ``logged_at`` is still recorded, so a correction is distinguishable from a
    first entry after the fact, but nothing reads it for placement.

    The ``CHECK`` bounds are deliberately wide — this is a guard against a
    fat-fingered ``724`` or a negative, not an opinion about anybody's body.
    Anything inside them is somebody's real weight.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weight_logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            log_date  TEXT NOT NULL,
            weight_kg REAL NOT NULL CHECK(weight_kg > 0 AND weight_kg <= 500),
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, log_date),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    # UNIQUE(user_id, log_date) already indexes exactly the range scan every
    # read performs (one user, a date window, in date order), so no second
    # index is created here.


async def _migration_0014_app_suggestions(conn: aiosqlite.Connection) -> None:
    """What the people using this bot think it should do next.

    Every other table here records something that happened to a user. This one
    records something they want from the *app*, which makes it the only table
    whose reader is a maintainer rather than a chart. That difference decides
    its shape:

    * The text is stored verbatim. A suggestion is an opinion, and parsing one
      into fields would be deciding in advance which kinds of opinion are
      expressible.
    * There is no status, priority, or assignee column. Two people use this
      bot; a workflow nobody runs is a column that goes stale and then lies.
      Whether a suggestion was acted on is answered by the app changing.
    * Nothing cascades and nothing else references it. A suggestion is not part
      of anyone's ledger, so deleting one leaves no hole in any total.

    A single index on ``(user_id, id DESC)`` serves the only read the bot makes
    — this user's most recent few — while the maintainer's read is a full scan
    of a table that will hold tens of rows.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_suggestions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            suggestion TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_suggestions_user "
        "ON app_suggestions(user_id, id DESC)"
    )


async def _migration_0015_ai_parsing_tristate(conn: aiosqlite.Connection) -> None:
    """Let AI parsing be on by default without asserting anyone consented.

    The owner asked for it on by default for both users. The obvious way to do
    that — ``UPDATE user_settings SET ai_parsing_enabled = 1`` — is exactly what
    :func:`_migration_0010_ai_parsing_consent` was written to prevent, and it
    would also stamp ``consented_at`` with a moment at which nobody agreed to
    anything. A recorded consent that never happened is worse than no record.

    So the column becomes **nullable, and NULL means "has not chosen"**. The
    deployment default (``AI_PARSING_DEFAULT_ON``) answers for those rows, while
    ``0`` and ``1`` remain what they have always been: a decision this user made,
    which the default never overrides. ``consented_at`` keeps its old meaning and
    is still only ever set by an explicit opt-in.

    Existing rows are converted only where no choice was ever recorded — enabled
    ``0`` with a NULL ``consented_at``, which is the state v10 created for every
    user and the state both live users are still in. A user who had explicitly
    opted out would be indistinguishable from one who never answered, because
    opt-out deliberately clears the timestamp; that ambiguity is accepted here
    only because it is verifiable that neither user has run the command, and it
    cannot recur, since every write from now on stores an explicit 0 or 1.

    SQLite cannot drop a NOT NULL constraint in place, so the table is rebuilt.
    """
    cursor = await conn.execute("PRAGMA table_info(user_settings)")
    columns = {row["name"]: row for row in await cursor.fetchall()}
    if "ai_parsing_enabled" not in columns:  # pragma: no cover - v10 guarantees it
        return
    if not columns["ai_parsing_enabled"]["notnull"]:
        return  # already nullable

    await conn.execute(
        """
        CREATE TABLE user_settings_new (
            user_id                 INTEGER PRIMARY KEY,
            reminders_enabled       INTEGER NOT NULL DEFAULT 1
                                        CHECK(reminders_enabled IN (0, 1)),
            routine_profile         TEXT,
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            suggestions_enabled     INTEGER NOT NULL DEFAULT 1
                                        CHECK(suggestions_enabled IN (0, 1)),
            ai_parsing_enabled      INTEGER
                                        CHECK(ai_parsing_enabled IS NULL
                                              OR ai_parsing_enabled IN (0, 1)),
            ai_parsing_consented_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        """
        INSERT INTO user_settings_new (
            user_id, reminders_enabled, routine_profile, created_at, updated_at,
            suggestions_enabled, ai_parsing_enabled, ai_parsing_consented_at
        )
        SELECT
            user_id, reminders_enabled, routine_profile, created_at, updated_at,
            suggestions_enabled,
            CASE
                WHEN ai_parsing_enabled = 0 AND ai_parsing_consented_at IS NULL
                    THEN NULL
                ELSE ai_parsing_enabled
            END,
            ai_parsing_consented_at
        FROM user_settings
        """
    )
    await conn.execute("DROP TABLE user_settings")
    await conn.execute("ALTER TABLE user_settings_new RENAME TO user_settings")


async def _migration_0016_catalog_preferences(conn: aiosqlite.Connection) -> None:
    """Let a shared catalog food carry a usual amount, a pin, and a hide.

    ``user_food_preferences`` has always been restricted to ``food`` and
    ``recipe``. The effect was that the *shared* half of the food data was
    second-class: rice and roti exist as one catalog row each, visible to both
    users, but neither user could store "my usual is one bowl" against them, and
    a source with no usual amount can never render as a ``⚡`` one-tap row. The
    only way to get a one-tap staple was therefore to make a private copy of
    something the catalog already had — the app was routing people into
    duplicating its own shared data.

    Nothing about ownership changes. The catalog row stays shared and unowned;
    the *preference* is per user, as it already was, so two people can keep
    different usual amounts for the same rice without either seeing the other's.
    ``meal_shortcuts`` (v12) already accepts ``catalog`` for exactly this reason,
    which is what made the restriction here look like an oversight rather than a
    rule.

    SQLite cannot widen a CHECK in place, so the table is rebuilt. Existing rows
    are copied unchanged.
    """
    cursor = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'user_food_preferences'"
    )
    row = await cursor.fetchone()
    if row is None:  # pragma: no cover - v7 guarantees the table
        return
    if "'catalog'" in str(row["sql"]):
        return  # already widened

    await conn.execute(
        """
        CREATE TABLE user_food_preferences_new (
            user_id      INTEGER NOT NULL,
            source_type  TEXT NOT NULL
                            CHECK(source_type IN ('food', 'recipe', 'catalog')),
            source_id    INTEGER NOT NULL,
            is_pinned    INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0, 1)),
            hidden       INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0, 1)),
            default_amount REAL,
            default_unit   TEXT,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, source_type, source_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        """
        INSERT INTO user_food_preferences_new (
            user_id, source_type, source_id, is_pinned, hidden,
            default_amount, default_unit, updated_at
        )
        SELECT user_id, source_type, source_id, is_pinned, hidden,
               default_amount, default_unit, updated_at
        FROM user_food_preferences
        """
    )
    await conn.execute("DROP TABLE user_food_preferences")
    await conn.execute(
        "ALTER TABLE user_food_preferences_new RENAME TO user_food_preferences"
    )


async def _migration_0017_monitors(conn: aiosqlite.Connection) -> None:
    """Monitored behaviours — counted occurrences measured against a target.

    Every other check-off table here (``habit_logs``, ``supplement_logs``) is
    keyed ``UNIQUE(user_id, thing_id, log_date)``: the thing either happened that
    day or it did not, and a second tap is idempotent. This table deliberately
    breaks that rule. A monitor answers *how many*, so three cigarettes is three
    rows, and each row can carry the quantity and variant of that one occurrence.

    The consequence is stated here because it drives the UI: a double tap is a
    real second occurrence, not a no-op, so every surface that logs one must also
    offer an undo. ``bot.handlers.monitors`` does.

    ``monitors`` holds the *intention* (``target_period`` plus an inclusive
    ``target_min``/``target_max`` pair) alongside the name. Both bounds nullable
    covers every shape the two users needed — ``max = 0`` for "zero",
    ``max = 1`` over a month, a ``2..4`` band over a week, and no bounds at all
    for something merely observed. ``bot.monitor_targets`` owns what those
    numbers *mean*; the schema only guarantees they are coherent.

    Soft deactivation and the partial unique index mirror habits and supplements,
    so history survives a removal and a name can be reused later.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            name            TEXT NOT NULL,
            name_key        TEXT NOT NULL,
            emoji           TEXT,
            target_period   TEXT NOT NULL DEFAULT 'day'
                            CHECK (target_period IN ('day', 'week', 'month')),
            target_min      INTEGER CHECK (target_min IS NULL OR target_min >= 0),
            target_max      INTEGER CHECK (target_max IS NULL OR target_max >= 0),
            tracks_quantity INTEGER NOT NULL DEFAULT 0
                            CHECK (tracks_quantity IN (0, 1)),
            quantity_unit   TEXT,
            tracks_variant  INTEGER NOT NULL DEFAULT 0
                            CHECK (tracks_variant IN (0, 1)),
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                target_min IS NULL OR target_max IS NULL OR target_min <= target_max
            ),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_monitors_active_name "
        "ON monitors(user_id, name_key) WHERE is_active = 1"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            monitor_id    INTEGER NOT NULL,
            log_date      TEXT NOT NULL,
            quantity      REAL CHECK (quantity IS NULL OR quantity > 0),
            quantity_unit TEXT,
            variant       TEXT,
            logged_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (monitor_id) REFERENCES monitors(id)
        )
        """
    )
    # Every read is "one monitor's occurrences inside a date window", either to
    # count them or to show the day's detail. One index serves both, and the
    # trailing id keeps "the most recent occurrence" (what undo removes) at the
    # end of the range rather than requiring a separate sort.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_monitor_logs_monitor_date "
        "ON monitor_logs(user_id, monitor_id, log_date, id)"
    )


async def _migration_0018_supplement_counts(conn: aiosqlite.Connection) -> None:
    """A supplement can be taken more than once a day, against a daily target.

    Until now a supplement was a pure check-off: the row in ``supplement_logs``
    said *that* it was taken on a date, and a second tap was idempotent by
    design. That is right for a once-a-day pill and wrong for anything dosed in
    scoops — "I aim for at least two scoops of creatine and had one" is a
    statement the schema had no way to hold, so the checklist could only record
    it as done.

    Two columns, no new table:

    * ``supplements.target_count`` — how many times a day this one is aimed at.
      ``NULL`` means what it has always meant: one tap and it is done. Only a
      supplement that opts in changes behaviour, so every existing row keeps its
      exact semantics and every existing streak keeps its exact value.
    * ``supplement_logs.taken_count`` — how many were actually taken that day.
      ``DEFAULT 1`` because that is precisely what every pre-existing row means:
      it exists, therefore it was taken once. The unique key is untouched, so a
      day is still one row per supplement — the count lives *in* that row rather
      than becoming a second row, which keeps every existing adherence read
      (streaks, ranges, the taken set) correct without rewriting it.

    This is deliberately the opposite trade-off from ``monitor_logs`` (v17),
    which stores one row per occurrence: a monitor cares *when* each occurrence
    happened, a supplement only cares how many landed on the day.
    """
    cursor = await conn.execute("PRAGMA table_info(supplements)")
    supplement_columns = {row["name"] for row in await cursor.fetchall()}
    if "target_count" not in supplement_columns:
        await conn.execute(
            "ALTER TABLE supplements ADD COLUMN target_count INTEGER "
            "CHECK (target_count IS NULL OR target_count >= 1)"
        )

    cursor = await conn.execute("PRAGMA table_info(supplement_logs)")
    log_columns = {row["name"] for row in await cursor.fetchall()}
    if "taken_count" not in log_columns:
        await conn.execute(
            "ALTER TABLE supplement_logs ADD COLUMN taken_count INTEGER "
            "NOT NULL DEFAULT 1 CHECK (taken_count >= 1)"
        )


_MIGRATIONS: dict[int, Callable[[aiosqlite.Connection], Awaitable[None]]] = {
    1: _migration_0001_baseline,
    2: _migration_0002_mutation_receipts,
    3: _migration_0003_user_settings,
    4: _migration_0004_habit_activity_periods,
    5: _migration_0005_reminder_deliveries,
    6: _migration_0006_diet_log_items,
    7: _migration_0007_food_preferences,
    8: _migration_0008_shared_catalog,
    9: _migration_0009_supplements,
    10: _migration_0010_ai_parsing_consent,
    11: _migration_0011_gym_sets_and_exercises,
    12: _migration_0012_meal_shortcuts,
    13: _migration_0013_weight_logs,
    14: _migration_0014_app_suggestions,
    15: _migration_0015_ai_parsing_tristate,
    16: _migration_0016_catalog_preferences,
    17: _migration_0017_monitors,
    18: _migration_0018_supplement_counts,
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
        # A matching version is not proof of a correct shape; verify before serving.
        await verify_current_schema(conn)
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

    # Prove the freshly migrated schema actually has every required object before
    # the bot starts accepting traffic against it.
    await verify_current_schema(conn)
    return LATEST_VERSION
