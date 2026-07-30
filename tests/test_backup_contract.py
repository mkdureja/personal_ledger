"""Contract tests for the shared schema/backup modules (plan §0.1).

A backup is the only recovery path for a bad committed migration, so the rules
that decide whether a copy is *certified* are worth more tests than the copy
itself. These cover:

* one source of truth: the migration registry, the startup verifier, and the
  backup verifier all agree on what a version-N database must contain;
* version-aware certification: a pre-migration v7 backup is correct at v7, while
  the same file must not be certifiable as current under a v8 checkout;
* fail-closed creation: a copy that does not match its source is renamed
  ``.INVALID`` and never reported as usable; and
* destination policy: no implicit default, and never inside the repository.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import ledger_backup
import ledger_schema
from bot import migrations
from ledger_backup import (
    BackupError,
    DatabaseFacts,
    VerificationFailed,
    create_backup,
    create_backup_in,
    inspect_database,
    prune_backups,
    resolve_destination,
    resolve_expect_version,
    restore_rehearsal_copy,
    verify_backup_file,
    verify_created_backup,
)
from ledger_schema import LATEST_SCHEMA_VERSION, required_tables_for
from scripts import backup_db

# ---------------------------------------------------------------------------
# Helpers — synthesize a database with the *table set* of a given version.
# ---------------------------------------------------------------------------
def _make_versioned_db(
    path: Path,
    version: int,
    *,
    drop: str | None = None,
    orphan: bool = False,
) -> Path:
    """Create a database stamped at ``version`` with its required tables.

    Only table existence matters to the backup contract, so the shapes are
    minimal — but ``users`` and its children carry a real foreign key so an
    orphan row can exercise ``foreign_key_check``.
    """
    tables = set(required_tables_for(version))
    if drop:
        tables.discard(drop)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        if "users" in tables:
            conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO users (user_id) VALUES (1)")
        for table in sorted(tables - {"users"}):
            conn.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, "
                "user_id INTEGER REFERENCES users(user_id))"
            )
        if orphan:
            conn.execute("INSERT INTO study_logs (id, user_id) VALUES (1, 999)")
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
    finally:
        conn.close()
    return path


def _make_legacy_db(path: Path) -> Path:
    """A populated pre-versioning database: real rows, ``user_version`` still 0."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO users (user_id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def outside_repo(tmp_path, monkeypatch):
    """Treat ``tmp_path`` as outside the repo by relocating the project root."""
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    monkeypatch.setattr(ledger_backup, "PROJECT_ROOT", fake_root)
    return fake_root


# ---------------------------------------------------------------------------
# One source of truth
# ---------------------------------------------------------------------------
def test_migration_registry_covers_every_version():
    """Every integer from 1 to the latest version has a registered migration."""
    assert sorted(migrations._MIGRATIONS) == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert migrations.LATEST_VERSION == LATEST_SCHEMA_VERSION


def test_every_required_table_is_introduced_by_some_migration():
    introduced = set(ledger_schema.TABLE_INTRODUCED.values())
    assert introduced <= set(range(1, LATEST_SCHEMA_VERSION + 1))
    assert required_tables_for(LATEST_SCHEMA_VERSION) == frozenset(
        ledger_schema.TABLE_INTRODUCED
    )


def test_required_tables_grow_monotonically():
    previous: frozenset[str] = frozenset()
    for version in range(1, LATEST_SCHEMA_VERSION + 1):
        current = required_tables_for(version)
        assert previous <= current
        previous = current


def test_required_tables_rejects_unknown_versions():
    """An unknown version must be an explicit error, never an empty required set."""
    with pytest.raises(ValueError):
        required_tables_for(0)
    with pytest.raises(ValueError):
        required_tables_for(LATEST_SCHEMA_VERSION + 1)


async def test_startup_and_backup_verification_share_the_table_contract(
    tmp_path, monkeypatch
):
    """Both verifiers read ``required_tables_for``, not private copies.

    Injecting one extra required table into the shared mapping must make the
    startup verifier *and* the backup verifier fail — proof neither keeps its own
    table list that could drift.
    """
    # Build both databases against the *real* contract first, so the injected
    # table is genuinely absent from each of them.
    backup_path = _make_versioned_db(tmp_path / "copy.db", LATEST_SCHEMA_VERSION)
    from bot.database import DatabaseManager

    manager = DatabaseManager(":memory:")
    await manager.connect()
    try:
        await manager.init_db()
        monkeypatch.setitem(
            ledger_schema.TABLE_INTRODUCED, "contract_probe", LATEST_SCHEMA_VERSION
        )
        with pytest.raises(VerificationFailed, match="contract_probe"):
            verify_backup_file(backup_path)
        with pytest.raises(migrations.SchemaVerificationError, match="contract_probe"):
            await migrations.verify_current_schema(manager.conn)
    finally:
        await manager.close()


