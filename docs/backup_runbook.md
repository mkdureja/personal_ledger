# Ledger backup & restore runbook

The Ledger database is **WAL-backed and live**. `ledger.db` on its own is not a
complete snapshot — recent writes live in the `ledger.db-wal` sidecar until a
checkpoint. Always back up with one of the consistent methods below; never copy
`ledger.db` alone.

> Store every backup **outside the repository** (e.g. `C:\ledger-backups\`). The
> repo's `.gitignore` blocks `*.db`, `*.db-wal`, `*.db-shm`, and `*.db-journal`,
> but a backup placed inside the working tree is still an accident waiting to
> happen. Backups and reports must never contain the bot token or real Telegram
> IDs.

## Option A — online backup while the bot is running (preferred)

Uses SQLite's online backup API, which folds pending WAL state into a single
consistent file. No downtime required.

```powershell
.\.venv\Scripts\python.exe scripts\backup_db.py `
    --source ledger.db `
    --dest   C:\ledger-backups\ledger-$(Get-Date -Format yyyyMMdd-HHmmss).db
```

The script prints sanitized verification only: `user_version`, `integrity_check`,
`foreign_key_check`, and per-table row counts. Record the printed path.

The script **fails closed**: it prints `OK: backup written and verified` and exits
`0` only when `integrity_check` returns `ok` and `foreign_key_check` finds no
violations. If either fails it renames the file to `*.INVALID`, prints an error,
and exits non-zero — so a corrupt copy can never be mistaken for a usable rollback
point. Always check the exit code before treating a backup as your pre-migration
safety net.

## Option B — clean shutdown + checkpoint + copy

1. Stop the bot process cleanly (let `post_shutdown` close the connection).
2. Checkpoint the WAL into the main file:

   ```powershell
   .\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('ledger.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
   ```

3. Copy **all** current database files together: `ledger.db`, and if present
   `ledger.db-wal` and `ledger.db-shm`.

## Restore

1. Stop the bot.
2. Move the current (failed/suspect) files aside for diagnosis — do **not** delete
   them:

   ```powershell
   Move-Item ledger.db     ledger.db.suspect
   Move-Item ledger.db-wal ledger.db-wal.suspect -ErrorAction SilentlyContinue
   Move-Item ledger.db-shm ledger.db-shm.suspect -ErrorAction SilentlyContinue
   ```

3. Copy the verified backup into place as `ledger.db` (a backup made with Option A
   is a single file and needs no sidecars).
4. Verify before starting the bot:

   ```powershell
   .\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('ledger.db'); print('user_version', c.execute('PRAGMA user_version').fetchone()[0]); print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
   ```

5. Start the previous release with the previous allowlist. Confirm row counts match
   the backup report before re-enabling both users.

## Pre-migration checklist

Before any production migration (see `implementation_plan.md`, Phase 0 and the
Production rollout gates):

- [ ] Fresh backup taken with Option A or B and its path recorded.
- [ ] `integrity_check` and `foreign_key_check` pass on the backup.
- [ ] Row counts captured (sanitized) for before/after comparison.
- [ ] Restore rehearsed to a temporary path and verified.
- [ ] Manoj/Ratika numeric-ID mapping confirmed out of band, without changing rows.
