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
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, NamedTuple

import aiosqlite

from .meal_models import (
    CurrentCommitStatus,
    CurrentValueCommitResult,
    CurrentValueDecision,
    CurrentValueIssue,
    CurrentValueIssueCode,
    CurrentValueItemProposal,
    CurrentValuePreview,
    DefaultQuantity,
    DietHeaderSnapshot,
    DietItemSnapshot,
    DietItemSourceType,
    DietLogItemInput,
    MealReceipt,
    NutrientValues,
    QuickMealResult,
    QuickMealStatus,
    RepeatResult,
    RepeatStatus,
    UndoResult,
    UndoStatus,
)
from .services.current_values import preview_signature
from .nutrition import (
    FOOD_BASE_UNITS,
    NutritionError,
    MAX_CATALOG_AMOUNT as NUTRITION_MAX_CATALOG_AMOUNT,
    MAX_CATALOG_NAME_LENGTH,
    MAX_MEAL_ITEMS,
    MAX_NUTRIENT_VALUE as NUTRITION_MAX_NUTRIENT_VALUE,
    MAX_PORTION_NAME_LENGTH,
    RECIPE_YIELD_UNITS,
    canonical_unit_alias,
    finalize_log_nutrients,
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


def _parse_stored_utc(value: object) -> datetime | None:
    """Read a stored timestamp back as an aware UTC datetime, or ``None``.

    Rows written by this module and by SQLite's ``CURRENT_TIMESTAMP`` are naive
    UTC, so a naive value is *stamped* as UTC rather than converted; an aware
    value (possible only for hand-written rows) is normalized to UTC. ``None``
    means the timestamp is missing or unparseable, which callers must treat as
    "age unprovable" rather than "old" or "new".
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _header_snapshot(row: Mapping[str, Any]) -> DietHeaderSnapshot:
    """Build the immutable header view of one ``diet_logs`` row."""
    return DietHeaderSnapshot(
        meal_id=int(row["id"]),
        user_id=int(row["user_id"]),
        meal_type=str(row["meal_type"]),
        food_items=str(row["food_items"]),
        nutrients=NutrientValues(
            calories=row["calories"],
            protein_g=row["protein_g"],
            carbs_g=row["carbs_g"],
            fat_g=row["fat_g"],
        ),
        logged_at_utc=_parse_stored_utc(row["logged_at"]),
    )


def _item_snapshot(row: Mapping[str, Any]) -> DietItemSnapshot:
    """Build the immutable child view of one ``diet_log_items`` row."""
    return DietItemSnapshot(
        child_id=int(row["id"]),
        user_id=int(row["user_id"]),
        meal_id=int(row["diet_log_id"]),
        item_order=int(row["item_order"]),
        source_type=DietItemSourceType(str(row["source_type"])),
        source_id=row["source_id"],
        source_provider=row["source_provider"],
        source_revision=row["source_revision"],
        display_name=str(row["display_name"]),
        entered_amount=row["entered_amount"],
        entered_unit=row["entered_unit"],
        resolved_base_amount=row["resolved_base_amount"],
        resolved_base_unit=row["resolved_base_unit"],
        calories=row["calories"],
        protein_g=row["protein_g"],
        carbs_g=row["carbs_g"],
        fat_g=row["fat_g"],
        created_at_utc=_parse_stored_utc(row["created_at"]),
    )


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
    async def _write_operation(
        self, *, begin_immediate: bool = False
    ) -> AsyncIterator[None]:
        """Serialize a complete mutation and close its transaction safely.

        Holds the connection lock for the whole BEGIN..COMMIT lifecycle so that
        no concurrent read (which also takes this lock) can see the in-progress
        transaction's uncommitted rows.

        With ``begin_immediate=True`` the transaction is opened *before* the
        body's first statement instead of on its first write. A read-modify-write
        (read a mutation receipt, then insert only if it is absent) needs this:
        under the default deferred behavior the leading SELECT would run outside
        the transaction, so another connection could commit between the check and
        the insert. The check is explicit rather than an ``assert`` because
        ``assert`` disappears under ``python -O`` (plan §7.1).
        """
        async with self._conn_lock:
            token = _conn_lock_held.set(True)
            try:
                if begin_immediate:
                    if self.conn.in_transaction:
                        raise RuntimeError(
                            "Refusing to BEGIN IMMEDIATE inside an open transaction"
                        )
                    await self.conn.execute("BEGIN IMMEDIATE")
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

    async def _get_mutation_receipt_locked(
        self, source: MutationSource | None, user_id: int, operation_key: str
    ) -> tuple[str, int] | None:
        """Return ``(entity_type, entity_id)`` for an already-applied update.

        The typed sibling of :meth:`_replayed_entity_id`, for operations whose
        replay outcome depends on *what* was recorded — a real meal, or the
        ``diet_repeat_empty`` tombstone meaning "there was nothing to repeat".
        Assumes the caller holds the write lock, and refuses a receipt belonging
        to another user for the same reason.
        """
        if source is None:
            return None
        cursor = await self.conn.execute(
            "SELECT user_id, entity_type, entity_id FROM mutation_receipts "
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
        return str(existing["entity_type"]), int(existing["entity_id"])

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

    async def _assert_source_not_cross_owner(
        self, user_id: int, source_type: object, source_id: object
    ) -> None:
        """Fail closed if a private source id belongs to a *different* user.

        Enforced inside the caller's write transaction so a cross-owner reference
        never lands. A non-existent id is allowed on purpose — a completed
        snapshot may outlive a deleted source — so only an id that currently
        exists under another owner is rejected. Must be called while the
        connection lock is held.
        """
        if source_id is None or source_type not in ("food", "recipe"):
            return
        table = "foods" if source_type == "food" else "recipes"
        cursor = await self.conn.execute(
            f"SELECT user_id FROM {table} WHERE id = ?",  # noqa: S608
            (source_id,),
        )
        row = await cursor.fetchone()
        if row is not None and row["user_id"] != user_id:
            raise ValueError(
                f"{source_type} source does not belong to the acting user."
            )

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
        async with self._write_operation():
            replayed = await self._replayed_entity_id(source, user_id, "diet_log")
            if replayed is not None:
                return replayed
            diet_log_id = await self._insert_diet_meal_locked(
                user_id, meal_type, items
            )
            if source is not None:
                await self._record_receipt(
                    source, user_id, "diet_log", "diet", diet_log_id
                )
            return diet_log_id

    async def _insert_diet_meal_locked(
        self,
        user_id: int,
        meal_type: str,
        items: Sequence[Mapping[str, Any]],
    ) -> int:
        """Insert one meal header plus its children; return the new meal id.

        The single write path for every *new* resolved meal — guided Save, Quick
        log, and current-value replay all land here — so the item cap, aggregate
        bounds, header display bounding, and cross-owner source rejection cannot
        drift apart between entry points. Assumes the caller holds the connection
        lock and owns the transaction, and records no receipt: the caller decides
        which operation key this write belongs to.
        """
        if not items:
            raise ValueError("A meal must have at least one item.")
        if len(items) > MAX_MEAL_ITEMS:
            raise ValueError(f"A meal can have at most {MAX_MEAL_ITEMS} items.")

        def _raw_total(field: str) -> float | None:
            values = [item.get(field) for item in items]
            if any(value is None for value in values):
                return None
            return sum(float(value) for value in values)

        # Route the aggregate through the same finalizer a single meal uses, so a
        # multi-item meal cannot bypass the per-meal calorie/macro bounds. This is
        # the single rounding authority for the stored header totals and raises
        # NutritionError if the summed totals exceed the limits.
        totals = finalize_log_nutrients(
            {
                field: _raw_total(field)
                for field in ("calories", "protein_g", "carbs_g", "fat_g")
            }
        )

        display = ", ".join(str(item["display_name"]) for item in items)
        if len(display) > 500:
            display = display[:499] + "…"

        # Reject any item that cites another user's private food/recipe before
        # writing the header, so a bad reference makes no partial meal.
        for item in items:
            await self._assert_source_not_cross_owner(
                user_id, item.get("source_type"), item.get("source_id")
            )
        cursor = await self.conn.execute(
            "INSERT INTO diet_logs "
            "(user_id, meal_type, food_items, calories, protein_g, carbs_g, "
            "fat_g, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                meal_type,
                display,
                totals["calories"],
                totals["protein_g"],
                totals["carbs_g"],
                totals["fat_g"],
                _utc_timestamp_now(),
            ),
        )
        diet_log_id: int = cursor.lastrowid  # type: ignore[assignment]
        for order, item in enumerate(items):
            await self.conn.execute(
                "INSERT INTO diet_log_items "
                "(user_id, diet_log_id, item_order, source_type, source_id, "
                "source_provider, source_revision, display_name, "
                "entered_amount, entered_unit, resolved_base_amount, "
                "resolved_base_unit, calories, protein_g, carbs_g, fat_g) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    diet_log_id,
                    order,
                    str(item.get("source_type", "freetext")),
                    item.get("source_id"),
                    item.get("source_provider"),
                    item.get("source_revision"),
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

    # -------------------------------------------------------------------
    # Exact Repeat and targeted Undo (Phase 1b fast mutations)
    # -------------------------------------------------------------------
    # The columns Repeat copies verbatim from the previous meal's children.
    # Deliberately explicit: `id`, `diet_log_id`, and `created_at` belong to the
    # *new* row, and a future column must be considered rather than inherited.
    _REPEATED_ITEM_COLUMNS = (
        "item_order",
        "source_type",
        "source_id",
        "source_provider",
        "source_revision",
        "display_name",
        "entered_amount",
        "entered_unit",
        "resolved_base_amount",
        "resolved_base_unit",
        "calories",
        "protein_g",
        "carbs_g",
        "fat_g",
    )

    async def _get_meal_receipt_locked(
        self, user_id: int, meal_id: int
    ) -> MealReceipt | None:
        """Load one owner-scoped meal as an immutable header/items snapshot.

        Assumes the caller holds the connection lock.
        """
        cursor = await self.conn.execute(
            "SELECT * FROM diet_logs WHERE id = ? AND user_id = ?",
            (meal_id, user_id),
        )
        header = await cursor.fetchone()
        if header is None:
            return None
        cursor = await self.conn.execute(
            "SELECT * FROM diet_log_items WHERE user_id = ? AND diet_log_id = ? "
            "ORDER BY item_order, id",
            (user_id, meal_id),
        )
        children = await cursor.fetchall()
        return MealReceipt(
            header=_header_snapshot(header),
            items=tuple(_item_snapshot(row) for row in children),
        )

    async def repeat_last_meal(
        self, user_id: int, source: MutationSource | None = None
    ) -> RepeatResult:
        """Re-log this user's most recent meal as an exact copy.

        "Exact" is the whole point: the stored header (meal type, description,
        calories, macros) and every child snapshot are copied verbatim, with only
        a new id, parent, and timestamp. Nothing is re-resolved, so a catalog edit
        or a deleted food since the original meal cannot change what Repeat logs —
        that is what the separate "use current values" action is for.

        The operation is idempotent per Telegram update: a redelivered update
        replays its recorded outcome instead of logging a second meal, including
        the "there was nothing to repeat" case, which is recorded as a tombstone
        so a later replay stays empty even if a meal has been logged since.
        """
        async with self._write_operation(begin_immediate=True):
            recorded = await self._get_mutation_receipt_locked(
                source, user_id, "diet_repeat"
            )
            if recorded is not None:
                entity_type, entity_id = recorded
                if entity_type == "diet_repeat_empty":
                    return RepeatResult(status=RepeatStatus.EMPTY, receipt=None)
                if entity_type != "diet":
                    raise RuntimeError(
                        f"Unexpected diet_repeat receipt type {entity_type!r}"
                    )
                replayed = await self._get_meal_receipt_locked(user_id, entity_id)
                if replayed is None:
                    # The repeated meal was undone. Never recreate it: the user
                    # deliberately removed that exact row.
                    return RepeatResult(
                        status=RepeatStatus.REPLAYED_REMOVED, receipt=None
                    )
                return RepeatResult(status=RepeatStatus.REPLAYED, receipt=replayed)

            cursor = await self.conn.execute(
                "SELECT id, meal_type, food_items, calories, protein_g, carbs_g, "
                "fat_g FROM diet_logs WHERE user_id = ? "
                "ORDER BY logged_at DESC, id DESC LIMIT 1",
                (user_id,),
            )
            previous = await cursor.fetchone()
            if previous is None:
                if source is not None:
                    await self._record_receipt(
                        source, user_id, "diet_repeat", "diet_repeat_empty", 0
                    )
                return RepeatResult(status=RepeatStatus.EMPTY, receipt=None)

            now = _utc_timestamp_now()
            cursor = await self.conn.execute(
                "INSERT INTO diet_logs "
                "(user_id, meal_type, food_items, calories, protein_g, carbs_g, "
                "fat_g, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    previous["meal_type"],
                    previous["food_items"],
                    previous["calories"],
                    previous["protein_g"],
                    previous["carbs_g"],
                    previous["fat_g"],
                    now,
                ),
            )
            new_meal_id: int = cursor.lastrowid  # type: ignore[assignment]

            columns = ", ".join(self._REPEATED_ITEM_COLUMNS)
            cursor = await self.conn.execute(
                f"SELECT {columns} FROM diet_log_items "  # noqa: S608
                "WHERE user_id = ? AND diet_log_id = ? ORDER BY item_order, id",
                (user_id, previous["id"]),
            )
            placeholders = ", ".join("?" for _ in self._REPEATED_ITEM_COLUMNS)
            for child in await cursor.fetchall():
                await self.conn.execute(
                    "INSERT INTO diet_log_items "
                    f"(user_id, diet_log_id, {columns}, created_at) "  # noqa: S608
                    f"VALUES (?, ?, {placeholders}, ?)",
                    (
                        user_id,
                        new_meal_id,
                        *(child[column] for column in self._REPEATED_ITEM_COLUMNS),
                        now,
                    ),
                )

            if source is not None:
                await self._record_receipt(
                    source, user_id, "diet_repeat", "diet", new_meal_id
                )
            created = await self._get_meal_receipt_locked(user_id, new_meal_id)
            return RepeatResult(status=RepeatStatus.CREATED, receipt=created)

    # -------------------------------------------------------------------
    # "Use current values" — re-resolve a past meal against today's sources
    # -------------------------------------------------------------------
    @staticmethod
    def _snapshot_as_input(item: DietItemSnapshot) -> DietLogItemInput:
        """The persisted payload of a historical child, ready to be re-inserted."""
        return DietLogItemInput(
            source_type=item.source_type,
            source_id=item.source_id,
            source_provider=item.source_provider,
            source_revision=item.source_revision,
            display_name=item.display_name,
            entered_amount=item.entered_amount,
            entered_unit=item.entered_unit,
            resolved_base_amount=item.resolved_base_amount,
            resolved_base_unit=item.resolved_base_unit,
            calories=item.calories,
            protein_g=item.protein_g,
            carbs_g=item.carbs_g,
            fat_g=item.fat_g,
        )

    @staticmethod
    def _input_as_row(item: DietLogItemInput) -> dict[str, Any]:
        """A ``DietLogItemInput`` as the mapping the insert helper expects."""
        return {
            "source_type": str(item.source_type),
            "source_id": item.source_id,
            "source_provider": item.source_provider,
            "source_revision": item.source_revision,
            "display_name": item.display_name,
            "entered_amount": item.entered_amount,
            "entered_unit": item.entered_unit,
            "resolved_base_amount": item.resolved_base_amount,
            "resolved_base_unit": item.resolved_base_unit,
            "calories": item.calories,
            "protein_g": item.protein_g,
            "carbs_g": item.carbs_g,
            "fat_g": item.fat_g,
        }

    @staticmethod
    def _totals_of(items: Sequence[DietLogItemInput]) -> NutrientValues:
        """Sum nutrients, keeping ``None`` (unknown) contagious rather than zero."""

        def total(field: str) -> float | None:
            values = [getattr(item, field) for item in items]
            if not values or any(value is None for value in values):
                return None
            return round(sum(float(value) for value in values), 2)

        calories = total("calories")
        return NutrientValues(
            calories=None if calories is None else int(round(calories)),
            protein_g=total("protein_g"),
            carbs_g=total("carbs_g"),
            fat_g=total("fat_g"),
        )

    async def _build_current_value_preview_locked(
        self,
        user_id: int,
        source_meal_id: int,
        decisions: Mapping[int, CurrentValueDecision],
    ) -> CurrentValuePreview | None:
        """Re-resolve one past meal's items against the sources as they are now.

        Every structured child is re-priced from its live source using the
        *stored* entered amount; a child whose source or amount no longer works
        becomes an issue the user must resolve by hand. Nothing is guessed:
        there is no automatic fallback to the old snapshot, because silently
        logging stale numbers is exactly what this feature exists to avoid.
        ``freetext`` children have no live source, so they carry through
        unchanged. Assumes the caller holds the connection lock.
        """
        receipt = await self._get_meal_receipt_locked(user_id, source_meal_id)
        if receipt is None:
            return None

        proposals: list[CurrentValueItemProposal] = []
        for item in receipt.items:
            issue: CurrentValueIssue | None = None
            proposed: DietLogItemInput | None = None

            if item.source_type is DietItemSourceType.FREETEXT:
                proposed = self._snapshot_as_input(item)
            elif item.source_id is None:
                issue = CurrentValueIssue(
                    item.child_id, CurrentValueIssueCode.SOURCE_MISSING
                )
            elif item.entered_amount is None or item.entered_unit is None:
                issue = CurrentValueIssue(
                    item.child_id, CurrentValueIssueCode.QUANTITY_MISSING
                )
            else:
                tokens = [
                    f"{float(item.entered_amount):g}",
                    str(item.entered_unit),
                ]
                try:
                    entry = await self._resolve_quantity_locked(
                        user_id, str(item.source_type), item.source_id, tokens
                    )
                except LookupError:
                    issue = CurrentValueIssue(
                        item.child_id, CurrentValueIssueCode.SOURCE_MISSING
                    )
                except NutritionError as exc:
                    code = (
                        CurrentValueIssueCode.RECIPE_EMPTY
                        if "no ingredients" in str(exc)
                        else CurrentValueIssueCode.QUANTITY_INVALID
                    )
                    issue = CurrentValueIssue(item.child_id, code)
                else:
                    row = entry.as_item()
                    proposed = DietLogItemInput(
                        source_type=DietItemSourceType(str(row["source_type"])),
                        source_id=row["source_id"],
                        source_provider=row["source_provider"],
                        source_revision=row["source_revision"],
                        display_name=str(row["display_name"]),
                        entered_amount=row["entered_amount"],
                        entered_unit=row["entered_unit"],
                        resolved_base_amount=row["resolved_base_amount"],
                        resolved_base_unit=row["resolved_base_unit"],
                        calories=row["calories"],
                        protein_g=row["protein_g"],
                        carbs_g=row["carbs_g"],
                        fat_g=row["fat_g"],
                    )

            if issue is None:
                decision = CurrentValueDecision.RESOLVE_CURRENT
            else:
                # An issue needs an explicit human choice; anything the caller
                # did not decide stays UNRESOLVED and blocks saving.
                chosen = decisions.get(item.child_id)
                if chosen is CurrentValueDecision.KEEP_ORIGINAL:
                    decision = chosen
                    proposed = self._snapshot_as_input(item)
                elif chosen is CurrentValueDecision.REMOVE:
                    decision = chosen
                    proposed = None
                else:
                    decision = CurrentValueDecision.UNRESOLVED

            proposals.append(
                CurrentValueItemProposal(
                    source_child_id=item.child_id,
                    decision=decision,
                    original=item,
                    persisted_item_order=None,
                    proposed=proposed,
                    issue=issue,
                )
            )

        kept = [
            p.proposed
            for p in proposals
            if p.decision is not CurrentValueDecision.REMOVE and p.proposed is not None
        ]
        # Assign the order the items would actually be written in.
        ordered: list[CurrentValueItemProposal] = []
        position = 0
        for proposal in proposals:
            if (
                proposal.decision is CurrentValueDecision.REMOVE
                or proposal.proposed is None
            ):
                ordered.append(proposal)
                continue
            ordered.append(
                replace(proposal, persisted_item_order=position)
            )
            position += 1

        original_totals = receipt.header.nutrients
        proposed_totals = self._totals_of(kept)

        def delta(new: float | None, old: float | None):
            if new is None or old is None:
                return None
            return round(new - old, 2)

        preview = CurrentValuePreview(
            source_meal_id=source_meal_id,
            meal_type=receipt.header.meal_type,
            items=tuple(ordered),
            original_totals=original_totals,
            proposed_totals=proposed_totals,
            delta=NutrientValues(
                calories=(
                    None
                    if proposed_totals.calories is None
                    or original_totals.calories is None
                    else proposed_totals.calories - original_totals.calories
                ),
                protein_g=delta(proposed_totals.protein_g, original_totals.protein_g),
                carbs_g=delta(proposed_totals.carbs_g, original_totals.carbs_g),
                fat_g=delta(proposed_totals.fat_g, original_totals.fat_g),
            ),
            digest="",
            can_save=bool(kept)
            and not any(
                p.decision is CurrentValueDecision.UNRESOLVED for p in ordered
            ),
        )
        return replace(preview, digest=preview_signature(preview))

    async def get_current_value_preview(
        self,
        user_id: int,
        source_meal_id: int,
        decisions: Mapping[int, CurrentValueDecision] | None = None,
    ) -> CurrentValuePreview | None:
        """Preview what re-logging a past meal at today's values would produce."""
        async with self._read_operation():
            return await self._build_current_value_preview_locked(
                user_id, source_meal_id, decisions or {}
            )

    async def commit_current_value_meal(
        self,
        user_id: int,
        source_meal_id: int,
        decisions: Mapping[int, CurrentValueDecision],
        expected_digest: str,
        *,
        source: MutationSource | None = None,
    ) -> CurrentValueCommitResult:
        """Write the re-resolved meal, but only if it still matches the preview.

        The whole point is that the numbers are re-read from live sources, so the
        preview must be re-derived inside this transaction and compared with what
        the user actually saw. Any drift returns ``REVIEW_REQUIRED`` with the new
        preview and writes nothing — the user confirms the change rather than
        discovering it in their ledger.
        """
        async with self._write_operation(begin_immediate=True):
            recorded = await self._get_mutation_receipt_locked(
                source, user_id, "diet_current"
            )
            if recorded is not None:
                entity_type, entity_id = recorded
                if entity_type != "diet":
                    raise RuntimeError(
                        f"Unexpected diet_current receipt type {entity_type!r}"
                    )
                replayed = await self._get_meal_receipt_locked(user_id, entity_id)
                if replayed is None:
                    return CurrentValueCommitResult(
                        status=CurrentCommitStatus.REPLAYED_REMOVED,
                        receipt=None,
                        preview=None,
                    )
                return CurrentValueCommitResult(
                    status=CurrentCommitStatus.REPLAYED,
                    receipt=replayed,
                    preview=None,
                )

            preview = await self._build_current_value_preview_locked(
                user_id, source_meal_id, decisions
            )
            if preview is None:
                return CurrentValueCommitResult(
                    status=CurrentCommitStatus.SOURCE_MEAL_REMOVED,
                    receipt=None,
                    preview=None,
                )
            if not preview.can_save or preview.digest != expected_digest:
                return CurrentValueCommitResult(
                    status=CurrentCommitStatus.REVIEW_REQUIRED,
                    receipt=None,
                    preview=preview,
                )

            rows = [
                self._input_as_row(p.proposed)
                for p in preview.items
                if p.decision is not CurrentValueDecision.REMOVE
                and p.proposed is not None
            ]
            meal_id = await self._insert_diet_meal_locked(
                user_id, preview.meal_type, rows
            )
            if source is not None:
                await self._record_receipt(
                    source, user_id, "diet_current", "diet", meal_id
                )
            return CurrentValueCommitResult(
                status=CurrentCommitStatus.CREATED,
                receipt=await self._get_meal_receipt_locked(user_id, meal_id),
                preview=None,
            )

    async def delete_meal_if_recent(
        self,
        user_id: int,
        meal_id: int,
        *,
        now_utc: datetime | None = None,
    ) -> UndoResult:
        """Delete one exact meal of this user's, if it is at most 24h old.

        Targeted rather than "latest": a receipt's Undo button must always remove
        the meal it was rendered for, never a newer one logged since. Repeating
        the same Undo is a harmless no-op (``ALREADY_REMOVED``), as is undoing a
        meal that does not exist or belongs to someone else — the caller learns
        nothing about another user's rows. Children cascade; the mutation receipt
        is kept on purpose, so a redelivered Repeat stays undone.
        """
        moment = now_utc or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        async with self._write_operation(begin_immediate=True):
            cursor = await self.conn.execute(
                "SELECT * FROM diet_logs WHERE id = ? AND user_id = ?",
                (meal_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return UndoResult(
                    status=UndoStatus.ALREADY_REMOVED,
                    meal_id=meal_id,
                    deleted_header=None,
                )
            header = _header_snapshot(row)
            if header.logged_at_utc is None:
                # Recency is unprovable, so refuse rather than delete an
                # arbitrarily old row.
                return UndoResult(
                    status=UndoStatus.EXPIRED, meal_id=meal_id, deleted_header=None
                )
            # Exactly 24h remains eligible, matching the /undo boundary.
            if (moment - header.logged_at_utc) > timedelta(hours=24):
                return UndoResult(
                    status=UndoStatus.EXPIRED, meal_id=meal_id, deleted_header=None
                )
            cursor = await self.conn.execute(
                "DELETE FROM diet_logs WHERE id = ? AND user_id = ?",
                (meal_id, user_id),
            )
            if cursor.rowcount <= 0:
                return UndoResult(
                    status=UndoStatus.ALREADY_REMOVED,
                    meal_id=meal_id,
                    deleted_header=None,
                )
            return UndoResult(
                status=UndoStatus.DELETED, meal_id=meal_id, deleted_header=header
            )

    # -------------------------------------------------------------------
    # Shared curated catalog (Phase 5) — reference data, not owner-scoped
    # -------------------------------------------------------------------
    async def seed_catalog(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """Reconcile the curated catalog to this complete provider snapshot.

        The bundled manifest is authoritative for its own provider namespace, so
        seeding is a snapshot reconciliation rather than an upsert-only refresh:
        present foods are upserted and (re)activated, each touched food's portions
        and aliases are replaced so a removed/changed child cannot linger, and
        foods absent from the manifest are soft-deactivated. Completed diet-log
        snapshots are unaffected because they store their own resolved nutrition.
        Keyed by ``(provider, provider_food_id)`` so re-running is idempotent.
        """
        from .catalog_seed import CATALOG_PROVIDER, CATALOG_REVISION

        async with self._write_operation():
            seen_provider_ids: list[Any] = []
            for entry in entries:
                display, name_key = normalize_catalog_name(entry["display_name"])
                await self.conn.execute(
                    "INSERT INTO catalog_foods (provider, provider_food_id, "
                    "provider_revision, display_name, name_key, category, "
                    "base_unit, basis_amount, calories, protein_g, carbs_g, fat_g) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(provider, provider_food_id) DO UPDATE SET "
                    "provider_revision = excluded.provider_revision, "
                    "display_name = excluded.display_name, "
                    "name_key = excluded.name_key, category = excluded.category, "
                    "base_unit = excluded.base_unit, "
                    "basis_amount = excluded.basis_amount, "
                    "calories = excluded.calories, protein_g = excluded.protein_g, "
                    "carbs_g = excluded.carbs_g, fat_g = excluded.fat_g, "
                    "is_active = 1, updated_at = ?",
                    (
                        CATALOG_PROVIDER,
                        entry["provider_food_id"],
                        CATALOG_REVISION,
                        display,
                        name_key,
                        entry.get("category"),
                        entry["base_unit"],
                        entry["basis_amount"],
                        entry.get("calories"),
                        entry.get("protein_g"),
                        entry.get("carbs_g"),
                        entry.get("fat_g"),
                        _utc_timestamp_now(),
                    ),
                )
                seen_provider_ids.append(entry["provider_food_id"])
                row = await self._query_one(
                    "SELECT id FROM catalog_foods "
                    "WHERE provider = ? AND provider_food_id = ?",
                    (CATALOG_PROVIDER, entry["provider_food_id"]),
                )
                catalog_id = row["id"]
                # Replace this food's child sets so a withdrawn portion/alias in a
                # newer manifest cannot remain resolvable.
                await self.conn.execute(
                    "DELETE FROM catalog_portions WHERE catalog_food_id = ?",
                    (catalog_id,),
                )
                for portion in entry.get("portions", ()):
                    p_display, p_key = normalize_catalog_name(
                        portion["name"], max_length=MAX_PORTION_NAME_LENGTH
                    )
                    await self.conn.execute(
                        "INSERT INTO catalog_portions "
                        "(catalog_food_id, name, name_key, base_amount) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(catalog_food_id, name_key) DO UPDATE SET "
                        "base_amount = excluded.base_amount",
                        (catalog_id, p_display, p_key, portion["base_amount"]),
                    )
                await self.conn.execute(
                    "DELETE FROM catalog_aliases WHERE catalog_food_id = ?",
                    (catalog_id,),
                )
                for alias in entry.get("aliases", ()):
                    a_display, a_key = normalize_catalog_name(alias)
                    await self.conn.execute(
                        "INSERT INTO catalog_aliases "
                        "(catalog_food_id, alias, alias_key) VALUES (?, ?, ?) "
                        "ON CONFLICT(catalog_food_id, alias_key) DO NOTHING",
                        (catalog_id, a_display, a_key),
                    )

            # Soft-deactivate any curated food not present in this manifest.
            if seen_provider_ids:
                placeholders = ", ".join("?" for _ in seen_provider_ids)
                await self.conn.execute(
                    "UPDATE catalog_foods SET is_active = 0, updated_at = ? "
                    f"WHERE provider = ? AND provider_food_id NOT IN ({placeholders})",  # noqa: S608
                    (_utc_timestamp_now(), CATALOG_PROVIDER, *seen_provider_ids),
                )
            else:
                await self.conn.execute(
                    "UPDATE catalog_foods SET is_active = 0, updated_at = ? "
                    "WHERE provider = ?",
                    (_utc_timestamp_now(), CATALOG_PROVIDER),
                )

    async def search_catalog(
        self, query: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Find active catalog foods by name or alias (exact/prefix/substring).

        Shared reference data, so not owner-scoped. Raises ``NutritionError`` via
        :func:`normalize_catalog_name` when the query is empty or unsupported.
        """
        _display, key = normalize_catalog_name(query, "Search")
        like = f"%{key}%"
        prefix = f"{key}%"
        rows = await self._query_all(
            """
            SELECT cf.*, cf.display_name AS name
            FROM catalog_foods AS cf
            WHERE cf.is_active = 1
              AND (
                cf.name_key LIKE ?
                OR cf.id IN (
                    SELECT catalog_food_id FROM catalog_aliases
                    WHERE alias_key LIKE ?
                )
              )
            ORDER BY (cf.name_key = ?) DESC, (cf.name_key LIKE ?) DESC, cf.name_key
            LIMIT ?
            """,
            (like, like, key, prefix, limit),
        )
        return [dict(row) for row in rows]

    async def get_catalog_food(self, catalog_id: int) -> dict[str, Any] | None:
        """Return one active catalog food (with a ``name`` alias for the resolver)."""
        row = await self._query_one(
            "SELECT *, display_name AS name FROM catalog_foods "
            "WHERE id = ? AND is_active = 1",
            (catalog_id,),
        )
        return dict(row) if row is not None else None

    async def get_catalog_portions(self, catalog_id: int) -> list[dict[str, Any]]:
        """Return a catalog food's named portions."""
        rows = await self._query_all(
            "SELECT * FROM catalog_portions WHERE catalog_food_id = ? "
            "ORDER BY name_key, id",
            (catalog_id,),
        )
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------
    # Suggestion signal (Phase 4) — all owner-scoped, from completed logs
    # -------------------------------------------------------------------
    async def get_diet_item_stats(
        self, user_id: int, meal_type: str
    ) -> dict[tuple[str, int], dict[str, Any]]:
        """Per-source usage counts and last-used time from completed meals.

        Keyed by ``(source_type, source_id)``. ``meal_uses`` counts only meals of
        ``meal_type``; ``total_uses`` counts all meals. Derived purely from saved
        history, never from exploratory taps.
        """
        rows = await self._query_all(
            """
            SELECT dli.source_type AS source_type, dli.source_id AS source_id,
                   COUNT(*) AS total_uses,
                   SUM(CASE WHEN dl.meal_type = ? THEN 1 ELSE 0 END) AS meal_uses,
                   MAX(dl.logged_at) AS last_used
            FROM diet_log_items AS dli
            JOIN diet_logs AS dl
                ON dl.id = dli.diet_log_id AND dl.user_id = dli.user_id
            WHERE dli.user_id = ?
              AND dli.source_type IN ('food', 'recipe', 'catalog')
              AND dli.source_id IS NOT NULL
            GROUP BY dli.source_type, dli.source_id
            """,
            (meal_type, user_id),
        )
        return {
            (row["source_type"], row["source_id"]): dict(row) for row in rows
        }

    async def get_user_catalog_history(
        self, user_id: int
    ) -> list[dict[str, Any]]:
        """Active shared-catalog foods this user has actually logged before.

        Suggestions must never enumerate the whole curated catalog — that is what
        Search is for. Only foods with a completed meal behind them earn a place
        in the picker.
        """
        rows = await self._query_all(
            """
            SELECT cf.*, cf.display_name AS name
            FROM catalog_foods AS cf
            WHERE cf.is_active = 1
              AND EXISTS (
                  SELECT 1
                  FROM diet_log_items AS dli
                  JOIN diet_logs AS dl
                    ON dl.id = dli.diet_log_id AND dl.user_id = dli.user_id
                  WHERE dli.user_id = ?
                    AND dli.source_type = 'catalog'
                    AND dli.source_id = cf.id
              )
            ORDER BY cf.name_key, cf.id
            """,
            (user_id,),
        )
        return [dict(row) for row in rows]

    async def get_recent_item_quantities(
        self,
        user_id: int,
        source_type: str,
        source_id: int,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """A source's most recent distinct entered quantities (owner-scoped)."""
        rows = await self._query_all(
            """
            SELECT dli.entered_amount AS entered_amount,
                   dli.entered_unit AS entered_unit,
                   MAX(dl.logged_at) AS last_used
            FROM diet_log_items AS dli
            JOIN diet_logs AS dl
                ON dl.id = dli.diet_log_id AND dl.user_id = dli.user_id
            WHERE dli.user_id = ? AND dli.source_type = ? AND dli.source_id = ?
              AND dli.entered_amount IS NOT NULL AND dli.entered_unit IS NOT NULL
            GROUP BY dli.entered_amount, dli.entered_unit
            ORDER BY last_used DESC
            LIMIT ?
            """,
            (user_id, source_type, source_id, limit),
        )
        return [dict(row) for row in rows]

    async def get_food_preferences(
        self, user_id: int
    ) -> dict[tuple[str, int], dict[str, Any]]:
        """All of a user's food/recipe preferences, keyed by source."""
        rows = await self._query_all(
            "SELECT * FROM user_food_preferences WHERE user_id = ?", (user_id,)
        )
        return {
            (row["source_type"], row["source_id"]): dict(row) for row in rows
        }

    async def get_food_preference(
        self, user_id: int, source_type: str, source_id: int
    ) -> dict[str, Any] | None:
        """One source's preference row, or ``None`` if never set."""
        row = await self._query_one(
            "SELECT * FROM user_food_preferences "
            "WHERE user_id = ? AND source_type = ? AND source_id = ?",
            (user_id, source_type, source_id),
        )
        return dict(row) if row is not None else None

    async def set_food_preference(
        self,
        user_id: int,
        source_type: str,
        source_id: int,
        *,
        is_pinned: bool | None = None,
        hidden: bool | None = None,
    ) -> None:
        """Upsert a source's pin/hide flags. Enforces mutual exclusivity and active sources."""
        if source_type not in ("food", "recipe"):
            raise ValueError(f"Unknown source_type {source_type!r}")
        async with self._write_operation():
            await self._assert_source_not_cross_owner(user_id, source_type, source_id)
            
            if is_pinned or hidden:
                table = "foods" if source_type == "food" else "recipes"
                cursor = await self.conn.execute(
                    f"SELECT is_active, user_id FROM {table} WHERE id = ?",
                    (source_id,)
                )
                row = await cursor.fetchone()
                if not row or not row["is_active"] or row["user_id"] != user_id:
                    raise ValueError(f"Cannot set true preference on missing or inactive {source_type}.")

            await self.conn.execute(
                "INSERT INTO user_food_preferences (user_id, source_type, source_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, source_type, source_id) DO NOTHING",
                (user_id, source_type, source_id),
            )
            if is_pinned is not None:
                hidden_sql = ", hidden = 0" if is_pinned else ""
                await self.conn.execute(
                    f"UPDATE user_food_preferences SET is_pinned = ?, updated_at = ?{hidden_sql} "
                    "WHERE user_id = ? AND source_type = ? AND source_id = ?",
                    (1 if is_pinned else 0, _utc_timestamp_now(),
                     user_id, source_type, source_id),
                )
            elif hidden is not None:
                pinned_sql = ", is_pinned = 0" if hidden else ""
                await self.conn.execute(
                    f"UPDATE user_food_preferences SET hidden = ?, updated_at = ?{pinned_sql} "
                    "WHERE user_id = ? AND source_type = ? AND source_id = ?",
                    (1 if hidden else 0, _utc_timestamp_now(),
                     user_id, source_type, source_id),
                )

    # -------------------------------------------------------------------
    # Private default quantities (Phase 1b "log my usual")
    # -------------------------------------------------------------------
    async def _resolve_quantity_locked(
        self,
        user_id: int,
        source_type: str,
        source_id: int,
        tokens: Sequence[str],
    ):
        """Resolve ``tokens`` against a currently active, owner-scoped source.

        The single validation boundary for defaults and quick logs: the shared
        nutrition resolvers decide what a valid amount/unit is (finite, positive,
        bounded, a known metric alias, a named portion, the recipe's yield unit),
        so no handler ever invents its own rules. Raises ``NutritionError`` for an
        unusable quantity and ``LookupError`` when the source itself is gone.

        Imported lazily because the resolvers are pure functions that happen to
        live in the handler package; importing them at module scope would make
        the database module depend on the Telegram layer.
        """
        from .handlers.catalog import (
            resolve_catalog_food_entry,
            resolve_food_diet_entry,
            resolve_recipe_diet_entry,
        )

        # The read helpers below re-enter the already-held connection lock rather
        # than re-acquiring it, so these reads join the caller's transaction and
        # see exactly the rows the write is about to act on.
        if source_type == "food":
            food = await self.get_food_by_id(user_id, source_id)
            if food is None:
                raise LookupError("food")
            portions = await self.get_food_portions(user_id, source_id)
            return resolve_food_diet_entry(food, portions, tokens)

        if source_type == "recipe":
            recipe = await self.get_recipe_by_id(user_id, source_id)
            if recipe is None:
                raise LookupError("recipe")
            ingredients = await self.get_recipe_ingredients(user_id, source_id)
            return resolve_recipe_diet_entry(recipe, ingredients, tokens)

        if source_type == "catalog":
            catalog_food = await self.get_catalog_food(source_id)
            if catalog_food is None:
                raise LookupError("catalog")
            portions = await self.get_catalog_portions(source_id)
            return resolve_catalog_food_entry(catalog_food, portions, tokens)

        raise ValueError(f"Unknown source_type {source_type!r}")

    async def resolve_quantity(
        self,
        user_id: int,
        source_type: str,
        source_id: int,
        tokens: Sequence[str],
    ):
        """Resolve a quantity against a live source without writing anything.

        Lets a handler preview or validate an amount (a proposed default, say)
        using exactly the rules the write path will apply. Raises
        ``NutritionError`` for an unusable quantity, ``LookupError`` when the
        source is missing/archived/another user's.
        """
        async with self._read_operation():
            return await self._resolve_quantity_locked(
                user_id, source_type, source_id, tokens
            )

    async def get_default_quantity(
        self, user_id: int, source_type: str, source_id: int
    ) -> DefaultQuantity | None:
        """Return a complete stored default, or ``None``.

        A partial legacy pair (one column set, the other null) is deliberately
        reported as absent rather than half-used — an incomplete default must be
        repaired or removed, never guessed at.
        """
        row = await self._query_one(
            "SELECT default_amount, default_unit FROM user_food_preferences "
            "WHERE user_id = ? AND source_type = ? AND source_id = ?",
            (user_id, source_type, source_id),
        )
        if row is None:
            return None
        amount, unit = row["default_amount"], row["default_unit"]
        if amount is None or unit is None:
            return None
        return DefaultQuantity(amount=float(amount), unit=str(unit))

    async def has_partial_default(
        self, user_id: int, source_type: str, source_id: int
    ) -> bool:
        """Whether exactly one half of the default pair is stored (needs repair)."""
        row = await self._query_one(
            "SELECT default_amount, default_unit FROM user_food_preferences "
            "WHERE user_id = ? AND source_type = ? AND source_id = ?",
            (user_id, source_type, source_id),
        )
        if row is None:
            return False
        return (row["default_amount"] is None) != (row["default_unit"] is None)

    async def set_default_quantity(
        self,
        user_id: int,
        source_type: Literal["food", "recipe"],
        source_id: int,
        quantity: DefaultQuantity,
    ) -> DefaultQuantity:
        """Store a validated default amount/unit for one of this user's sources.

        The pair is stored only after the nutrition resolver accepts it against
        the source's *current* rows, and only as the normalized values the
        resolver produced — so a stored default is always something that resolved
        at least once. Pin/hide are preserved. Raises ``NutritionError`` for an
        unusable quantity and ``ValueError`` when the source is missing, archived,
        or another user's (both fail identically, revealing nothing).
        """
        if source_type not in ("food", "recipe"):
            raise ValueError(f"Unknown source_type {source_type!r}")
        tokens = [f"{float(quantity.amount):g}", str(quantity.unit)]
        async with self._write_operation(begin_immediate=True):
            try:
                entry = await self._resolve_quantity_locked(
                    user_id, source_type, source_id, tokens
                )
            except LookupError as exc:
                raise ValueError(
                    f"That {source_type} is no longer available."
                ) from exc
            stored = DefaultQuantity(
                amount=float(entry.entered_amount), unit=str(entry.entered_unit)
            )
            await self.conn.execute(
                "INSERT INTO user_food_preferences (user_id, source_type, source_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, source_type, source_id) DO NOTHING",
                (user_id, source_type, source_id),
            )
            await self.conn.execute(
                "UPDATE user_food_preferences "
                "SET default_amount = ?, default_unit = ?, updated_at = ? "
                "WHERE user_id = ? AND source_type = ? AND source_id = ?",
                (
                    stored.amount,
                    stored.unit,
                    _utc_timestamp_now(),
                    user_id,
                    source_type,
                    source_id,
                ),
            )
            return stored

    async def clear_default_quantity(
        self,
        user_id: int,
        source_type: Literal["food", "recipe"],
        source_id: int,
    ) -> bool:
        """Remove this user's stored default, keeping any pin/hide.

        Deliberately does *not* require the source to still exist: an invalid
        default left behind by an archived food must remain removable. Deletes
        the row only when nothing else is left on it. A missing preference is a
        harmless ``False``.
        """
        if source_type not in ("food", "recipe"):
            raise ValueError(f"Unknown source_type {source_type!r}")
        async with self._write_operation(begin_immediate=True):
            cursor = await self.conn.execute(
                "UPDATE user_food_preferences "
                "SET default_amount = NULL, default_unit = NULL, updated_at = ? "
                "WHERE user_id = ? AND source_type = ? AND source_id = ? "
                "AND (default_amount IS NOT NULL OR default_unit IS NOT NULL)",
                (_utc_timestamp_now(), user_id, source_type, source_id),
            )
            cleared = cursor.rowcount > 0
            await self.conn.execute(
                "DELETE FROM user_food_preferences "
                "WHERE user_id = ? AND source_type = ? AND source_id = ? "
                "AND default_amount IS NULL AND default_unit IS NULL "
                "AND is_pinned = 0 AND hidden = 0",
                (user_id, source_type, source_id),
            )
            return cleared

    async def create_quick_meal(
        self,
        user_id: int,
        meal_type: str,
        source_type: Literal["food", "recipe", "catalog"],
        source_id: int,
        *,
        quantity: DefaultQuantity | None = None,
        set_as_default: bool = False,
        source: MutationSource | None = None,
    ) -> QuickMealResult:
        """Log one complete single-item meal in a single transaction.

        Either everything lands — header, child, the optional new default, and the
        replay receipt — or nothing does. The quantity comes from the caller or
        from the stored default; there is no third fallback, because silently
        logging *some* amount is worse than asking. Every failure mode writes
        nothing and says which one it was, so the handler can open the right
        repair screen.
        """
        if source_type not in ("food", "recipe", "catalog"):
            raise ValueError(f"Unknown source_type {source_type!r}")
        if set_as_default and (source_type == "catalog" or quantity is None):
            raise ValueError("A default needs a private source and an amount.")

        async with self._write_operation(begin_immediate=True):
            recorded = await self._get_mutation_receipt_locked(
                source, user_id, "diet_quick"
            )
            if recorded is not None:
                entity_type, entity_id = recorded
                if entity_type != "diet":
                    raise RuntimeError(
                        f"Unexpected diet_quick receipt type {entity_type!r}"
                    )
                replayed = await self._get_meal_receipt_locked(user_id, entity_id)
                if replayed is None:
                    return QuickMealResult(
                        status=QuickMealStatus.REPLAYED_REMOVED, receipt=None
                    )
                return QuickMealResult(
                    status=QuickMealStatus.REPLAYED, receipt=replayed
                )

            supplied = quantity is not None
            if not supplied:
                if source_type == "catalog":
                    return QuickMealResult(
                        status=QuickMealStatus.QUANTITY_REQUIRED, receipt=None
                    )
                quantity = await self._get_default_quantity_locked(
                    user_id, source_type, source_id
                )
                if quantity is None:
                    return QuickMealResult(
                        status=QuickMealStatus.QUANTITY_REQUIRED, receipt=None
                    )

            tokens = [f"{float(quantity.amount):g}", str(quantity.unit)]
            try:
                entry = await self._resolve_quantity_locked(
                    user_id, source_type, source_id, tokens
                )
            except LookupError:
                return QuickMealResult(
                    status=QuickMealStatus.SOURCE_UNAVAILABLE, receipt=None
                )
            except NutritionError:
                # A supplied amount is the user's mistake to fix; a stored one is
                # a stale default that must be repaired rather than reused.
                return QuickMealResult(
                    status=(
                        QuickMealStatus.QUANTITY_INVALID
                        if supplied
                        else QuickMealStatus.DEFAULT_INVALID
                    ),
                    receipt=None,
                )

            meal_id = await self._insert_diet_meal_locked(
                user_id, meal_type, [entry.as_item()]
            )
            if set_as_default:
                await self.conn.execute(
                    "INSERT INTO user_food_preferences "
                    "(user_id, source_type, source_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, source_type, source_id) DO NOTHING",
                    (user_id, source_type, source_id),
                )
                await self.conn.execute(
                    "UPDATE user_food_preferences "
                    "SET default_amount = ?, default_unit = ?, updated_at = ? "
                    "WHERE user_id = ? AND source_type = ? AND source_id = ?",
                    (
                        float(entry.entered_amount),
                        str(entry.entered_unit),
                        _utc_timestamp_now(),
                        user_id,
                        source_type,
                        source_id,
                    ),
                )
            if source is not None:
                await self._record_receipt(
                    source, user_id, "diet_quick", "diet", meal_id
                )
            return QuickMealResult(
                status=QuickMealStatus.CREATED,
                receipt=await self._get_meal_receipt_locked(user_id, meal_id),
            )

    async def _get_default_quantity_locked(
        self, user_id: int, source_type: str, source_id: int
    ) -> DefaultQuantity | None:
        """Locked twin of :meth:`get_default_quantity` (complete pairs only)."""
        cursor = await self.conn.execute(
            "SELECT default_amount, default_unit FROM user_food_preferences "
            "WHERE user_id = ? AND source_type = ? AND source_id = ?",
            (user_id, source_type, source_id),
        )
        row = await cursor.fetchone()
        if row is None or row["default_amount"] is None or row["default_unit"] is None:
            return None
        return DefaultQuantity(
            amount=float(row["default_amount"]), unit=str(row["default_unit"])
        )

    async def reset_food_preferences(self, user_id: int) -> int:
        """Clear all of a user's pins/hides while preserving defaults. Returns count of cleared pins/hides."""
        async with self._write_operation():
            count_cursor = await self.conn.execute(
                "SELECT COUNT(*) as c FROM user_food_preferences WHERE user_id = ? AND (is_pinned = 1 OR hidden = 1)",
                (user_id,)
            )
            row = await count_cursor.fetchone()
            cleared_count = row["c"] if row else 0

            await self.conn.execute(
                "UPDATE user_food_preferences SET is_pinned = 0, hidden = 0 "
                "WHERE user_id = ? AND default_amount IS NOT NULL",
                (user_id,)
            )
            await self.conn.execute(
                "DELETE FROM user_food_preferences "
                "WHERE user_id = ? AND default_amount IS NULL",
                (user_id,)
            )
            return cleared_count

    async def get_suggestions_enabled(self, user_id: int) -> bool:
        """Whether personalized ordering is on (default on; missing row = on)."""
        row = await self._query_one(
            "SELECT suggestions_enabled FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        return row is None or bool(row["suggestions_enabled"])

    async def set_suggestions_enabled(self, user_id: int, enabled: bool) -> None:
        """Turn personalized ordering on or off (history is still recorded)."""
        async with self._write_operation():
            await self.conn.execute(
                "INSERT INTO user_settings (user_id) VALUES (?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            await self.conn.execute(
                "UPDATE user_settings SET suggestions_enabled = ?, updated_at = ? "
                "WHERE user_id = ?",
                (1 if enabled else 0, _utc_timestamp_now(), user_id),
            )

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
