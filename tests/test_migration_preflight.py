"""No production migration runs without a verified backup (plan §0.5).

The enforcement point is deliberately the production executable boundary, not
``run_migrations()``. These tests therefore drive
:func:`prepare_database_for_startup` and ``bot.main.post_init`` the way the real
process does, and one of them asserts the property that made this design
necessary: the existing migration suite — which stamps an old version, inserts
rows, and migrates — still passes with no exemption flag, because it never goes
through this path.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

import ledger_backup
from bot import main as main_module
from bot import migrations
from bot.database import DatabaseManager
from bot.migration_preflight import (
    MigrationPreflightError,
    prepare_database_for_startup,
)
from ledger_schema import LATEST_SCHEMA_VERSION, required_tables_for


@pytest.fixture
def outside_repo(tmp_path, monkeypatch):
    """Let ``tmp_path`` count as outside the repository."""
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    monkeypatch.setattr(ledger_backup, "PROJECT_ROOT", fake_root)
    return fake_root


async def _make_db_at(path: Path, version: int) -> None:
    """Build a real database migrated to ``version`` (via a pinned latest)."""
    manager = DatabaseManager(str(path))
    await manager.connect()
    try:
        if version == 0:
            return
        original = migrations.LATEST_VERSION
        migrations.LATEST_VERSION = version
        try:
            await manager.init_db()
        finally:
            migrations.LATEST_VERSION = original
    finally:
        await manager.close()


def _make_populated_legacy_db(path: Path) -> None:
    """A pre-versioning database: real user rows, ``user_version`` still 0."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, "
            "first_name TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (7, 'a', 'A')")
        conn.commit()
    finally:
        conn.close()


async def _preflight(path: Path, dest: Path | str | None):
    manager = DatabaseManager(str(path))
    await manager.connect()
    try:
        return await prepare_database_for_startup(
            manager.conn,
            db_path=str(path),
            backup_dest_dir=None if dest is None else str(dest),
        )
    finally:
        await manager.close()


# ---------------------------------------------------------------------------
# No migration pending
# ---------------------------------------------------------------------------
async def test_current_schema_needs_no_backup(tmp_path, outside_repo):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION)
    dest = tmp_path / "backups"

    outcome = await _preflight(db, dest)

    assert outcome.action == "current"
    assert not outcome.migration_pending
    assert outcome.backup_path is None
    assert not dest.exists()  # nothing created for a no-op start


async def test_empty_new_database_starts_without_a_meaningless_backup(
    tmp_path, outside_repo
):
    db = tmp_path / "ledger.db"
    dest = tmp_path / "backups"

    outcome = await _preflight(db, dest)

    assert outcome.action == "new-database"
    assert outcome.source_version == 0
    assert outcome.backup_path is None
    assert not dest.exists()


async def test_empty_new_database_starts_without_any_destination(tmp_path):
    """A first run must not require a backup destination to create its schema."""
    outcome = await _preflight(tmp_path / "ledger.db", None)
    assert outcome.action == "new-database"


# ---------------------------------------------------------------------------
# Pending migration from a known version
# ---------------------------------------------------------------------------
async def test_pending_migration_creates_a_verified_source_version_backup(
    tmp_path, outside_repo
):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)
    dest = tmp_path / "backups"

    outcome = await _preflight(db, dest)

    assert outcome.action == "backed-up"
    assert outcome.source_version == LATEST_SCHEMA_VERSION - 1
    assert outcome.target_version == LATEST_SCHEMA_VERSION
    backup = outcome.backup_path
    assert backup is not None and backup.parent == dest.resolve()
    assert backup.name.startswith(f"ledger-premigration-v{LATEST_SCHEMA_VERSION - 1}-")

    # The rollback copy carries the *source's* version, never the target's, and
    # verifies independently at that version.
    facts = ledger_backup.verify_backup_file(
        backup, expect_version=LATEST_SCHEMA_VERSION - 1
    )
    assert facts.user_version == LATEST_SCHEMA_VERSION - 1

    # The live database is untouched by the preflight itself; init_db migrates it.
    assert await _version_of(db) == LATEST_SCHEMA_VERSION - 1


async def test_missing_destination_refuses_and_changes_nothing(tmp_path):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)

    with pytest.raises(MigrationPreflightError, match="no backup destination"):
        await _preflight(db, "")

    assert await _version_of(db) == LATEST_SCHEMA_VERSION - 1


