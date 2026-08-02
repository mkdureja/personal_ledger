r"""Consistent, verified backups of the Ledger SQLite database.

Dependency-free by design: standard library only, no ``bot`` import, no
``aiosqlite``, no Telegram code, no application configuration. Two very different
callers therefore share exactly one implementation —

* ``scripts/backup_db.py``, a standalone CLI an operator runs by hand; and
* ``bot/migration_preflight.py``, which calls these functions **directly** (not
  through a subprocess) to refuse a production migration that has no verified
  rollback point.

Backups use SQLite's online backup API, which captures a transactionally
consistent snapshot *while the bot is running* and folds pending WAL state into a
single self-contained ``.db`` file. Copying ``ledger.db`` on its own is not a
backup: it misses the ``-wal``/``-shm`` sidecars and can restore a torn, older
state.

One documented side effect: the source is opened read-write, so closing it
**checkpoints the WAL** into the main database file and removes the
``-wal``/``-shm`` sidecars. No row changes — the WAL held already-committed data —
and it is the same thing a clean shutdown does. Read-only access is deliberately
not used: a read-only connection cannot recover a leftover WAL if its ``-shm`` is
gone, which would make the tool fail exactly when a post-crash backup matters
most.

Two verification contracts, deliberately distinct:

**Creation** (:func:`create_backup`) proves the copy matches the source it was
taken from — same ``user_version``, the tables required at that version, clean
integrity and foreign keys. The caller must also state its intent through
``expect_version``, so a v8-era tool cannot quietly certify a v7 source as a
current backup, while a pre-migration backup taken from that same v7 source
passes with ``--expect-version 7``.

**Restore verification** (:func:`verify_backup_file`) proves a file already on
disk is a certifiable rollback point at whatever version it is stamped with. It
accepts any known version 1..``LATEST_SCHEMA_VERSION`` and rejects the legacy
unversioned 0 and any future version with distinct, actionable messages.

Diagnostics are sanitized: schema names, version numbers, and aggregate row
counts only. Never a Telegram ID, username, first name, entry text, or token.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ledger_schema import (
    LATEST_SCHEMA_VERSION,
    LEGACY_UNVERSIONED,
    TABLE_INTRODUCED,
    is_known_schema_version,
    required_columns_for,
    required_tables_for,
    shared_tables_for,
    user_tables_for,
)

PROJECT_ROOT = Path(__file__).resolve().parent

#: ``--expect-version latest`` resolves to this checkout's version.
LATEST_TOKEN = "latest"

#: Default rolling retention for routine backups sharing one name prefix.
DEFAULT_KEEP = 10

_ROUTINE_PREFIX = "ledger-v"
_PREMIGRATION_PREFIX = "ledger-premigration-v"


class BackupError(RuntimeError):
    """A backup could not be created, or was created and then rejected."""


class VerificationFailed(BackupError):
    """A database file did not satisfy its verification contract."""


@dataclass(frozen=True)
class DatabaseFacts:
    """Sanitized, verifiable facts about one SQLite database file."""

    path: Path
    user_version: int
    integrity: str
    fk_violations: int
    tables: frozenset[str]
    user_counts: Mapping[str, int]
    shared_counts: Mapping[str, int]
    #: table name -> its column names, for every table present. Only tables in
    #: the shared manifest are ever checked against it; the rest are carried so a
    #: diagnostic never has to reopen the file.
    columns: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @property
    def sound(self) -> bool:
        """Whether the file passes the checks that apply at any version."""
        return self.integrity == "ok" and self.fk_violations == 0


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
def inspect_database(path: Path | str) -> DatabaseFacts:
    """Read sanitized schema facts from an existing database file."""
    path = Path(path)
    if not path.exists():
        raise BackupError(f"database not found: {path}")
    # A truncated, encrypted, or simply wrong file raises from deep inside sqlite3.
    # Letting that escape means an operator verifying a rollback point gets a raw
    # traceback, and the migration preflight — which only wraps BackupError — would
    # abort startup with a driver error instead of its own actionable refusal.
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        raise VerificationFailed(
            f"{path}: could not be opened as a SQLite database ({exc}). Do NOT use "
            "this file as a rollback point."
        ) from exc
    try:
        return _facts_from_connection(conn, path)
    except sqlite3.Error as exc:
        raise VerificationFailed(
            f"{path}: could not be read as a SQLite database ({exc}) — it may be "
            "truncated, encrypted, or not a database. Do NOT use this file as a "
            "rollback point."
        ) from exc
    finally:
        conn.close()


def _facts_from_connection(conn: sqlite3.Connection, path: Path) -> DatabaseFacts:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    tables = frozenset(
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    )
    if is_known_schema_version(version):
        user_names = user_tables_for(version)
        shared_names = shared_tables_for(version)
    else:
        # An unknown version has no defined table list. Report what is present of
        # the tables this checkout knows about, purely as a diagnostic — it never
        # substitutes for the schema verification the caller still has to fail on.
        known_shared = shared_tables_for(LATEST_SCHEMA_VERSION)
        user_names = tuple(
            sorted(t for t in TABLE_INTRODUCED if t not in known_shared)
        )
        shared_names = known_shared
    return DatabaseFacts(
        path=path,
        user_version=version,
        integrity=integrity,
        fk_violations=fk_violations,
        tables=tables,
        user_counts=_count_rows(conn, user_names, tables),
        shared_counts=_count_rows(conn, shared_names, tables),
        columns=_read_columns(conn, tables),
    )


def _read_columns(
    conn: sqlite3.Connection, present: frozenset[str]
) -> dict[str, frozenset[str]]:
    """Column names for each present table, read once per inspection."""
    columns: dict[str, frozenset[str]] = {}
    for table in sorted(present):
        # Table names come from sqlite_master, never from user input; PRAGMA
        # does not accept a bound parameter here.
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
        columns[table] = frozenset(str(row[1]) for row in rows)
    return columns


def _count_rows(
    conn: sqlite3.Connection, names: tuple[str, ...], present: frozenset[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in names:
        if table not in present:
            continue
        # Table names come from the vetted ledger_schema mapping, never user input.
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    return counts


def format_facts(facts: DatabaseFacts) -> list[str]:
    """Render sanitized diagnostics for an inspected database."""
    lines = [
        f"file                : {facts.path}",
        f"schema user_version : {facts.user_version}",
        f"integrity_check     : {facts.integrity}",
        "foreign_key_check   : "
        + ("OK" if not facts.fk_violations else f"{facts.fk_violations} problem(s)"),
        f"tables present      : {len(facts.tables)}",
        "row counts (sanitized, user-owned tables):",
    ]
    lines.extend(f"  {table:<24} {count}" for table, count in facts.user_counts.items())
    if facts.shared_counts:
        lines.append("shared reference totals (diagnostic only):")
        lines.extend(
            f"  {table:<24} {count}" for table, count in facts.shared_counts.items()
        )
    return lines


# ---------------------------------------------------------------------------
# Verification contracts
# ---------------------------------------------------------------------------
def resolve_expect_version(raw: str | int | None) -> int | None:
    """Resolve an ``--expect-version`` argument to an integer version.

    Accepts ``"latest"`` (this checkout's version) or an explicit integer.
    ``None`` passes through, meaning "no assertion" — legal only for generic
    restore verification, never for creation.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text == LATEST_TOKEN:
            return LATEST_SCHEMA_VERSION
        try:
            raw = int(text)
        except ValueError as exc:
            raise BackupError(
                f"--expect-version must be an integer or {LATEST_TOKEN!r}; "
                f"got {raw!r}."
            ) from exc
    value = int(raw)
    if value < LEGACY_UNVERSIONED:
        raise BackupError(f"--expect-version cannot be negative; got {value}.")
    if value == LEGACY_UNVERSIONED:
        raise BackupError(
            "--expect-version 0 is a legacy, unversioned database with no schema "
            "contract. It can only be backed up by the startup migration preflight, "
            "which necessarily rehearses the migration on a throwaway copy."
        )
    if value > LATEST_SCHEMA_VERSION:
        # Without this, a caller could assert a version this checkout cannot
        # describe and get a "verified" copy that was never checked against any
        # table contract — integrity and foreign keys only. Certifying a schema
        # whose required objects are unknown is exactly what verification is for.
        raise BackupError(
            f"--expect-version {value} is newer than this checkout understands "
            f"({LATEST_SCHEMA_VERSION}), so it has no known table contract and a "
            "copy at that version cannot be certified. Use the matching (or "
            "newer) build."
        )
    return value


def _shape_problems(facts: DatabaseFacts) -> list[str]:
    """Version-independent soundness problems, plus required table/column gaps.

    Columns are checked, not just tables. A table list alone cannot see a
    column-only migration — a v10 database stripped of both AI-consent columns
    holds every required table and would otherwise certify as a sound rollback
    point. The manifest is the same one the bot enforces at startup, so a copy
    can never pass here and fail there.
    """
    problems: list[str] = []
    if facts.integrity != "ok":
        problems.append(f"integrity_check returned {facts.integrity!r}, not 'ok'")
    if facts.fk_violations:
        problems.append(f"foreign_key_check found {facts.fk_violations} violation(s)")
    if is_known_schema_version(facts.user_version):
        missing = sorted(required_tables_for(facts.user_version) - facts.tables)
        if missing:
            problems.append(
                f"missing {len(missing)} table(s) required at version "
                f"{facts.user_version}: {', '.join(missing)}"
            )
        for table, absent in sorted(_column_gaps(facts).items()):
            problems.append(
                f"table {table!r} is missing column(s) required at version "
                f"{facts.user_version}: {', '.join(absent)}"
            )
    return problems


def _column_gaps(facts: DatabaseFacts) -> dict[str, list[str]]:
    """Required columns absent from each present table, by table name."""
    gaps: dict[str, list[str]] = {}
    for table, required in required_columns_for(facts.user_version).items():
        if table not in facts.tables:
            # Already reported as a missing table; naming every column too would
            # bury the one fact the operator needs.
            continue
        absent = sorted(required - facts.columns.get(table, frozenset()))
        if absent:
            gaps[table] = absent
    return gaps


def verify_backup_file(
    path: Path | str, *, expect_version: str | int | None = None
) -> DatabaseFacts:
    """Verify a file on disk is a certifiable rollback point.

    Accepts any known stamped version, so a pre-migration v7 backup stays
    verifiable under a v8 checkout. Raises :class:`VerificationFailed` with a
    sanitized, actionable message otherwise.
    """
    expected = resolve_expect_version(expect_version)
    facts = inspect_database(path)
    version = facts.user_version

    if version == LEGACY_UNVERSIONED:
        raise VerificationFailed(
            f"{facts.path}: stamped legacy version 0, which is not a certifiable "
            "schema version — pre-versioning databases never recorded their shape. "
            "A populated legacy database is migrated through the startup preflight, "
            "which backs it up and rehearses the migration on a copy first."
        )
    if version > LATEST_SCHEMA_VERSION:
        raise VerificationFailed(
            f"{facts.path}: stamped version {version}, which is newer than this "
            f"checkout understands ({LATEST_SCHEMA_VERSION}). Verify it with the "
            "matching (or newer) build; this one does not know that schema's "
            "required objects."
        )
    if not is_known_schema_version(version):
        # Version 0 and future versions have their own actionable messages above.
        # This catches every other impossible/undescribable stamp, notably negative
        # PRAGMA user_version values, rather than silently verifying against no
        # table contract.
        raise VerificationFailed(
            f"{facts.path}: stamped version {version}, which is not a known schema "
            f"version in this checkout (1 through {LATEST_SCHEMA_VERSION}). Do NOT "
            "use this file as a rollback point."
        )

    problems = _shape_problems(facts)
    if expected is not None and version != expected:
        problems.append(
            f"stamped version {version} does not match the asserted "
            f"--expect-version {expected}"
        )
    if problems:
        raise VerificationFailed(_problem_message(facts.path, problems))
    return facts


def verify_created_backup(
    facts: DatabaseFacts, *, source_version: int
) -> DatabaseFacts:
    """Verify a freshly created copy against the source it was taken from.

    The copy must carry the *source's* version, not this checkout's: a
    pre-migration backup of a v7 database is correct precisely because it
    contains v7. Public certification requires a known schema contract; legacy
    version 0 is handled only by the private migration-rehearsal path.
    """
    return _verify_created_backup(
        facts, source_version=source_version, allow_legacy_unversioned=False
    )


def _verify_created_backup(
    facts: DatabaseFacts,
    *,
    source_version: int,
    allow_legacy_unversioned: bool,
) -> DatabaseFacts:
    """Shared create-path verifier, with one private v0 rehearsal exception."""
    problems: list[str] = []
    if facts.user_version != source_version:
        problems.append(
            f"copy is stamped version {facts.user_version} but its source is "
            f"version {source_version}"
        )
    if facts.user_version == LEGACY_UNVERSIONED:
        if not allow_legacy_unversioned:
            problems.append(
                "legacy version 0 has no schema contract and is not publicly "
                "certifiable; it must go through migration preflight rehearsal"
            )
    elif not is_known_schema_version(facts.user_version):
        # Defence in depth behind resolve_expect_version: never certify a version
        # with no table contract.
        problems.append(
            f"version {facts.user_version} has no known table contract in this "
            f"checkout (1 through {LATEST_SCHEMA_VERSION})"
        )
    problems.extend(_shape_problems(facts))
    if problems:
        raise VerificationFailed(_problem_message(facts.path, problems))
    return facts


def _problem_message(path: Path, problems: list[str]) -> str:
    joined = "; ".join(problems)
    return (
        f"{path}: verification FAILED — {joined}. Do NOT use this file as a "
        "rollback point."
    )


# ---------------------------------------------------------------------------
# Destination policy
# ---------------------------------------------------------------------------
def resolve_destination(dest: Path | str, *, project_root: Path | None = None) -> Path:
    """Resolve a backup destination and reject one inside the repository.

    A backup beside ``ledger.db`` shares the failure it exists to survive — the
    same disk, the same directory, the same accidental ``git clean``. The old
    implicit default landed exactly there, so the containment rule is enforced in
    one place for every caller.
    """
    root = (project_root or PROJECT_ROOT).resolve()
    resolved = Path(dest).expanduser().resolve()
    if resolved == root or root in resolved.parents:
        raise BackupError(
            f"refusing a backup destination inside the project root: {resolved}. "
            "Choose a directory outside the repository (see docs/backup_runbook.md)."
        )
    return resolved


def backup_filename(version: int, *, pre_migration: bool = False) -> str:
    """A timestamped, version-bearing backup filename.

    The version is in the name so an operator can see at a glance which build a
    rollback point belongs to, and the pre-migration prefix keeps those precious
    copies out of routine rolling retention.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = _PREMIGRATION_PREFIX if pre_migration else _ROUTINE_PREFIX
    return f"{prefix}{version}-{stamp}Z.db"


def prune_backups(directory: Path, *, prefix: str, keep: int) -> list[Path]:
    """Delete the oldest backups sharing ``prefix`` beyond the newest ``keep``.

    Returns the removed paths. ``keep <= 0`` disables pruning entirely, which is
    what the migration preflight uses: a pre-migration rollback point is never
    auto-deleted.
    """
    if keep <= 0:
        return []
    candidates = sorted(
        (p for p in directory.glob(f"{prefix}*.db") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    removed: list[Path] = []
    for path in candidates[: max(0, len(candidates) - keep)]:
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------
def clone_database(source: Path | str, dest: Path | str) -> Path:
    """Online-copy ``source`` to ``dest`` without applying any contract.

    Used for a migration *rehearsal* target, where the point is to mutate the
    copy. Verified rollback points go through :func:`create_backup` instead.
    """
    source, dest = Path(source), Path(dest)
    if not source.exists():
        raise BackupError(f"source database not found: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    try:
        out = sqlite3.connect(str(dest))
        try:
            with out:
                src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def create_backup(
    source: Path | str,
    dest: Path | str,
    *,
    expect_version: str | int,
    project_root: Path | None = None,
) -> DatabaseFacts:
    """Create and verify a backup of ``source`` at ``dest``.

    ``expect_version`` is mandatory: creation always states whether it believes
    it is backing up a current database (``"latest"``) or a specific older
    version it just read (pre-migration). A mismatch aborts *before* any file is
    written, so a stale source can never produce a file labelled current.

    On verification failure the copy is renamed ``*.INVALID`` and
    :class:`VerificationFailed` is raised — a corrupt copy must never be
    reported as a rollback point.
    """
    source = Path(source)
    expected = resolve_expect_version(expect_version)
    if expected is None:  # pragma: no cover - signature requires a value
        raise BackupError("creating a backup requires an explicit expect_version.")
    return _create_backup_at_version(
        source,
        dest,
        expected=expected,
        project_root=project_root,
        allow_legacy_unversioned=False,
    )


def _create_backup_at_version(
    source: Path | str,
    dest: Path | str,
    *,
    expected: int,
    project_root: Path | None,
    allow_legacy_unversioned: bool,
) -> DatabaseFacts:
    """Create one copy after its caller has selected a certification contract."""
    source_facts = inspect_database(source)
    if source_facts.user_version != expected:
        raise BackupError(
            f"{source}: source is at schema version {source_facts.user_version}, "
            f"but --expect-version asserts {expected}. Refusing to create a backup "
            "labelled with a version its source does not have."
        )

    resolved_dest = resolve_destination(dest, project_root=project_root)
    if resolved_dest.exists():
        raise BackupError(
            f"destination already exists, refusing to overwrite: {resolved_dest}"
        )

    clone_database(source, resolved_dest)
    facts = inspect_database(resolved_dest)
    try:
        _verify_created_backup(
            facts,
            source_version=source_facts.user_version,
            allow_legacy_unversioned=allow_legacy_unversioned,
        )
    except VerificationFailed:
        invalid = resolved_dest.with_name(resolved_dest.name + ".INVALID")
        try:
            resolved_dest.replace(invalid)
        except OSError:
            pass
        raise
    return facts


def _create_legacy_backup_in_for_migration_rehearsal(
    source: Path | str,
    directory: Path | str,
    *,
    project_root: Path | None = None,
) -> DatabaseFacts:
    """Create the v0 rollback input used only by startup's rehearsal path.

    This intentionally private capability does not certify a v0 schema—there is
    no such contract. It proves only that the online copy matches a v0 source and
    is internally sound. ``bot.migration_preflight`` immediately migrates a
    throwaway copy of this file and refuses to touch the live database unless that
    rehearsal reaches and verifies the current schema.
    """
    directory = resolve_destination(directory, project_root=project_root)
    directory.mkdir(parents=True, exist_ok=True)
    return _create_backup_at_version(
        source,
        directory / backup_filename(LEGACY_UNVERSIONED, pre_migration=True),
        expected=LEGACY_UNVERSIONED,
        project_root=project_root,
        allow_legacy_unversioned=True,
    )


def create_backup_in(
    source: Path | str,
    directory: Path | str,
    *,
    expect_version: str | int,
    pre_migration: bool = False,
    keep: int = 0,
    project_root: Path | None = None,
) -> DatabaseFacts:
    """Create a verified, auto-named backup inside ``directory``.

    ``keep`` prunes only the *same family* of names — routine backups never
    delete a pre-migration rollback point, and the preflight passes ``keep=0`` so
    its own copies are never auto-deleted at all.
    """
    version = resolve_expect_version(expect_version)
    assert version is not None  # resolve_expect_version only returns None for None
    directory = resolve_destination(directory, project_root=project_root)
    directory.mkdir(parents=True, exist_ok=True)
    name = backup_filename(version, pre_migration=pre_migration)
    facts = create_backup(
        source,
        directory / name,
        expect_version=version,
        project_root=project_root,
    )
    if keep > 0:
        prefix = _PREMIGRATION_PREFIX if pre_migration else _ROUTINE_PREFIX
        prune_backups(directory, prefix=f"{prefix}{version}-", keep=keep)
    return facts


def restore_rehearsal_copy(backup: Path | str) -> Path:
    """Copy a verified backup into a fresh temporary file and return its path.

    The caller owns the returned file and is responsible for deleting it. Used
    by the restore rehearsal and by the legacy-version-0 migration rehearsal, so
    neither ever touches the live database or the rollback point itself.
    """
    backup = Path(backup)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed immediately below
        prefix="ledger-rehearsal-", suffix=".db", delete=False
    )
    handle.close()
    target = Path(handle.name)
    target.unlink(missing_ok=True)
    shutil.copy2(backup, target)
    return target
