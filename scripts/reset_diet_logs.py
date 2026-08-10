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
``--all`` is both.

For a genuine clean slate there is ``--everything``, which additionally clears
daily weight, habit and supplement check-offs, study sessions, filed
suggestions, reminder bookkeeping, and the whole receipt table:

    .\.venv\Scripts\python.exe -m scripts.reset_diet_logs --everything --dry-run

Each kind can also be cleared on its own (``--weight``, ``--habits``,
``--supplements``, ``--study``, ``--suggestions``, ``--reminders``).

**No option deletes a definition.** Saved foods and their portions, recipes,
usual amounts, meal shortcuts, pins and hides, saved exercises, the habits and
supplements themselves, user settings, and the shared catalog all survive every
combination — so configuring the app is never work a wipe can undo.

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

# ---------------------------------------------------------------------------
# The rest of the loggable history. Added when the tables it clears did not yet
# exist: daily weight arrived at schema v13, app suggestions at v14, and the
# supplement and habit check-offs were simply never in scope for the original
# changeover.
#
# Each of these is a *record of something that happened*. Nothing here defines
# anything: the habits, the supplements, the saved foods, the exercises and the
# shared catalog are untouched by every option in this script, so a wipe costs
# setup work only if setup work was stored as a log, and none of it is.
# ---------------------------------------------------------------------------

#: Deliberately NOT cleared, though it looks like history at a glance.
#: ``habit_activity_periods`` records when each habit was *eligible* — it is the
#: denominator adherence divides by, not a record of anything the user did.
#: Deleting it would leave a surviving habit with no eligible days at all, which
#: reads as "no data" rather than as a clean slate. It also costs nothing to
#: keep: weekly adherence clamps every period to the current week, so an
#: activation date from July cannot reach into a fresh start.
KEPT_DESPITE_LOOKING_LIKE_HISTORY = ("habit_activity_periods",)

WEIGHT_TARGETS = (("weight_logs", "DELETE FROM weight_logs"),)
SUPPLEMENT_TARGETS = (("supplement_logs", "DELETE FROM supplement_logs"),)
HABIT_TARGETS = (("habit_logs", "DELETE FROM habit_logs"),)
STUDY_TARGETS = (
    ("study_logs", "DELETE FROM study_logs"),
    ("mutation_receipts (study)", "DELETE FROM mutation_receipts WHERE entity_type = 'study'"),
)
SUGGESTION_TARGETS = (
    ("app_suggestions", "DELETE FROM app_suggestions"),
    (
        "mutation_receipts (suggestion)",
        "DELETE FROM mutation_receipts WHERE entity_type = 'suggestion'",
    ),
)
REMINDER_TARGETS = (("reminder_deliveries", "DELETE FROM reminder_deliveries"),)

WEIGHT_COUNTS = {"weight_logs": "SELECT COUNT(*) FROM weight_logs"}
SUPPLEMENT_COUNTS = {"supplement_logs": "SELECT COUNT(*) FROM supplement_logs"}
HABIT_COUNTS = {"habit_logs": "SELECT COUNT(*) FROM habit_logs"}
STUDY_COUNTS = {
    "study_logs": "SELECT COUNT(*) FROM study_logs",
    "mutation_receipts (study)": (
        "SELECT COUNT(*) FROM mutation_receipts WHERE entity_type = 'study'"
    ),
}
SUGGESTION_COUNTS = {
    "app_suggestions": "SELECT COUNT(*) FROM app_suggestions",
    "mutation_receipts (suggestion)": (
        "SELECT COUNT(*) FROM mutation_receipts WHERE entity_type = 'suggestion'"
    ),
}
REMINDER_COUNTS = {"reminder_deliveries": "SELECT COUNT(*) FROM reminder_deliveries"}

#: ``--everything`` sweeps the receipt table rather than filtering it. Every
#: entity type the bot records — diet, gym, study, suggestion — is being cleared
#: in the same transaction, so a surviving receipt could only ever point at a row
#: that no longer exists, which is the exact replay hazard receipts create.
ALL_RECEIPTS_TARGET = (("mutation_receipts (all)", "DELETE FROM mutation_receipts"),)
ALL_RECEIPTS_COUNTS = {
    "mutation_receipts (all)": "SELECT COUNT(*) FROM mutation_receipts"
}

#: name -> (targets, counts, help). Order matters: children before parents.
OPTIONAL_GROUPS = {
    "weight": (WEIGHT_TARGETS, WEIGHT_COUNTS, "daily weight readings"),
    "supplements": (
        SUPPLEMENT_TARGETS,
        SUPPLEMENT_COUNTS,
        "supplement check-offs (the supplements themselves are kept)",
    ),
    "habits": (
        HABIT_TARGETS,
        HABIT_COUNTS,
        "habit check-offs (the habits themselves are kept)",
    ),
    "study": (STUDY_TARGETS, STUDY_COUNTS, "study sessions"),
    "suggestions": (SUGGESTION_TARGETS, SUGGESTION_COUNTS, "filed app suggestions"),
    "reminders": (
        REMINDER_TARGETS,
        REMINDER_COUNTS,
        "reminder delivery bookkeeping",
    ),
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
    for name, (_targets, _counts_map, description) in OPTIONAL_GROUPS.items():
        parser.add_argument(
            f"--{name}", action="store_true", help=f"also clear {description}"
        )
    parser.add_argument(
        "--everything",
        action="store_true",
        help=(
            "clear every kind of logged history: meals, workouts, weight, "
            "habit and supplement check-offs, study, suggestions, reminder "
            "bookkeeping, and all receipts. Definitions are still kept"
        ),
    )
    args = parser.parse_args(argv)

    do_gym = args.gym or args.all or args.gym_only or args.everything
    do_diet = not args.gym_only or args.everything
    targets = (DIET_TARGETS if do_diet else ()) + (GYM_TARGETS if do_gym else ())
    queries = {
        **(DIET_COUNTS if do_diet else {}),
        **(GYM_COUNTS if do_gym else {}),
    }
    for name, (group_targets, group_counts, _description) in OPTIONAL_GROUPS.items():
        if args.everything or getattr(args, name):
            targets += group_targets
            queries.update(group_counts)
    if args.everything:
        # Sweeps the whole receipt table, which subsumes the per-entity deletes
        # already queued above. Harmless to run both, and cheaper to reason about
        # than working out which entity types the earlier statements missed.
        targets += ALL_RECEIPTS_TARGET
        queries.update(ALL_RECEIPTS_COUNTS)

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
    print(f"\nDeleted {deleted} row(s).")
    print(
        "Kept: saved foods and their portions, recipes, usual amounts, meal "
        "shortcuts, pins and hides, saved exercises, the habits and supplements "
        "themselves, user settings, and the shared catalog."
    )
    if args.everything:
        print(
            "Also kept: " + ", ".join(KEPT_DESPITE_LOOKING_LIKE_HISTORY) + " — the "
            "span each habit has been active for, which adherence divides by "
            "rather than counts."
        )
    else:
        untouched = [
            description
            for name, (_t, _c, description) in OPTIONAL_GROUPS.items()
            if not getattr(args, name)
        ]
        if not do_gym:
            untouched.insert(0, "workouts")
        if untouched:
            print("Not touched by this run: " + "; ".join(untouched) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