# ---------------------------------------------------------------------------
# Version-aware certification
# ---------------------------------------------------------------------------
def test_older_complete_database_verifies_at_its_own_version(tmp_path):
    older = _make_versioned_db(tmp_path / "v7.db", 7)
    facts = verify_backup_file(older, expect_version=7)
    assert facts.user_version == 7


def test_pre_migration_creation_accepts_the_exact_source_version(
    tmp_path, outside_repo
):
    source = _make_versioned_db(tmp_path / "ledger.db", 7)
    facts = create_backup(source, tmp_path / "pre.db", expect_version=7)
    assert facts.user_version == 7
    # And the created file is itself independently certifiable at that version.
    assert verify_backup_file(facts.path, expect_version=7).user_version == 7


def test_routine_creation_rejects_a_behind_source(tmp_path, outside_repo):
    """``--expect-version latest`` must not certify a v7 source under a v8 tool."""
    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION - 1)
    dest = tmp_path / "routine.db"
    with pytest.raises(BackupError, match="expect-version asserts"):
        create_backup(source, dest, expect_version="latest")
    # Nothing was written: the mismatch is detected before any copy is made.
    assert not dest.exists()


def test_verify_only_rejects_expect_version_mismatch(tmp_path):
    older = _make_versioned_db(tmp_path / "v7.db", 7)
    with pytest.raises(VerificationFailed, match="does not match the asserted"):
        verify_backup_file(older, expect_version="latest")


def test_verify_only_rejects_legacy_version_zero_distinctly(tmp_path):
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    with pytest.raises(VerificationFailed, match="legacy version 0"):
        verify_backup_file(legacy)


def test_verify_only_rejects_a_negative_version_as_unknown(tmp_path):
    """Generic verification accepts only known versions, even without an assertion."""
    path = tmp_path / "negative.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE bogus (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = -1")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(VerificationFailed, match="not a known schema version"):
        verify_backup_file(path)


def test_verify_only_rejects_a_future_version_distinctly(tmp_path):
    future = tmp_path / "future.db"
    _make_versioned_db(future, LATEST_SCHEMA_VERSION)
    conn = sqlite3.connect(str(future))
    try:
        conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(VerificationFailed, match="newer than this checkout"):
        verify_backup_file(future)


def test_resolve_expect_version_accepts_latest_and_integers():
    assert resolve_expect_version("latest") == LATEST_SCHEMA_VERSION
    assert resolve_expect_version("LATEST") == LATEST_SCHEMA_VERSION
    assert resolve_expect_version(7) == 7
    assert resolve_expect_version("7") == 7
    assert resolve_expect_version(None) is None
    with pytest.raises(BackupError):
        resolve_expect_version("v8")
    with pytest.raises(BackupError):
        resolve_expect_version(-1)
    with pytest.raises(BackupError, match="startup migration preflight"):
        resolve_expect_version(0)


def test_an_undescribable_version_cannot_be_asserted_or_certified(
    tmp_path, outside_repo
):
    """A version with no table contract must not be certifiable at all.

    Otherwise asserting a future version yields a copy checked only for integrity
    and foreign keys — verification that proves nothing about its shape.
    """
    future = LATEST_SCHEMA_VERSION + 1
    with pytest.raises(BackupError, match="no known table contract"):
        resolve_expect_version(future)

    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION)
    conn = sqlite3.connect(str(source))
    try:
        conn.execute(f"PRAGMA user_version = {future}")
        conn.commit()
    finally:
        conn.close()
    dest = tmp_path / "future-copy.db"
    with pytest.raises(BackupError, match="no known table contract"):
        create_backup(source, dest, expect_version=future)
    assert not dest.exists()

    # Defence in depth: even reached directly, the create-path contract refuses.
    facts = inspect_database(source)
    with pytest.raises(VerificationFailed, match="no known table contract"):
        verify_created_backup(facts, source_version=future)


