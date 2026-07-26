r"""Consistent, WAL-safe backup of the Ledger SQLite database.

Uses SQLite's online backup API, which captures a transactionally consistent
snapshot **while the bot is running** and folds any pending WAL state into a
single self-contained ``.db`` file. This is the safe alternative to copying
``ledger.db`` on its own — a bare file copy misses the ``-wal``/``-shm`` sidecars
and can restore a torn, older state.

Usage (from the repo root, using the project venv):

    .\.venv\Scripts\python.exe scripts\backup_db.py \
        --source ledger.db \
        --dest   C:\ledger-backups\ledger-YYYYMMDD-HHMMSS.db

The destination directory should live **outside** the repository so a backup is
never committed. The script prints only sanitized diagnostics: schema version,
integrity result, and per-table row counts. It never prints Telegram user IDs,
usernames, first names, or the bot token.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Tables whose row counts are safe to report as totals (no identifying content).
_COUNTED_TABLES = (
    "users",
    "study_logs",
    "gym_logs",
    "diet_logs",
    "foods",
    "food_portions",
    "recipes",
    "recipe_ingredients",
    "habits",
    "habit_logs",
)


def _default_dest(source: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return source.with_name(f"{source.stem}-backup-{stamp}.db")


def backup(source: Path, dest: Path) -> int:
    """Create a consistent backup of ``source`` at ``dest``. Returns 0 on success."""
    if not source.exists():
        print(f"ERROR: source database not found: {source}", file=sys.stderr)
        return 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"ERROR: destination already exists, refusing to overwrite: {dest}", file=sys.stderr)
        return 2

    # Open the live database read-only via the online backup API. The source is
    # opened normally (backup needs a read transaction); the destination is a
    # fresh file that receives a fully checkpointed copy.
    src = sqlite3.connect(str(source))
    try:
        out = sqlite3.connect(str(dest))
        try:
            with out:
                src.backup(out)
            _report(out)
        finally:
            out.close()
    finally:
        src.close()

    print(f"OK: backup written to {dest}")
    print("Record this path and the restore command in your deployment runbook.")
    return 0


def _report(conn: sqlite3.Connection) -> None:
    """Print sanitized verification output for a freshly written backup."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    print(f"schema user_version : {version}")
    print(f"integrity_check     : {integrity}")
    print(f"foreign_key_check   : {'OK' if not fk_problems else f'{len(fk_problems)} problem(s)'}")
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    print("row counts (sanitized):")
    for table in _COUNTED_TABLES:
        if table in existing:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            print(f"  {table:<20} {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="ledger.db", type=Path, help="Live database path")
    parser.add_argument("--dest", type=Path, default=None, help="Backup destination (outside the repo)")
    args = parser.parse_args(argv)
    dest = args.dest or _default_dest(args.source)
    return backup(args.source, dest)


if __name__ == "__main__":
    raise SystemExit(main())
