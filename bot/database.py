"""
Async SQLite database manager for Ledger bot.

Single shared connection with WAL mode, foreign keys, and composite indexes.
Row-presence semantics for habit_logs (no 'completed' column).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import unicodedata
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, NamedTuple

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

# True while the current task holds the connection lock (set by _write_operation
# or a standalone locked read). Lets reads nested inside an operation that
# already holds the lock skip re-acquiring it, avoiding self-deadlock while still
# serializing independent tasks. contextvars propagate through ``await`` within a
# task but are isolated between tasks, so this is safe under concurrency.
_conn_lock_held: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ledger_conn_lock_held", default=False
)

HabitAddStatus = Literal["added", "reactivated", "already_active"]


class MutationSource(NamedTuple):
    """Identity of the Telegram update that triggered a mutation.

    Threaded into the log methods to make a write idempotent under at-least-once
    delivery (``drop_pending_updates=False``): replaying the *same* update returns
    the original row instead of inserting a duplicate. ``update_id`` is globally
    unique per bot; ``message_id`` is only unique within a chat, so it is stored
    for reconciliation but never used as the idempotency key on its own.
    """

    update_id: int
    chat_id: int | None = None
    message_id: int | None = None

MAX_DISPLAY_UNIT_LENGTH = MAX_PORTION_NAME_LENGTH
MAX_ACTIVE_FOODS = 500
MAX_PORTIONS_PER_FOOD = 50
MAX_ACTIVE_RECIPES = 200
MAX_INGREDIENTS_PER_RECIPE = 100
MAX_CATALOG_AMOUNT = float(NUTRITION_MAX_CATALOG_AMOUNT)
MAX_NUTRIENT_VALUE = float(NUTRITION_MAX_NUTRIENT_VALUE)

# ---------------------------------------------------------------------------
# Schema DDL and migrations now live in bot/migrations.py, keyed on
# PRAGMA user_version. init_db() delegates to migrations.run_migrations().
# ---------------------------------------------------------------------------


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


def _habit_key(name: str) -> str:
    """Case-insensitive, Unicode-normalized key for habit de-duplication.

    Matches the handler's ``casefold`` check but also NFKC-normalizes so visually
    identical names collide, keeping the streak on one habit id.
    """
    return unicodedata.normalize("NFKC", str(name)).strip().casefold()


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


def _utc_date_now() -> str:
    """Current UTC date (ISO). Fallback when a caller supplies no local date."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DatabaseManager:
    """Async SQLite manager holding a single shared connection."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        # One lock guards the shared connection for BOTH writes and reads, so a
        # read can never observe another task's uncommitted (later rolled-back)
        # write on the same connection.
        self._conn_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the connection and set pragmas."""
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = aiosqlite.Row
        logger.info("Database connected: %s", self.db_path)

    async def init_db(self) -> None:
        """Bring the schema to the latest version via ordered migrations.

        Schema evolution lives in :mod:`bot.migrations`, keyed on
        ``PRAGMA user_version``. Runs under the connection lock so no concurrent
        read can observe a half-applied migration.
        """
        assert self._conn is not None, "Call connect() first"
        from . import migrations

        async with self._conn_lock:
            token = _conn_lock_held.set(True)
            try:
                await migrations.run_migrations(self._conn)
            finally:
                _conn_lock_held.reset(token)
        logger.info("Database schema initialized")

    async def _migrate_habit_name_keys(self) -> None:
        """Backfill ``habits.name_key`` (kept for targeted migration tests)."""
        from . import migrations

        await migrations.backfill_habit_name_keys(self._conn)

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

        Holds the connection lock for the whole BEGIN..COMMIT lifecycle so that
        no concurrent read (which also takes this lock) can see the in-progress
        transaction's uncommitted rows.
        """
        async with self._conn_lock:
            token = _conn_lock_held.set(True)
            try:
                yield
                await self.conn.commit()
            except BaseException:
                if self.conn.in_transaction:
                    await self.conn.rollback()
                raise
            finally:
                _conn_lock_held.reset(token)

    @asynccontextmanager
    async def _read_operation(self) -> AsyncIterator[None]:
        """Serialize a read against writes, reentrant within a held operation.

        A read that runs inside a task already holding the lock (e.g. a write
        that inspects rows before mutating) proceeds without re-acquiring it;
        an independent read waits for any in-progress write to finish and thus
        only ever observes committed state.
        """
        if _conn_lock_held.get():
            yield
            return
        async with self._conn_lock:
            token = _conn_lock_held.set(True)
            try:
                yield
            finally:
                _conn_lock_held.reset(token)

    async def _query_all(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        """Run a SELECT and fetch all rows under the connection lock."""
        async with self._read_operation():
            cursor = await self.conn.execute(query, params)
            return await cursor.fetchall()

    async def _query_one(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        """Run a SELECT and fetch one row under the connection lock."""
        async with self._read_operation():
            cursor = await self.conn.execute(query, params)
            return await cursor.fetchone()

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
    # Mutation idempotency (replay-safety)
    # -------------------------------------------------------------------
    async def _replayed_entity_id(
        self, source: MutationSource | None, user_id: int, operation_key: str
    ) -> int | None:
        """Return an existing entity id if this exact update was already applied.

        Assumes the caller holds the write lock. Raises if a receipt exists for a
        *different* owner (impossible for a genuine Telegram update, whose id is
        globally unique) so a replay can never return another user's row.
        """
        if source is None:
            return None
        cursor = await self.conn.execute(
            "SELECT user_id, entity_id FROM mutation_receipts "
            "WHERE telegram_update_id = ? AND operation_key = ?",
            (source.update_id, operation_key),
        )
        existing = await cursor.fetchone()
        if existing is None:
            return None
        if existing["user_id"] != user_id:
            raise RuntimeError(
                "mutation receipt owner mismatch — refusing cross-user replay"
            )
        return existing["entity_id"]

    async def _record_receipt(
        self,
        source: MutationSource,
        user_id: int,
        operation_key: str,
        entity_type: str,
        entity_id: int,
    ) -> None:
        """Persist a receipt in the same transaction as its domain insert."""
        await self.conn.execute(
            "INSERT INTO mutation_receipts "
            "(telegram_update_id, operation_key, user_id, chat_id, message_id, "
            "entity_type, entity_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.update_id,
                operation_key,
                user_id,
                source.chat_id,
                source.message_id,
                entity_type,
                entity_id,
                _utc_timestamp_now(),
            ),
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
        *,
        source: MutationSource | None = None,
    ) -> int:
        """Log a study session. Returns the row ID.

        When ``source`` is given the write is idempotent: replaying the same
        Telegram update returns the original row id instead of inserting again.
        """
        async with self._write_operation():
            replayed = await self._replayed_entity_id(source, user_id, "study_log")
            if replayed is not None:
                return replayed
            cursor = await self.conn.execute(
                "INSERT INTO study_logs "
                "(user_id, subject, duration_min, notes, logged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, subject, duration_min, notes, _utc_timestamp_now()),
            )
            row_id: int = cursor.lastrowid  # type: ignore[assignment]
            if source is not None:
                await self._record_receipt(source, user_id, "study_log", "study", row_id)
            return row_id

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
        return await self._query_all(
            "SELECT * FROM study_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )

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
        *,
        source: MutationSource | None = None,
    ) -> int:
        """Log a single gym exercise. Returns the row ID.

        Idempotent when ``source`` is supplied (see :meth:`log_study`).
        """
        async with self._write_operation():
            replayed = await self._replayed_entity_id(source, user_id, "gym_log")
            if replayed is not None:
                return replayed
            cursor = await self.conn.execute(
                "INSERT INTO gym_logs "
                "(user_id, exercise, sets, reps, weight_kg, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, exercise, sets, reps, weight_kg, _utc_timestamp_now()),
            )
            row_id: int = cursor.lastrowid  # type: ignore[assignment]
            if source is not None:
                await self._record_receipt(source, user_id, "gym_log", "gym", row_id)
            return row_id

    async def get_gym_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get gym logs within a local-date range (with ±1 day buffer)."""
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        return await self._query_all(
            "SELECT * FROM gym_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )

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
        *,
        source: MutationSource | None = None,
    ) -> int:
        """Log a diet entry. Returns the row ID.

        Idempotent when ``source`` is supplied (see :meth:`log_study`).
        """
        async with self._write_operation():
            replayed = await self._replayed_entity_id(source, user_id, "diet_log")
            if replayed is not None:
                return replayed
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
            row_id: int = cursor.lastrowid  # type: ignore[assignment]
            if source is not None:
                await self._record_receipt(source, user_id, "diet_log", "diet", row_id)
            return row_id

    async def log_diet_with_items(
        self,
        user_id: int,
        meal_type: str,
        items: Sequence[Mapping[str, Any]],
        *,
        source: MutationSource | None = None,
    ) -> int:
        """Log a meal as a header row plus one structured child per item.

        The ``diet_logs`` header keeps the meal totals so existing analytics and
        summaries are unaffected (one row per meal). A header nutrient field is
        the sum of the items' snapshots, or ``None`` (unknown) if any item's
        value is unknown — the same conservative propagation the recipe
        aggregator uses, so an unknown item never masquerades as a numeric zero.
        Each item is snapshotted into ``diet_log_items`` at save time, so a later
        catalog edit never rewrites a completed meal. Idempotent when ``source``
        is supplied (a replayed final tap returns the existing meal id).
        """
        if not items:
            raise ValueError("A meal must have at least one item.")

        def _total(field: str, *, integer: bool = False) -> float | int | None:
            values = [item.get(field) for item in items]
            if any(value is None for value in values):
                return None
            summed = sum(float(value) for value in values)
            return int(round(summed)) if integer else round(summed, 2)

        display = ", ".join(str(item["display_name"]) for item in items)
        if len(display) > 500:
            display = display[:499] + "…"

        async with self._write_operation():
            replayed = await self._replayed_entity_id(source, user_id, "diet_log")
            if replayed is not None:
                return replayed
            cursor = await self.conn.execute(
                "INSERT INTO diet_logs "
                "(user_id, meal_type, food_items, calories, protein_g, carbs_g, "
                "fat_g, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    meal_type,
                    display,
                    _total("calories", integer=True),
                    _total("protein_g"),
                    _total("carbs_g"),
                    _total("fat_g"),
                    _utc_timestamp_now(),
                ),
            )
            diet_log_id: int = cursor.lastrowid  # type: ignore[assignment]
            for order, item in enumerate(items):
                await self.conn.execute(
                    "INSERT INTO diet_log_items "
                    "(user_id, diet_log_id, item_order, source_type, source_id, "
                    "display_name, entered_amount, entered_unit, "
                    "resolved_base_amount, resolved_base_unit, calories, "
                    "protein_g, carbs_g, fat_g) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        diet_log_id,
                        order,
                        str(item.get("source_type", "freetext")),
                        item.get("source_id"),
                        str(item["display_name"]),
                        item.get("entered_amount"),
                        item.get("entered_unit"),
                        item.get("resolved_base_amount"),
                        item.get("resolved_base_unit"),
                        item.get("calories"),
                        item.get("protein_g"),
                        item.get("carbs_g"),
                        item.get("fat_g"),
                    ),
                )
            if source is not None:
                await self._record_receipt(
                    source, user_id, "diet_log", "diet", diet_log_id
                )
            return diet_log_id

    async def get_diet_log_items(
        self, user_id: int, diet_log_id: int
    ) -> list[dict[str, Any]]:
        """Return one meal's structured items in order (owner-scoped)."""
        rows = await self._query_all(
            "SELECT * FROM diet_log_items "
            "WHERE user_id = ? AND diet_log_id = ? ORDER BY item_order, id",
            (user_id, diet_log_id),
        )
        return [dict(row) for row in rows]

    async def get_diet_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get diet logs within a local-date range (with ±1 day buffer)."""
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        return await self._query_all(
            "SELECT * FROM diet_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )

    # -------------------------------------------------------------------
    # Recent activity (reconciliation for /recent)
    # -------------------------------------------------------------------
    async def get_recent_entries(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return a user's most recent study/gym/diet entries, newest first.

        Lets a user confirm a save landed even when Telegram could not deliver
        the confirmation. Strictly owner-scoped.
        """
        rows = await self._query_all(
            """
            SELECT 'study' AS kind, id, logged_at,
                   subject AS summary, duration_min AS n1, NULL AS n2
            FROM study_logs WHERE user_id = ?
            UNION ALL
            SELECT 'gym', id, logged_at, exercise, sets, reps
            FROM gym_logs WHERE user_id = ?
            UNION ALL
            SELECT 'diet', id, logged_at, food_items, calories, NULL
            FROM diet_logs WHERE user_id = ?
            ORDER BY logged_at DESC, kind, id DESC
            LIMIT ?
            """,
            (user_id, user_id, user_id, limit),
        )
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------
    # Per-user settings (reminder opt-in, routine profile)
    # -------------------------------------------------------------------
    async def get_user_settings(self, user_id: int) -> dict[str, Any] | None:
        """Return a user's settings row, or ``None`` if they have none yet."""
        row = await self._query_one(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        )
        return dict(row) if row is not None else None

    async def ensure_user_settings(
        self, user_id: int, *, default_enabled: bool = False
    ) -> None:
        """Create a settings row if absent (new users default to opt-in = off)."""
        async with self._write_operation():
            await self.conn.execute(
                "INSERT INTO user_settings (user_id, reminders_enabled) "
                "VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, 1 if default_enabled else 0),
            )

    async def set_reminders_enabled(self, user_id: int, enabled: bool) -> None:
        """Turn a user's reminders on or off (upsert, owner-scoped)."""
        async with self._write_operation():
            await self.conn.execute(
                "INSERT INTO user_settings (user_id, reminders_enabled, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "reminders_enabled = excluded.reminders_enabled, "
                "updated_at = excluded.updated_at",
                (user_id, 1 if enabled else 0, _utc_timestamp_now()),
            )

    async def get_reminder_enabled_users(
        self, candidate_ids: Iterable[int]
    ) -> set[int]:
        """Subset of ``candidate_ids`` whose reminders are enabled.

        A user with no settings row is treated as opted out, so a new authorized
        user receives nothing until they explicitly opt in.
        """
        candidates = list(candidate_ids)
        if not candidates:
            return set()
        placeholders = ",".join("?" for _ in candidates)
        rows = await self._query_all(
            "SELECT user_id FROM user_settings "
            f"WHERE reminders_enabled = 1 AND user_id IN ({placeholders})",  # noqa: S608
            tuple(candidates),
        )
        return {row["user_id"] for row in rows}

    # -------------------------------------------------------------------
    # Durable reminder delivery (resumable chunk state)
    # -------------------------------------------------------------------
    async def get_delivered_chunk_indices(
        self, user_id: int, job_key: str, local_date: str
    ) -> set[int]:
        """Chunk indices already delivered for this owner/job/date (idempotency)."""
        rows = await self._query_all(
            "SELECT chunk_index FROM reminder_deliveries "
            "WHERE user_id = ? AND job_key = ? AND local_date = ? "
            "AND status = 'delivered'",
            (user_id, job_key, local_date),
        )
        return {row["chunk_index"] for row in rows}

    async def record_chunk_delivery(
        self,
        user_id: int,
        job_key: str,
        local_date: str,
        chunk_index: int,
        *,
        delivered: bool,
        error_category: str | None = None,
    ) -> None:
        """Persist a chunk's delivery outcome (owner-scoped, attempt-counted).

        ``error_category`` is a sanitized label only (e.g. ``"permanent"``,
        ``"retry_exhausted"``) — never a token or raw exception/URL.
        """
        now = _utc_timestamp_now()
        status = "delivered" if delivered else "failed"
        delivered_at = now if delivered else None
        async with self._write_operation():
            await self.conn.execute(
                """
                INSERT INTO reminder_deliveries
                    (user_id, job_key, local_date, chunk_index, status, attempts,
                     last_attempt_at, delivered_at, error_category)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(user_id, job_key, local_date, chunk_index) DO UPDATE SET
                    -- 'delivered' is terminal: a later failed attempt (e.g. from an
                    -- overlapping run) must never reopen an already-sent chunk, or
                    -- it would be re-sent against freshly generated content.
                    status = CASE
                        WHEN reminder_deliveries.status = 'delivered' THEN 'delivered'
                        ELSE excluded.status
                    END,
                    attempts = reminder_deliveries.attempts + 1,
                    last_attempt_at = excluded.last_attempt_at,
                    delivered_at = COALESCE(
                        reminder_deliveries.delivered_at, excluded.delivered_at
                    ),
                    error_category = CASE
                        WHEN reminder_deliveries.status = 'delivered'
                            THEN reminder_deliveries.error_category
                        ELSE excluded.error_category
                    END
                """,
                (
                    user_id,
                    job_key,
                    local_date,
                    chunk_index,
                    status,
                    now,
                    delivered_at,
                    error_category,
                ),
            )

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
        row = await self._query_one(
            "SELECT * FROM foods "
            "WHERE user_id = ? AND name_key = ? AND is_active = 1",
            (user_id, name_key),
        )
        return dict(row) if row is not None else None

    async def get_food_by_id(
        self, user_id: int, food_id: int
    ) -> dict[str, Any] | None:
        """Return one active food owned by the user, by its primary-key id."""
        row = await self._query_one(
            "SELECT * FROM foods WHERE id = ? AND user_id = ? AND is_active = 1",
            (food_id, user_id),
        )
        return dict(row) if row is not None else None

    async def list_foods(self, user_id: int) -> list[dict[str, Any]]:
        """List a user's active foods in normalized-name order."""
        rows = await self._query_all(
            "SELECT * FROM foods WHERE user_id = ? AND is_active = 1 "
            "ORDER BY name_key, id",
            (user_id,),
        )
        return [dict(row) for row in rows]

    async def get_food_portions(
        self, user_id: int, food_id: int
    ) -> list[dict[str, Any]]:
        """List named portions for an active food owned by the user."""
        rows = await self._query_all(
            "SELECT fp.*, f.base_unit AS food_base_unit "
            "FROM food_portions AS fp "
            "JOIN foods AS f ON f.id = fp.food_id AND f.user_id = fp.user_id "
            "WHERE fp.user_id = ? AND fp.food_id = ? AND f.is_active = 1 "
            "ORDER BY fp.name_key, fp.id",
            (user_id, food_id),
        )
        return [dict(row) for row in rows]

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
        row = await self._query_one(
            "SELECT * FROM recipes "
            "WHERE user_id = ? AND name_key = ? AND is_active = 1",
            (user_id, name_key),
        )
        return dict(row) if row is not None else None

    async def get_recipe_by_id(
        self, user_id: int, recipe_id: int
    ) -> dict[str, Any] | None:
        """Return one active recipe owned by the user, by its primary-key id."""
        row = await self._query_one(
            "SELECT * FROM recipes "
            "WHERE id = ? AND user_id = ? AND is_active = 1",
            (recipe_id, user_id),
        )
        return dict(row) if row is not None else None

    async def list_recipes(self, user_id: int) -> list[dict[str, Any]]:
        """List a user's active recipes in normalized-name order."""
        rows = await self._query_all(
            "SELECT * FROM recipes WHERE user_id = ? AND is_active = 1 "
            "ORDER BY name_key, id",
            (user_id,),
        )
        return [dict(row) for row in rows]

    async def get_recipe_ingredients(
        self, user_id: int, recipe_id: int
    ) -> list[dict[str, Any]]:
        """Return active-recipe ingredients with food nutrition fields."""
        rows = await self._query_all(
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
        return [dict(row) for row in rows]

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
        self, user_id: int, habit_name: str, *, today: date | None = None
    ) -> tuple[int, HabitAddStatus]:
        """Add, reactivate, or find an active habit.

        Returns ``(habit_id, status)`` where status is ``"added"``,
        ``"reactivated"``, or ``"already_active"``. Adding or reactivating opens a
        new activity period starting on ``today`` (local date from the caller;
        falls back to the UTC date when omitted) so lifecycle-aware analytics know
        which days the habit was live.
        """
        name_key = _habit_key(habit_name)
        on_date = today.isoformat() if today is not None else _utc_date_now()
        async with self._write_operation():
            cursor = await self.conn.execute(
                "SELECT id, is_active FROM habits "
                "WHERE user_id = ? AND name_key = ? "
                "ORDER BY is_active DESC, id LIMIT 1",
                (user_id, name_key),
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
                          WHERE user_id = ? AND name_key = ? AND is_active = 1
                      )
                    """,
                    (row["id"], user_id, name_key),
                )
                if cursor.rowcount > 0:
                    await self._open_habit_period(user_id, row["id"], on_date)
                    return row["id"], "reactivated"

            cursor = await self.conn.execute(
                """
                INSERT INTO habits (user_id, habit_name, name_key)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, name_key) WHERE is_active = 1 DO NOTHING
                RETURNING id
                """,
                (user_id, habit_name, name_key),
            )
            inserted = await cursor.fetchone()
            if inserted is not None:
                await self._open_habit_period(user_id, inserted["id"], on_date)
                return inserted["id"], "added"

            # This can occur if another database connection made the habit
            # active between this operation's read and write.
            cursor = await self.conn.execute(
                "SELECT id FROM habits "
                "WHERE user_id = ? AND name_key = ? AND is_active = 1",
                (user_id, name_key),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("Habit add completed without an active habit")
            return active["id"], "already_active"

    async def deactivate_habit(
        self, user_id: int, habit_id: int, *, today: date | None = None
    ) -> bool:
        """Soft-delete a habit. Returns True if a row was affected.

        Closes the habit's current open activity period on ``today`` (local date
        from the caller; UTC date when omitted) so its active span is bounded.
        """
        on_date = today.isoformat() if today is not None else _utc_date_now()
        async with self._write_operation():
            cursor = await self.conn.execute(
                "UPDATE habits SET is_active = 0 "
                "WHERE id = ? AND user_id = ? AND is_active = 1",
                (habit_id, user_id),
            )
            if cursor.rowcount <= 0:
                return False
            await self._close_habit_period(user_id, habit_id, on_date)
            return True

    async def _open_habit_period(
        self, user_id: int, habit_id: int, started_on: str
    ) -> None:
        """Open an activity period (caller holds the write lock). No-op if open."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM habit_activity_periods "
            "WHERE user_id = ? AND habit_id = ? AND ended_on IS NULL LIMIT 1",
            (user_id, habit_id),
        )
        if await cursor.fetchone() is not None:
            return
        await self.conn.execute(
            "INSERT INTO habit_activity_periods (user_id, habit_id, started_on) "
            "VALUES (?, ?, ?)",
            (user_id, habit_id, started_on),
        )

    async def _close_habit_period(
        self, user_id: int, habit_id: int, ended_on: str
    ) -> None:
        """Close the current open activity period (caller holds the write lock).

        Guards against a reversed span if a habit is added and deactivated on the
        same day boundary: the end is clamped to at least the period's start.
        """
        await self.conn.execute(
            "UPDATE habit_activity_periods "
            "SET ended_on = MAX(started_on, ?) "
            "WHERE user_id = ? AND habit_id = ? AND ended_on IS NULL",
            (ended_on, user_id, habit_id),
        )

    async def get_habit_adherence(
        self, user_id: int, week_start: date, today: date
    ) -> tuple[int, int]:
        """Return ``(done, possible)`` habit-days across ``[week_start, today]``.

        A habit-day counts toward ``possible`` only when an activity period covers
        that local day, so a habit deactivated mid-week keeps the days it was live
        (and its completions on them) instead of vanishing from both sides. Habits
        that were active earlier in the window but are inactive now are included.
        """
        period_rows = await self._query_all(
            "SELECT habit_id, started_on, ended_on FROM habit_activity_periods "
            "WHERE user_id = ? AND started_on <= ? "
            "AND (ended_on IS NULL OR ended_on >= ?)",
            (user_id, today.isoformat(), week_start.isoformat()),
        )
        eligible: dict[int, set[date]] = {}
        for row in period_rows:
            start = max(week_start, date.fromisoformat(row["started_on"]))
            end = (
                today
                if row["ended_on"] is None
                else min(today, date.fromisoformat(row["ended_on"]))
            )
            days = eligible.setdefault(row["habit_id"], set())
            current = start
            while current <= end:
                days.add(current)
                current += timedelta(days=1)

        total_possible = sum(len(days) for days in eligible.values())
        if total_possible == 0:
            return 0, 0

        log_rows = await self._query_all(
            "SELECT habit_id, log_date FROM habit_logs "
            "WHERE user_id = ? AND log_date >= ? AND log_date <= ?",
            (user_id, week_start.isoformat(), today.isoformat()),
        )
        total_done = 0
        for row in log_rows:
            habit_days = eligible.get(row["habit_id"])
            if habit_days and date.fromisoformat(row["log_date"]) in habit_days:
                total_done += 1
        return total_done, total_possible

    async def get_active_habits(self, user_id: int) -> list[dict[str, Any]]:
        """Get all active habits for a user."""
        rows = await self._query_all(
            "SELECT * FROM habits WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,),
        )
        return [dict(row) for row in rows]

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
        rows = await self._query_all(
            "SELECT habit_id FROM habit_logs WHERE user_id = ? AND log_date = ?",
            (user_id, log_date.isoformat()),
        )
        return {row["habit_id"] for row in rows}

    async def get_habit_logs_range(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        """Get habit logs in a date range (inclusive)."""
        rows = await self._query_all(
            "SELECT * FROM habit_logs WHERE user_id = ? AND log_date >= ? AND log_date <= ? "
            "ORDER BY log_date",
            (user_id, start_date.isoformat(), end_date.isoformat()),
        )
        return [dict(row) for row in rows]

    _STREAK_PAGE_SIZE = 500

    async def get_streak(self, user_id: int, habit_id: int, today: date) -> int:
        """Calculate the current streak for a habit.

        Streak = number of consecutive days with a row in habit_logs, counting
        backward from today. If today is not checked, streak is 0. History is
        scanned in bounded pages until the first gap, so the streak is never
        capped at a fixed length while memory stays bounded.
        """
        streak = 0
        expected = today
        offset = 0
        while True:
            rows = await self._query_all(
                "SELECT log_date FROM habit_logs "
                "WHERE user_id = ? AND habit_id = ? AND log_date <= ? "
                "ORDER BY log_date DESC LIMIT ? OFFSET ?",
                (
                    user_id,
                    habit_id,
                    today.isoformat(),
                    self._STREAK_PAGE_SIZE,
                    offset,
                ),
            )
            if not rows:
                break
            for row in rows:
                if date.fromisoformat(row["log_date"]) != expected:
                    return streak  # first gap ends the streak
                streak += 1
                expected -= timedelta(days=1)
            if len(rows) < self._STREAK_PAGE_SIZE:
                break
            offset += self._STREAK_PAGE_SIZE
        return streak

    # -------------------------------------------------------------------
    # Undo (preview + delete a recent log across all tables)
    # -------------------------------------------------------------------
    _UNDO_LABELS: dict[str, str] = {
        "study_logs": "📖 Study",
        "gym_logs": "🏋️ Gym",
        "diet_logs": "🍽️ Diet",
    }
    _UNDO_TABLES: frozenset[str] = frozenset(_UNDO_LABELS)

    async def _select_last_entry(self, user_id: int) -> dict[str, Any] | None:
        """Read-only: find the most recent undoable entry (within 24h).

        Returns a dict of the row plus a ``category`` label and a private
        ``_table`` key naming its source table, or ``None``.
        """
        latest: dict[str, Any] | None = None
        latest_dt: datetime | None = None

        for table_name, label in self._UNDO_LABELS.items():
            assert table_name in self._UNDO_TABLES  # guard against injection
            row = await self._query_one(
                f"SELECT *, '{label}' as category, '{table_name}' as _table "  # noqa: S608
                f"FROM {table_name} "
                f"WHERE user_id = ? ORDER BY logged_at DESC, id DESC LIMIT 1",
                (user_id,),
            )
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
                latest_dt = logged_at

        return latest

    async def peek_last(self, user_id: int) -> dict[str, Any] | None:
        """Return the most recent undoable entry (within 24h) WITHOUT deleting.

        ``/undo`` previews this exact row and deletes it only on confirmation
        via :meth:`delete_log_by_id`, so a failed confirmation can never delete
        a different, newer entry on retry.
        """
        return await self._select_last_entry(user_id)

    async def undo_last(self, user_id: int) -> dict[str, Any] | None:
        """Select and delete the most recent log entry for this user (within 24h).

        Checks study_logs, gym_logs, diet_logs. habit_logs use a different
        undo path (uncheck_habit). Returns the deleted entry, or None.
        """
        async with self._write_operation():
            entry = await self._select_last_entry(user_id)
            if entry is None:
                return None
            table = entry["_table"]
            assert table in self._UNDO_TABLES  # guard against injection
            cursor = await self.conn.execute(
                f"DELETE FROM {table} WHERE id = ? AND user_id = ?",  # noqa: S608
                (entry["id"], user_id),
            )
            if cursor.rowcount <= 0:
                return None
            return entry

    async def delete_log_by_id(
        self, user_id: int, table: str, entry_id: int
    ) -> dict[str, Any] | None:
        """Idempotently delete one log row by exact id.

        Returns the deleted row (with a ``category`` label) or ``None`` when no
        such row exists — so a repeated confirmation is a harmless no-op rather
        than deleting a different, newer entry.
        """
        if table not in self._UNDO_TABLES:
            raise ValueError(f"Refusing to delete from unknown table {table!r}")
        label = self._UNDO_LABELS[table]
        async with self._write_operation():
            cursor = await self.conn.execute(
                f"SELECT *, '{label}' as category FROM {table} "  # noqa: S608
                f"WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            entry = dict(row)
            cursor = await self.conn.execute(
                f"DELETE FROM {table} WHERE id = ? AND user_id = ?",  # noqa: S608
                (entry_id, user_id),
            )
            if cursor.rowcount <= 0:
                return None
            return entry

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

    async def get_today_meal_count(self, user_id: int, local_today: date) -> int:
        """Number of diet entries logged today (local date)."""
        logs = await self.get_diet_logs(user_id, local_today, local_today)
        from .config import local_date_from_utc

        return sum(
            1
            for row in logs
            if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == local_today
        )

    async def get_today_gym_count(self, user_id: int, local_today: date) -> int:
        """Number of gym entries logged today (local date)."""
        logs = await self.get_gym_logs(user_id, local_today, local_today)
        from .config import local_date_from_utc

        return sum(
            1
            for row in logs
            if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == local_today
        )

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
