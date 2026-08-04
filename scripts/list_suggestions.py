r"""Read what the users have suggested about the app.

``/suggest`` files an idea from inside Telegram; this is the other end of it.
It exists because a capture command with no read path is a hole: the person who
would act on a suggestion is at a terminal in this repository, not in the chat.

    .\.venv\Scripts\python.exe -m scripts.list_suggestions
    .\.venv\Scripts\python.exe -m scripts.list_suggestions --limit 10
    .\.venv\Scripts\python.exe -m scripts.list_suggestions --user 1554408692

It only ever reads: there is no flag here that edits or clears anything.
Withdrawing a suggestion is the sender's to do, from the receipt in their own
chat, which is the only place the ownership check can be honest.

Safe to run while the bot is polling — a SELECT takes a shared read lock and
this database runs in WAL mode, so it never blocks a write.

Timestamps are stored in UTC and printed in UTC, labelled as such — a
maintenance script has no user whose local day it should be answering in.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Suggestions arrived before this table existed only in the sense that people
#: had ideas; a database older than v14 simply has nowhere to have stored one.
_TABLE = "app_suggestions"


def _rows(
    conn: sqlite3.Connection, user_id: int | None, limit: int
) -> list[sqlite3.Row]:
    """Newest first, joined to whatever name the users table knows."""
    sql = (
        "SELECT s.id, s.user_id, s.suggestion, s.created_at, "
        "       u.first_name, u.username "
        f"FROM {_TABLE} AS s "
        "LEFT JOIN users AS u ON u.user_id = s.user_id "
    )
    params: tuple[object, ...] = ()
    if user_id is not None:
        sql += "WHERE s.user_id = ? "
        params = (user_id,)
    sql += "ORDER BY s.id DESC LIMIT ?"
    return conn.execute(sql, (*params, limit)).fetchall()


def _who(row: sqlite3.Row) -> str:
    """Name the sender the way a person would recognize them."""
    name = row["first_name"] or row["username"]
    return f"{name} ({row['user_id']})" if name else str(row["user_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "ledger.db"),
        help="database to read (default: ledger.db in the project root)",
    )
    parser.add_argument(
        "--user",
        type=int,
        help="only this Telegram user's suggestions",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="how many to show, newest first (default: 50)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2

    # An ordinary connection, deliberately not ``immutable=1``: immutable mode
    # promises SQLite the file cannot change and so it never opens the -wal
    # sidecar. Against a live bot that hides every suggestion committed since
    # the last checkpoint — which is exactly the newest ones, the reason anybody
    # runs this.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = _rows(conn, args.user, max(1, args.limit))
        except sqlite3.OperationalError:
            # Plain ASCII output: this prints to a Windows console whose code
            # page mangles anything else.
            print(
                f"No {_TABLE} table in {db_path} - this database predates schema "
                "v14, so no suggestion could have been stored yet.",
                file=sys.stderr,
            )
            return 1
    finally:
        conn.close()

    if not rows:
        scope = f" from user {args.user}" if args.user else ""
        print(f"No suggestions{scope} yet.")
        return 0

    print(f"{len(rows)} suggestion(s), newest first; times are UTC\n")
    for row in rows:
        print(f"#{row['id']}  {row['created_at']}  {_who(row)}")
        for line in str(row["suggestion"]).splitlines() or [""]:
            print(f"    {line}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
