"""Refuse a production migration that has no verified rollback point.

``DatabaseManager.init_db()`` calls ``migrations.run_migrations()``
unconditionally, applying every pending version immediately. A bad committed
migration is one of the few failures that cannot be fixed by an application retry
or a feature flag rollback: it needs a database restore. Until now the only
protection was a manual checklist in ``docs/backup_runbook.md``.

This module adds the enforcement at the **production executable boundary**:
``bot.main.post_init`` calls :func:`prepare_database_for_startup` after
``connect()`` and before ``init_db()``. ``run_migrations()`` stays the low-level
primitive that migration tests drive directly — it acquires no operational paths
and reads no deployment configuration — so nothing needed a
"skip-the-backup" flag. That matters concretely: ``tests/test_migrations.py`` and
its neighbours stamp an old ``user_version``, insert rows, and migrate, which is
indistinguishable from production by inspection. No "exempt an empty database"
heuristic could tell them apart, whereas entry-point placement separates them by
construction.

**Any future executable migration or administration entry point must call this
same function.** Calling ``run_migrations()`` directly is not an approved
production path.

The backup is created and verified through :mod:`ledger_backup` directly rather
than by spawning ``scripts/backup_db.py``, so both callers share one contract and
a subprocess failure cannot be mistaken for a verified backup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

import ledger_backup
from ledger_schema import LEGACY_UNVERSIONED, required_tables_for

from . import migrations

logger = logging.getLogger(__name__)


class MigrationPreflightError(RuntimeError):
    """Startup must not proceed: a pending migration has no verified backup."""


@dataclass(frozen=True)
class PreflightOutcome:
    """What the preflight decided, for logging and tests."""

    action: str
    source_version: int
    target_version: int
    backup_path: Path | None = None

    @property
    def migration_pending(self) -> bool:
        return self.source_version != self.target_version


async def prepare_database_for_startup(
    conn: aiosqlite.Connection,
    *,
    db_path: str,
    backup_dest_dir: str | None,
) -> PreflightOutcome:
    """Ensure a pending migration has a verified rollback point, or refuse.

    Assumes the caller already holds the single-instance lock (see
    :mod:`bot.instance_lock`), so no second process can be migrating the same
    file. Returns an outcome describing what happened; raises
    :class:`MigrationPreflightError` — leaving ``user_version`` untouched — when
    the migration must not proceed.
    """
    target = migrations.LATEST_VERSION
    source = await migrations.get_user_version(conn)

    if source > target:
        # Fail closed before creating a backup or changing anything: this build
        # does not know the newer schema's invariants, and a "backup" of it could
        # not be verified against any table contract we hold.
        raise migrations.UnsupportedSchemaError(
            f"Database schema version {source} is newer than this binary supports "
            f"({target}). Deploy the matching (or newer) application version; "
            "refusing to run against an unknown schema."
        )

    if source == target:
        logger.info("Schema already at version %d; no migration pending", target)
        return PreflightOutcome("current", source, target)

    if source == LEGACY_UNVERSIONED and not await _has_application_tables(conn, target):
        # A genuinely new database. Backing up an empty file before creating its
        # schema protects nothing, and would leave a meaningless rollback point.
        logger.info("New database detected; creating schema without a backup")
        return PreflightOutcome("new-database", source, target)

    destination = _require_destination(backup_dest_dir, source, target)
    backup = _create_pre_migration_backup(db_path, destination, source)

    if source == LEGACY_UNVERSIONED:
        # A populated pre-versioning database. Its shape is unknown, so the copy
        # cannot be certified against a table contract; rehearse the whole
        # migration on a throwaway copy and touch the live file only if that
        # rehearsal reaches and verifies the target.
        await _rehearse_migration(backup.path, target)
        logger.info(
            "Legacy version-0 migration rehearsed successfully on a temporary copy"
        )
        return PreflightOutcome("legacy-rehearsed", source, target, backup.path)

    logger.info(
        "Verified pre-migration backup at schema version %d; migrating to %d",
        source,
        target,
    )
    return PreflightOutcome("backed-up", source, target, backup.path)


async def _has_application_tables(conn: aiosqlite.Connection, target: int) -> bool:
    """Whether any table the target version requires already exists."""
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    present = {row[0] for row in await cursor.fetchall()}
    return bool(present & required_tables_for(target))


def _require_destination(
    backup_dest_dir: str | None, source: int, target: int
) -> Path:
    """Resolve the configured external destination, or refuse to start."""
    raw = (backup_dest_dir or "").strip()
    if not raw:
        raise MigrationPreflightError(
            f"Refusing to migrate schema {source} -> {target}: no backup "
            "destination is configured. Set BACKUP_DEST_DIR in .env to a directory "
            "outside the project root (see docs/backup_runbook.md), then start "
            "again. The database has not been changed."
        )
    try:
        destination = ledger_backup.resolve_destination(raw)
    except ledger_backup.BackupError as exc:
        raise MigrationPreflightError(
            f"Refusing to migrate schema {source} -> {target}: {exc} The database "
            "has not been changed."
        ) from exc
    return destination


def _create_pre_migration_backup(
    db_path: str, destination: Path, source: int
) -> ledger_backup.DatabaseFacts:
    """Create and verify the rollback copy at the source's own version."""
    try:
        return ledger_backup.create_backup_in(
            db_path,
            destination,
            expect_version=source,
            pre_migration=True,
            # A pre-migration rollback point is never subject to rolling
            # retention; only routine backups are pruned.
            keep=0,
        )
    except ledger_backup.BackupError as exc:
        raise MigrationPreflightError(
            f"Refusing to migrate: the pre-migration backup could not be created "
            f"or verified ({exc}). The database has not been changed."
        ) from exc


async def _rehearse_migration(backup_path: Path, target: int) -> None:
    """Migrate a throwaway copy of the backup and require it to reach ``target``.

    The rollback copy itself is never migrated: a rehearsal that mutated it would
    destroy the very artifact it exists to protect.
    """
    from .database import DatabaseManager

    rehearsal = ledger_backup.restore_rehearsal_copy(backup_path)
    manager = DatabaseManager(str(rehearsal))
    try:
        await manager.connect()
        try:
            # init_db() runs the real migration chain and verifies the resulting
            # schema, so reaching here means the rehearsal genuinely succeeded.
            await manager.init_db()
            reached = await migrations.get_user_version(manager.conn)
        finally:
            await manager.close()
        if reached != target:
            raise MigrationPreflightError(
                f"Refusing to migrate: the rehearsal on a copy of the backup "
                f"reached version {reached}, not {target}. The database has not "
                "been changed."
            )
    except MigrationPreflightError:
        raise
    except Exception as exc:
        raise MigrationPreflightError(
            f"Refusing to migrate: the rehearsal on a copy of the backup failed "
            f"({type(exc).__name__}: {exc}). The database has not been changed."
        ) from exc
    finally:
        _discard(rehearsal)


def _discard(path: Path) -> None:
    """Delete a rehearsal file and its WAL sidecars, ignoring what is absent."""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            logger.warning("Could not remove a temporary rehearsal file")
