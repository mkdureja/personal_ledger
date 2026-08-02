r"""Delete logged history for a fresh start: meals, and optionally workouts.

Written for the changeover to mandatory nutrition: the existing meals were a
mix of test entries and rows saved before calories and macros were required, so
they could never be totalled. Rather than backfill numbers nobody can remember,
they are cleared and real logging starts from a clean ledger.

It is a *script*, not a bot command, on purpose. Wiping history should take a
deliberate act at a terminal, not a mistappable button next to "Log meal".

    .\.venv\Scripts\python.exe -m scripts.reset_diet_logs --dry-run
    .\.venv\Scripts\python.exe -m scripts.reset_diet_logs --dest E:\ledger-backups
    .\.venv\Scripts\python.exe -m scripts.reset_diet_logs --gym --dest E:\ledger-backups

By default it touches ``diet_logs``, ``diet_log_items``, and the ``diet`` rows of
``mutation_receipts`` (a stale receipt would otherwise make a replayed Telegram
update resolve to a meal id that no longer exists).

``--gym`` adds ``gym_logs``, its ``gym_sets`` children, and the ``gym`` receipts.
``--all`` is both. Habits, study, supplements, saved foods, saved exercises,
recipes, and the shared catalogs are never touched by any of them.

A verified backup is taken first unless ``--no-backup`` is given, and the
deletion runs in one transaction: it either all happens or none of it does.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ledger_backup import (
    BackupError,
    VerificationFailed,
    create_backup_in,
    inspect_database,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Every meal write records ``entity_type = 'diet'``. The *operation_key* varies
#: by entry point (``diet_log``, ``diet_quick``, ``diet_repeat``), so filtering
#: on that would silently leave most receipts behind — pointing at meal ids that
#: no longer exist, which is precisely the replay hazard this clears.
_DIET_RECEIPTS = "FROM mutation_receipts WHERE entity_type = 'diet'"

_GYM_RECEIPTS = "FROM mutation_receipts WHERE entity_type = 'gym'"

#: Deleted in this order so children never outlive their parent.
DIET_TARGETS = (
    ("diet_log_items", "DELETE FROM diet_log_items"),
    ("diet_logs", "DELETE FROM diet_logs"),
    ("mutation_receipts (diet)", f"DELETE {_DIET_RECEIPTS}"),
)
GYM_TARGETS = (
    ("gym_sets", "DELETE FROM gym_sets"),
    ("gym_logs", "DELETE FROM gym_logs"),
    ("mutation_receipts (gym)", f"DELETE {_GYM_RECEIPTS}"),
)

DIET_COUNTS = {
    "diet_logs": "SELECT COUNT(*) FROM diet_logs",
    "diet_log_items": "SELECT COUNT(*) FROM diet_log_items",
    "mutation_receipts (diet)": f"SELECT COUNT(*) {_DIET_RECEIPTS}",
}
GYM_COUNTS = {
    "gym_logs": "SELECT COUNT(*) FROM gym_logs",
    "gym_sets": "SELECT COUNT(*) FROM gym_sets",
    "mutation_receipts (gym)": f"SELECT COUNT(*) {_GYM_RECEIPTS}",
}


def _counts(conn: sqlite3.Connection, queries: dict[str, str]) -> dict[str, int]:
    """Row counts, tolerating a table a pre-v11 database has not got yet."""
    counts: dict[str, int] = {}
    for label, sql in queries.items():
        try:
            counts[label] = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            continue
    return counts


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
    parser.add_argument(
        "--gym",
        action="store_true",
        help="also clear workouts (gym_logs, gym_sets, and their receipts)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="clear both meals and workouts",
    )
    parser.add_argument(
        "--gym-only",
        action="store_true",
        help="clear workouts and leave meals alone",
    )
    args = parser.parse_args(argv)

    do_gym = args.gym or args.all or args.gym_only
    do_diet = not args.gym_only
    targets = (DIET_TARGETS if do_diet else ()) + (GYM_TARGETS if do_gym else ())
    queries = {
        **(DIET_COUNTS if do_diet else {}),
        **(GYM_COUNTS if do_gym else {}),
    }

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        before = _counts(conn, queries)
    finally:
        conn.close()

    _report(f"Rows in {db_path}:", before)
    if not any(before.values()):
        print("\nNothing to delete; that history is already empty.")
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
            # Back up at whatever version the file *is*, not at the checkout's
            # latest: this script clears rows, it does not migrate, and asserting
            # "latest" would refuse to protect a database still awaiting one.
            backup = create_backup_in(
                db_path, Path(args.dest), expect_version=inspect_database(db_path).user_version
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
            for _name, statement in targets:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    continue  # table not present in this (older) database
        after = _counts(conn, queries)
        conn.execute("VACUUM")
    finally:
        conn.close()

    print()
    _report("Rows remaining:", after)
    deleted = sum(before.values()) - sum(after.values())
    kept = "Saved foods, recipes, saved exercises, habits, study, supplements"
    if not do_gym:
        kept += ", workouts"
    print(f"\nDeleted {deleted} row(s). {kept}")
    print("and the shared catalogs were not touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
