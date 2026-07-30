r"""Create or verify a consistent, schema-verified Ledger database backup.

A thin CLI over the dependency-free :mod:`ledger_backup` module; every rule lives
there so this command and the production migration preflight cannot drift apart.

Run it from the repository root with the project virtual environment:

    .\.venv\Scripts\python.exe -m scripts.backup_db \
        --source ledger.db \
        --dest   E:\ledger-backups \
        --expect-version latest

    .\.venv\Scripts\python.exe -m scripts.backup_db \
        --verify-only E:\ledger-backups\ledger-v8-20260730-101500Z.db

``--dest`` is mandatory and must resolve **outside** the repository: a backup
beside ``ledger.db`` shares the disk, directory, and accidental deletion it
exists to survive. When ``--dest`` is a directory (or ends with a separator) the
file is named automatically as ``ledger-v<version>-<UTC stamp>Z.db``.

``--expect-version`` is mandatory when creating a backup:

* ``latest`` — a routine backup of a current database. Fails if the source is
  behind, so a stale copy can never be certified as current.
* ``<N>`` — a pre-migration backup of a database you have just read as version
  N. A v7 backup taken by this v8 build is *correct* at v7.

``--verify-only`` verifies a file already on disk at whatever known version it
carries, which is how a restore rehearsal is checked. Add ``--expect-version``
to assert a particular restore target.

Output is sanitized: schema names, versions, and aggregate row counts only. Never
a Telegram ID, username, first name, entry text, or the bot token.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ledger_backup import (
    DEFAULT_KEEP,
    BackupError,
    VerificationFailed,
    create_backup,
    create_backup_in,
    format_facts,
    verify_backup_file,
)
from ledger_schema import LATEST_SCHEMA_VERSION


def _looks_like_directory(raw: str, path: Path) -> bool:
    return path.is_dir() or raw.endswith(("/", "\\")) or path.suffix == ""


def _print_facts(facts) -> None:
    for line in format_facts(facts):
        print(line)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.backup_db",
        description="Create or verify a schema-verified Ledger backup.",
    )
    parser.add_argument(
        "--source", default="ledger.db", help="Live database path (create mode)"
    )
    parser.add_argument(
        "--dest",
        help="Backup destination file or directory, outside the repository (create mode)",
    )
    parser.add_argument(
        "--verify-only",
        metavar="PATH",
        help="Verify an existing backup file instead of creating one",
    )
    parser.add_argument(
        "--expect-version",
        help=(
            f"'latest' (currently {LATEST_SCHEMA_VERSION}) or an explicit integer "
            "version. Required when creating a backup."
        ),
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=(
            "Rolling retention for auto-named routine backups in --dest "
            f"(default {DEFAULT_KEEP}; 0 disables pruning). Pre-migration "
            "rollback points are never pruned."
        ),
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        if args.dest:
            parser.error("--verify-only verifies an existing file; --dest is unused")
        try:
            facts = verify_backup_file(
                args.verify_only, expect_version=args.expect_version
            )
        except VerificationFailed as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        _print_facts(facts)
        print(f"OK: verified rollback point at schema version {facts.user_version}.")
        return 0

    if not args.dest:
        parser.error(
            "--dest is required (a directory or file outside the repository). "
            "There is deliberately no default: an implicit destination landed "
            "beside ledger.db."
        )
    if not args.expect_version:
        parser.error(
            "--expect-version is required when creating a backup: pass 'latest' "
            "for a routine backup, or the exact integer version you just read "
            "for a pre-migration backup."
        )

    dest = Path(args.dest).expanduser()
    try:
        if _looks_like_directory(args.dest, dest):
            facts = create_backup_in(
                args.source,
                dest,
                expect_version=args.expect_version,
                keep=args.keep,
            )
        else:
            facts = create_backup(
                args.source, dest, expect_version=args.expect_version
            )
    except VerificationFailed as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print_facts(facts)
    print(f"OK: backup written and verified: {facts.path}")
    print(
        "Record this path in docs/backup_runbook.md, then rehearse a restore with "
        f"--verify-only {facts.path}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point; kept as a stable name for the runbook and tests."""
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