@pytest.mark.parametrize(
    "content",
    # A zero-byte file is deliberately absent: SQLite treats it as a valid *empty*
    # database, so it is rejected by the version rule (stamped 0), not by the
    # driver. These are the shapes that make sqlite3 itself raise.
    [b"not a database at all" * 40, b"SQLite format 3\x00truncated"],
)
def test_a_damaged_file_fails_with_a_controlled_error(tmp_path, content):
    """A driver traceback is not a verification result.

    The CLI must exit non-zero with an actionable message, and the migration
    preflight — which wraps ``BackupError`` — must be able to catch this.
    """
    broken = tmp_path / "broken.db"
    broken.write_bytes(content)
    with pytest.raises(VerificationFailed) as excinfo:
        verify_backup_file(broken)
    assert "rollback point" in str(excinfo.value)


def test_cli_reports_a_damaged_file_without_a_traceback(tmp_path, capsys):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"definitely not sqlite")
    assert backup_db.main(["--verify-only", str(broken)]) == 1
    assert "ERROR" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fail-closed verification
# ---------------------------------------------------------------------------
def test_missing_required_table_fails_verification(tmp_path):
    incomplete = _make_versioned_db(
        tmp_path / "incomplete.db", LATEST_SCHEMA_VERSION, drop="user_food_preferences"
    )
    with pytest.raises(VerificationFailed, match="user_food_preferences"):
        verify_backup_file(incomplete)


def test_foreign_key_violation_fails_verification(tmp_path):
    broken = _make_versioned_db(
        tmp_path / "broken.db", LATEST_SCHEMA_VERSION, orphan=True
    )
    with pytest.raises(VerificationFailed, match="foreign_key_check"):
        verify_backup_file(broken)


def test_create_marks_a_failed_copy_invalid(tmp_path, outside_repo):
    source = _make_versioned_db(
        tmp_path / "ledger.db", LATEST_SCHEMA_VERSION, orphan=True
    )
    dest = tmp_path / "backup.db"
    with pytest.raises(VerificationFailed):
        create_backup(source, dest, expect_version="latest")
    assert not dest.exists()
    assert dest.with_name("backup.db.INVALID").exists()


def test_copy_version_differing_from_source_fails_the_create_contract(tmp_path):
    """The create path compares copy against source, not against the tool."""
    copy = _make_versioned_db(tmp_path / "copy.db", LATEST_SCHEMA_VERSION)
    facts = inspect_database(copy)
    with pytest.raises(VerificationFailed, match="but its source is version"):
        verify_created_backup(facts, source_version=LATEST_SCHEMA_VERSION - 1)
    assert verify_created_backup(facts, source_version=LATEST_SCHEMA_VERSION) is facts


def test_integrity_failure_is_reported(tmp_path):
    """An ``integrity_check`` other than ok fails, independent of table shape."""
    facts = DatabaseFacts(
        path=tmp_path / "x.db",
        user_version=LATEST_SCHEMA_VERSION,
        integrity="*** in database main ***",
        fk_violations=0,
        tables=frozenset(required_tables_for(LATEST_SCHEMA_VERSION)),
        user_counts={},
        shared_counts={},
    )
    assert not facts.sound
    with pytest.raises(VerificationFailed, match="integrity_check"):
        verify_created_backup(facts, source_version=LATEST_SCHEMA_VERSION)


def test_public_backup_apis_cannot_certify_a_legacy_source(tmp_path, outside_repo):
    """The sole v0 exception belongs to preflight, never ordinary creation."""
    source = _make_legacy_db(tmp_path / "ledger.db")
    explicit = tmp_path / "legacy-copy.db"
    auto_dir = tmp_path / "out"

    with pytest.raises(BackupError, match="startup migration preflight"):
        create_backup(source, explicit, expect_version=0)
    with pytest.raises(BackupError, match="startup migration preflight"):
        create_backup_in(source, auto_dir, expect_version=0)
    assert not explicit.exists()
    assert not auto_dir.exists()

    # Defence in depth if a caller bypasses the ordinary create entry point.
    with pytest.raises(VerificationFailed, match="not publicly certifiable"):
        verify_created_backup(inspect_database(source), source_version=0)


# ---------------------------------------------------------------------------
# Destination policy and retention
# ---------------------------------------------------------------------------
def test_destination_inside_the_project_root_is_rejected():
    root = ledger_backup.PROJECT_ROOT
    with pytest.raises(BackupError, match="inside the project root"):
        resolve_destination(root / "ledger-backup.db")
    with pytest.raises(BackupError, match="inside the project root"):
        resolve_destination(root)


