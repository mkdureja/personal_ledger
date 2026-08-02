r"""Delete every diet log (and its item children) for a fresh start.

Written for the changeover to mandatory nutrition: the existing meals were a
mix of test entries and rows saved before calories and macros were required, so
they could never be totalled. Rather than backfill numbers nobody can remember,
they are cleared and real logging starts from a clean ledger.

It is a *script*, not a bot command, on purpose. Wiping history should take a
deliberate act at a terminal, not a mistappable button next to "Log meal".

    .\.venv\Scripts\python.exe -m scripts.reset_diet_logs --dry-run
    .\.venv\Scripts\python.exe -m scripts.reset_diet_logs --dest E:\ledger-backups

What it touches: ``diet_logs``, ``diet_log_items``, and the ``diet`` rows of
``mutation_receipts`` (a stale receipt would otherwise make a replayed Telegram
update resolve to a meal id that no longer exists). Habits, study, gym,
supplements, saved foods, recipes, and the shared catalog are never touched.

A verified backup is taken first unless ``--no-backup`` is given, and the
deletion runs in one transaction: it either all happens or none of it does.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ledger_backup import BackupError, VerificationFailed, create_backup_in
from ledger_schema import LATEST_SCHEMA_VERSION

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Every meal write records ``entity_type = 'diet'``. The *operation_key* varies
#: by entry point (``diet_log``, ``diet_quick``, ``diet_repeat``), so filtering
#: on that would silently leave most receipts behind — pointing at meal ids that
#: no longer exist, which is precisely the replay hazard this clears.
_DIET_RECEIPTS = "FROM mutation_receipts WHERE entity_type = 'diet'"

#: Deleted in this order so children never outlive their parent.
TARGETS = (
    ("diet_log_items", "DELETE FROM diet_log_items"),
    ("diet_logs", "DELETE FROM diet_logs"),
    ("mutation_receipts (diet)", f"DELETE {_DIET_RECEIPTS}"),
)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "diet_logs": conn.execute("SELECT COUNT(*) FROM diet_logs").fetchone()[0],
        "diet_log_items": conn.execute(
            "SELECT COUNT(*) FROM diet_log_items"
        ).fetchone()[0],
        "mutation_receipts (diet)": conn.execute(
            f"SELECT COUNT(*) {_DIET_RECEIPTS}"
        ).fetchone()[0],
    }


def _report(title: str, counts: dict[str, int]) -> None:
    print(title)
    for name, count in counts.items():
        print(f"  {name:<26} {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "ledger.db"),
        help="database to clear (default: ledger.db in the project root)",
    )
    parser.add_argument(
        "--dest",
        help="directory for the safety backup; required unless --no-backup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted and exit without changing anything",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the safety backup (only sensible when you just took one)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        before = _counts(conn)
    finally:
        conn.close()

    _report(f"Rows in {db_path}:", before)
    if not any(before.values()):
        print("\nNothing to delete; the diet history is already empty.")
        return 0

    if args.dry_run:
        print("\n--dry-run: nothing was changed.")
        return 0

    if not args.no_backup:
        if not args.dest:
            print(
                "\nERROR: --dest is required so a backup exists before deleting. "
                "Pass --no-backup only if you have just taken one.",
                file=sys.stderr,
            )
            return 2
        try:
            backup = create_backup_in(
                db_path, Path(args.dest), expect_version=LATEST_SCHEMA_VERSION
            )
        except (BackupError, VerificationFailed) as exc:
            print(f"\nERROR: backup failed, nothing deleted: {exc}", file=sys.stderr)
            return 1
        print(f"\nVerified backup: {backup.path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # One transaction: a partial wipe would leave orphaned children or
        # receipts pointing at deleted meals.
        with conn:
            for _name, statement in TARGETS:
                conn.execute(statement)
        after = _counts(conn)
        conn.execute("VACUUM")
    finally:
        conn.close()

    print()
    _report("Rows remaining:", after)
    deleted = sum(before.values()) - sum(after.values())
    print(f"\nDeleted {deleted} row(s). Saved foods, recipes, habits, study, gym,")
    print("supplements and the shared catalog were not touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
