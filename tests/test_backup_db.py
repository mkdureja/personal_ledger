"""Backup verification must fail closed on a corrupt copy (codex finding #12).

A backup is the rollback point taken immediately before a one-time production
migration. The script must never print ``OK`` / exit zero for a copy that fails
its integrity or foreign-key checks.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.py"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location("backup_db", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


backup_db = _load_backup_module()


def _make_healthy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO users (user_id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def _make_fk_violating_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE study_logs ("
            "id INTEGER PRIMARY KEY, user_id INTEGER, "
            "FOREIGN KEY (user_id) REFERENCES users(user_id))"
        )
        # Orphan row: no matching parent, so foreign_key_check reports a problem.
        conn.execute("INSERT INTO study_logs (id, user_id) VALUES (1, 999)")
        conn.commit()
    finally:
        conn.close()


def test_backup_succeeds_on_healthy_db(tmp_path):
    source = tmp_path / "ledger.db"
    dest = tmp_path / "backup.db"
    _make_healthy_db(source)
    assert backup_db.backup(source, dest) == 0
    assert dest.exists()


def test_backup_fails_and_marks_invalid_on_fk_violation(tmp_path):
    source = tmp_path / "ledger.db"
    dest = tmp_path / "backup.db"
    _make_fk_violating_db(source)

    rc = backup_db.backup(source, dest)

    assert rc == 1
    # The verified-bad copy is renamed so it cannot be mistaken for a rollback point.
    assert not dest.exists()
    assert (tmp_path / "backup.db.INVALID").exists()