async def test_in_repository_destination_refuses_and_changes_nothing(tmp_path):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)

    inside = ledger_backup.PROJECT_ROOT / "backups"
    with pytest.raises(MigrationPreflightError, match="inside the project root"):
        await _preflight(db, inside)

    assert await _version_of(db) == LATEST_SCHEMA_VERSION - 1
    assert not inside.exists()


async def test_failed_backup_refuses_and_changes_nothing(
    tmp_path, outside_repo, monkeypatch
):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)

    def _fail(*_args, **_kwargs):
        raise ledger_backup.VerificationFailed("copy did not verify")

    monkeypatch.setattr(ledger_backup, "create_backup_in", _fail)

    with pytest.raises(MigrationPreflightError, match="could not be created"):
        await _preflight(db, tmp_path / "backups")

    assert await _version_of(db) == LATEST_SCHEMA_VERSION - 1


# ---------------------------------------------------------------------------
# Newer than this checkout
# ---------------------------------------------------------------------------
async def test_future_version_aborts_without_a_backup(tmp_path, outside_repo):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()
    dest = tmp_path / "backups"

    with pytest.raises(migrations.UnsupportedSchemaError):
        await _preflight(db, dest)

    assert not dest.exists()
    assert await _version_of(db) == LATEST_SCHEMA_VERSION + 1


# ---------------------------------------------------------------------------
# Populated legacy version 0
# ---------------------------------------------------------------------------
async def test_populated_legacy_database_is_backed_up_and_rehearsed(
    tmp_path, outside_repo
):
    db = tmp_path / "ledger.db"
    _make_populated_legacy_db(db)
    dest = tmp_path / "backups"

    outcome = await _preflight(db, dest)

    assert outcome.action == "legacy-rehearsed"
    assert outcome.source_version == 0
    backup = outcome.backup_path
    assert backup is not None and backup.name.startswith("ledger-premigration-v0-")

    # The rollback copy stayed at version 0 — the rehearsal ran on a throwaway
    # copy of it, never on the copy itself.
    assert _sqlite_version(backup) == 0
    # Generic verification still refuses to certify version 0.
    with pytest.raises(ledger_backup.VerificationFailed, match="legacy version 0"):
        ledger_backup.verify_backup_file(backup)
    # The live database is still legacy; init_db performs the real migration.
    assert await _version_of(db) == 0

    # No rehearsal temporary files were left behind in the destination.
    assert sorted(p.name for p in dest.iterdir()) == [backup.name]


async def test_failed_rehearsal_refuses_and_changes_nothing(
    tmp_path, outside_repo, monkeypatch
):
    """If the migration cannot succeed on a copy, the live file is not touched."""
    db = tmp_path / "ledger.db"
    _make_populated_legacy_db(db)

    async def _boom(_conn):
        raise migrations.MigrationCollisionError("2 ambiguous rows need a decision")

    monkeypatch.setattr(migrations, "run_migrations", _boom)

    with pytest.raises(MigrationPreflightError, match="rehearsal"):
        await _preflight(db, tmp_path / "backups")

    assert await _version_of(db) == 0
    # The rollback point still exists: the failure is in the rehearsal, not the copy.
    backups = list((tmp_path / "backups").glob("ledger-premigration-v0-*.db"))
    assert len(backups) == 1


async def test_rehearsal_short_of_target_refuses(tmp_path, outside_repo, monkeypatch):
    db = tmp_path / "ledger.db"
    _make_populated_legacy_db(db)

    real_run = migrations.run_migrations

    async def _stop_short(conn):
        original = migrations.LATEST_VERSION
        migrations.LATEST_VERSION = max(1, LATEST_SCHEMA_VERSION - 1)
        try:
            return await real_run(conn)
        finally:
            migrations.LATEST_VERSION = original

    monkeypatch.setattr(migrations, "run_migrations", _stop_short)

    with pytest.raises(MigrationPreflightError, match="reached version"):
        await _preflight(db, tmp_path / "backups")

    assert await _version_of(db) == 0


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------
async def test_connect_closes_a_partial_handle_when_the_live_file_is_corrupt(tmp_path):
    """The first PRAGMA may discover corruption after connect() returned a handle."""
    broken = tmp_path / "ledger.db"
    broken.write_bytes(b"not a SQLite database")
    manager = DatabaseManager(str(broken))

    with pytest.raises(sqlite3.DatabaseError):
        await manager.connect()

    assert manager._conn is None
    # On Windows this also proves no SQLite handle still pins the file.
    broken.unlink()