def test_existing_destination_is_never_overwritten(tmp_path, outside_repo):
    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION)
    dest = tmp_path / "taken.db"
    dest.write_bytes(b"not a database")
    with pytest.raises(BackupError, match="already exists"):
        create_backup(source, dest, expect_version="latest")
    assert dest.read_bytes() == b"not a database"


def test_auto_named_backup_carries_its_version(tmp_path, outside_repo):
    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION)
    facts = create_backup_in(source, tmp_path / "out", expect_version="latest")
    assert facts.path.parent == (tmp_path / "out").resolve()
    assert facts.path.name.startswith(f"ledger-v{LATEST_SCHEMA_VERSION}-")


def test_retention_keeps_the_newest_and_spares_pre_migration(tmp_path):
    directory = tmp_path / "out"
    directory.mkdir()
    routine = []
    for index in range(4):
        path = directory / f"ledger-v8-2026073{index}-000000Z.db"
        path.write_bytes(b"x")
        routine.append(path)
    precious = directory / "ledger-premigration-v7-20260730-000000Z.db"
    precious.write_bytes(b"x")

    removed = prune_backups(directory, prefix="ledger-v8-", keep=2)

    assert set(removed) == set(routine[:2])
    assert routine[2].exists() and routine[3].exists()
    assert precious.exists()
    assert prune_backups(directory, prefix="ledger-v8-", keep=0) == []


def test_restore_rehearsal_copy_verifies_independently(tmp_path, outside_repo):
    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION)
    facts = create_backup_in(source, tmp_path / "out", expect_version="latest")
    restored = restore_rehearsal_copy(facts.path)
    try:
        assert verify_backup_file(restored, expect_version="latest").user_version == (
            LATEST_SCHEMA_VERSION
        )
    finally:
        restored.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------
def test_cli_requires_explicit_destination(capsys):
    with pytest.raises(SystemExit) as excinfo:
        backup_db.main(["--source", "ledger.db", "--expect-version", "latest"])
    assert excinfo.value.code == 2
    assert "--dest is required" in capsys.readouterr().err


def test_cli_create_requires_expect_version(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        backup_db.main(["--source", "ledger.db", "--dest", str(tmp_path / "out")])
    assert excinfo.value.code == 2
    assert "--expect-version is required" in capsys.readouterr().err


def test_cli_create_rejects_legacy_zero_without_rehearsal(
    tmp_path, outside_repo, capsys
):
    source = _make_legacy_db(tmp_path / "legacy.db")
    out = tmp_path / "legacy-copy.db"

    assert backup_db.main(
        [
            "--source",
            str(source),
            "--dest",
            str(out),
            "--expect-version",
            "0",
        ]
    ) == 2
    assert "startup migration preflight" in capsys.readouterr().err
    assert not out.exists()


def test_cli_creates_and_then_verifies(tmp_path, outside_repo, capsys):
    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION)
    out = tmp_path / "out"

    assert backup_db.main(
        ["--source", str(source), "--dest", str(out), "--expect-version", "latest"]
    ) == 0
    created = capsys.readouterr().out
    assert "backup written and verified" in created
    backups = list(out.glob("*.db"))
    assert len(backups) == 1

    assert backup_db.main(["--verify-only", str(backups[0])]) == 0
    assert "verified rollback point" in capsys.readouterr().out


def test_cli_reports_failures_without_certifying(tmp_path, outside_repo, capsys):
    behind = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION - 1)
    rc = backup_db.main(
        [
            "--source",
            str(behind),
            "--dest",
            str(tmp_path / "out"),
            "--expect-version",
            "latest",
        ]
    )
    assert rc == 2
    assert "ERROR" in capsys.readouterr().err

    legacy = _make_legacy_db(tmp_path / "legacy.db")
    assert backup_db.main(["--verify-only", str(legacy)]) == 1
    assert "legacy version 0" in capsys.readouterr().err


def test_cli_output_is_sanitized(tmp_path, outside_repo, capsys):
    """Diagnostics carry schema names and counts only."""
    source = _make_versioned_db(tmp_path / "ledger.db", LATEST_SCHEMA_VERSION)
    conn = sqlite3.connect(str(source))
    try:
        conn.execute("INSERT INTO users (user_id) VALUES (987654321)")
        conn.commit()
    finally:
        conn.close()

    backup_db.main(
        [
            "--source",
            str(source),
            "--dest",
            str(tmp_path / "out"),
            "--expect-version",
            "latest",
        ]
    )
    out = capsys.readouterr().out
    assert "987654321" not in out
    assert "users" in out and "row counts (sanitized" in out