async def test_post_init_refuses_a_corrupt_live_database_cleanly(
    tmp_path, monkeypatch, caplog
):
    """Corruption is reported before preflight, without a raw driver exception."""
    broken = tmp_path / "ledger.db"
    broken.write_bytes(b"not a SQLite database")
    monkeypatch.setattr(main_module, "DB_PATH", str(broken))
    monkeypatch.setattr(main_module, "BACKUP_DEST_DIR", "")
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))
    application = _build_application()

    with caplog.at_level(logging.ERROR, logger=main_module.__name__):
        with pytest.raises(MigrationPreflightError, match="could not be opened"):
            await main_module.post_init(application)

    assert "restore a verified backup" in caplog.text
    assert "db" not in application.bot_data
    # The failed connection was closed rather than retained until process exit.
    broken.unlink()


async def test_post_init_migrates_only_after_a_verified_backup(
    tmp_path, outside_repo, monkeypatch
):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)
    dest = tmp_path / "backups"
    monkeypatch.setattr(main_module, "DB_PATH", str(db))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(main_module, "BACKUP_DEST_DIR", str(dest))

    application = _build_application()
    await main_module.post_init(application)
    try:
        assert await _version_of_conn(application.bot_data["db"]) == (
            LATEST_SCHEMA_VERSION
        )
    finally:
        await main_module.post_shutdown(application)

    backups = list(dest.glob(f"ledger-premigration-v{LATEST_SCHEMA_VERSION - 1}-*.db"))
    assert len(backups) == 1
    ledger_backup.verify_backup_file(
        backups[0], expect_version=LATEST_SCHEMA_VERSION - 1
    )


async def test_post_init_refuses_to_start_without_a_destination(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)
    monkeypatch.setattr(main_module, "DB_PATH", str(db))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(main_module, "BACKUP_DEST_DIR", "")

    application = _build_application()
    with caplog.at_level(logging.ERROR, logger=main_module.__name__):
        with pytest.raises(MigrationPreflightError):
            await main_module.post_init(application)

    # Refused, with an actionable message and no leaked connection or schema change.
    assert "BACKUP_DEST_DIR" in caplog.text
    assert "db" not in application.bot_data
    assert await _version_of(db) == LATEST_SCHEMA_VERSION - 1


async def test_post_init_starts_a_current_database_without_a_destination(
    tmp_path, monkeypatch
):
    """The setting stays optional for ordinary starts with no pending migration."""
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION)
    monkeypatch.setattr(main_module, "DB_PATH", str(db))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(main_module, "BACKUP_DEST_DIR", "")

    application = _build_application()
    await main_module.post_init(application)
    try:
        assert application.bot_data["db"] is not None
    finally:
        await main_module.post_shutdown(application)


async def test_low_level_runner_still_migrates_without_any_preflight(tmp_path):
    """The property that made entry-point placement necessary.

    A populated database behind the current version is indistinguishable from
    production, which is what every migration test constructs. Driving
    ``run_migrations()`` directly must therefore keep working with no exemption
    flag and no backup destination.
    """
    db = tmp_path / "ledger.db"
    await _make_db_at(db, LATEST_SCHEMA_VERSION - 1)
    manager = DatabaseManager(str(db))
    await manager.connect()
    try:
        await manager.ensure_user(1, "u", "U")
        assert await migrations.run_migrations(manager.conn) == LATEST_SCHEMA_VERSION
    finally:
        await manager.close()


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------
def test_config_rejects_an_in_repository_backup_destination(monkeypatch):
    import importlib

    from bot import config as config_module

    monkeypatch.setenv(
        "BACKUP_DEST_DIR", str(Path(config_module.__file__).resolve().parent.parent)
    )
    with pytest.raises(RuntimeError, match="outside the project root"):
        importlib.reload(config_module)

    # Restore the module other tests import.
    monkeypatch.setenv("BACKUP_DEST_DIR", "")
    importlib.reload(config_module)
    assert config_module.BACKUP_DEST_DIR == ""


def test_conftest_pins_the_destination_empty():
    """A developer's real .env must never leak a live backup directory into tests."""
    import os

    assert os.environ["BACKUP_DEST_DIR"] == ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sqlite_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


async def _version_of(path: Path) -> int:
    return _sqlite_version(path)


async def _version_of_conn(manager: DatabaseManager) -> int:
    return await migrations.get_user_version(manager.conn)


def _build_application():
    from telegram.ext import ApplicationBuilder

    return ApplicationBuilder().token("123456:TEST_TOKEN").build()


def test_required_tables_used_for_the_new_database_exemption():
    """The exemption checks the real table contract, not a hardcoded list."""
    assert "users" in required_tables_for(LATEST_SCHEMA_VERSION)
