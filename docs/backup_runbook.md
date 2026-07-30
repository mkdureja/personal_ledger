# Ledger backup & restore runbook

The Ledger database is **WAL-backed and live**. `ledger.db` on its own is not a
complete snapshot — recent writes live in the `ledger.db-wal` sidecar until a
checkpoint. Always back up with one of the consistent methods below; never copy
`ledger.db` alone.

> Store every backup **outside the repository**. The repo's `.gitignore` blocks
> `*.db`, `*.db-wal`, `*.db-shm`, and `*.db-journal`, but a backup placed inside
> the working tree is still an accident waiting to happen — and the tool now
> refuses an in-repository destination outright. Backups and reports must never
> contain the bot token or real Telegram IDs.

## Destination and security decision (settled)

| Decision | Value |
|---|---|
| Decision | Value | Status |
|---|---|---|
| Destination | `E:\ledger-backups` | ✅ in place, verified |
| Scope | **Local-only**, on a different physical disk than the repository (`D:`) | ✅ |
| Cloud sync | **Excluded.** Never place a plaintext database backup in cloud storage | ✅ |
| At-rest encryption | Intended: device/disk encryption of the host volume | ❌ **not satisfied** — see below |
| Filesystem permissions | Intended: restricted to the operating account | ❌ not reviewed |
| Retention | Rolling: the newest 10 routine backups per schema version | ✅ enforced by the tool |
| Scheduling | Daily task on the host that runs the bot | ❌ not registered |

> **Open gap, checked 2026-07-30.** `Get-BitLockerVolume` reports every volume on
> this host — including `E:` — as `FullyDecrypted`, `ProtectionStatus: Off`. The
> recorded policy allows a plaintext local backup **only** on a
> device/disk-encrypted volume, so the backups now sitting on `E:` do not yet meet
> it. They are a complete, unencrypted copy of both users' ledgers on a drive with
> inherited default permissions. Close this by either:
>
> 1. enabling BitLocker on `E:` (`Enable-BitLocker -MountPoint E: …`) and
>    tightening the folder ACL to the operating account; or
> 2. deciding the household accepts unencrypted local backups, and editing the
>    policy above to say so — an accepted risk is fine, an undocumented one is not.
>
> No scheduled task exists yet either (`Get-ScheduledTask` matches nothing named
> Ledger), so backups are currently manual. The command to register one is in
> **Scheduling** below; it is not run automatically because it changes the host's
> configuration.

The two acceptable policies are: a plaintext local backup on a verified
device/disk-encrypted volume that is excluded from cloud sync (the choice
recorded above); or, for anything synced or moved off-host, file/archive-layer
encryption applied *before* transfer with its recovery key stored separately. If
the destination ever changes to a synced or off-host target, choose an archive
tool and key-recovery method first and record them here — the second policy is
not satisfied by the current setup.

Separate disk, same host: this survives a repository mistake, a bad migration, or
a `D:` failure. It does **not** survive loss of the machine. That is an accepted
limit of a two-user household ledger, not an oversight.

The one historical rollback point that used to sit in the working tree
(`ledger.db.bak-*`, created by a manual file copy) was verified and relocated to
this destination as `ledger-legacy-manual-v8-*.db`. Its `ledger-legacy-` prefix
keeps it outside rolling retention. No database or backup file remains inside the
repository.

**Side effect worth knowing:** creating or verifying a backup opens the file
read-write, so closing it checkpoints the WAL into the main file and removes the
`-wal`/`-shm` sidecars. No rows change — the WAL held already-committed data — and
it is what a clean shutdown does anyway. Read-only access is deliberately not
used, because a read-only connection cannot recover a leftover WAL whose `-shm` is
missing, and that is precisely the post-crash case where a backup matters most.

## Option A — online backup while the bot is running (preferred)

Uses SQLite's online backup API, which folds pending WAL state into a single
consistent file. No downtime required.

```powershell
.\.venv\Scripts\python.exe -m scripts.backup_db `
    --source ledger.db `
    --dest   E:\ledger-backups `
    --expect-version latest
```

Run it from the repository root. `python -m scripts.backup_db` is the documented
invocation: it puts the root on `sys.path` so the standard-library-only script can
import the shared `ledger_schema` / `ledger_backup` contract — the same code the
bot's startup verifier and migration preflight use — with no path hack and no
application import.

Both arguments are mandatory by design:

- `--dest` has **no default**. It must resolve outside the repository, and may be
  a directory (the file is then auto-named `ledger-v<version>-<UTC stamp>Z.db`) or
  an explicit filename.
- `--expect-version` states your intent. Use `latest` for a routine backup of a
  current database; the command fails if the source is behind, so a stale copy can
  never be certified as current. Use the exact integer version for a
  pre-migration backup — a v7 backup taken by a v8 build is *correct* at v7.

The command prints sanitized verification only: `user_version`,
`integrity_check`, `foreign_key_check`, table count, and per-table row counts for
every user-owned table required at the stamped version. Record the printed path.

It **fails closed**: `OK: backup written and verified` and exit `0` happen only
when the copy carries its source's version, contains every table required at that
version, returns `integrity_check = ok`, and has zero foreign-key violations. On
failure it renames the file to `*.INVALID`, prints an error, and exits non-zero —
so a corrupt copy can never be mistaken for a usable rollback point. Always check
the exit code before treating a backup as your pre-migration safety net.

### Verify an existing backup (restore rehearsal)

```powershell
.\.venv\Scripts\python.exe -m scripts.backup_db `
    --verify-only E:\ledger-backups\ledger-v8-20260730-101500Z.db
```

`--verify-only` accepts any known stamped version from 1 to the current one, so an
older pre-migration rollback point stays verifiable under a newer build. Add
`--expect-version <N>` to assert a specific restore target. Two cases are
rejected with distinct messages: a legacy unversioned (`user_version = 0`)
database, which is not schema-certifiable at all, and a version newer than this
checkout, which needs the matching build.

### Scheduling

Create the destination once, then schedule the routine backup daily on the host
that runs the bot:

```powershell
New-Item -ItemType Directory -Force E:\ledger-backups

$action  = New-ScheduledTaskAction -Execute "D:\claude\12_ledger\.venv\Scripts\python.exe" `
    -Argument "-m scripts.backup_db --source ledger.db --dest E:\ledger-backups --expect-version latest" `
    -WorkingDirectory "D:\claude\12_ledger"
$trigger = New-ScheduledTaskTrigger -Daily -At 3:30am
Register-ScheduledTask -TaskName "Ledger backup" -Action $action -Trigger $trigger
```

Retention is handled by the tool: `--keep` (default 10) prunes the oldest
auto-named routine backups sharing a version prefix. Pre-migration rollback
points use a distinct `ledger-premigration-v<N>-` prefix and are **never** pruned
automatically.

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

3. Rehearse first: copy the backup to a temporary path and verify **that copy**,
   so a bad rollback point is discovered before it becomes the live database.

   ```powershell
   Copy-Item E:\ledger-backups\ledger-v8-20260730-101500Z.db $env:TEMP\ledger-restore-test.db
   .\.venv\Scripts\python.exe -m scripts.backup_db --verify-only $env:TEMP\ledger-restore-test.db
   Remove-Item $env:TEMP\ledger-restore-test.db
   ```

4. Copy the verified backup into place as `ledger.db` (a backup made with Option A
   is a single file and needs no sidecars), then verify it in place:

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.backup_db --verify-only ledger.db
   ```

5. Start only a release that supports the restored `user_version`. For a
   deployment rollback, use the previous release only with its matching
   pre-migration backup and previous allowlist. Confirm row counts match the
   backup report before re-enabling both users.

## Pre-migration backup: enforced at startup

Migrations are no longer gated by a checklist you have to remember. Startup runs
`bot/migration_preflight.py::prepare_database_for_startup()` after connecting and
**before** `init_db()`, and it refuses to migrate without a freshly verified
backup of the exact source it is about to change.

Set the destination once in `.env`:

```ini
BACKUP_DEST_DIR=E:\ledger-backups
```

It must resolve outside the project root, and a value inside the repository is
rejected at startup. What the preflight does, by case:

| Live `user_version` | Behavior |
|---|---|
| equal to this build's | Start normally; no backup created |
| newer than this build's | Abort with `UnsupportedSchemaError`; **no** backup, no schema change |
| `0` with no application tables | Genuinely new database: create the schema, no meaningless backup |
| `1`..latest-1 | Create and verify `ledger-premigration-v<N>-<stamp>Z.db`, then migrate |
| `0` but populated (legacy) | Create a source-matching backup, then rehearse the whole migration on a throwaway copy; touch the live file only if the rehearsal reaches and verifies the latest version |
| any pending, `BACKUP_DEST_DIR` unset | **Refuse to start**, exit non-zero, no schema change |

The rollback copy is kept at its **source** version — a v7 backup taken before a
v8 migration is correct at v7 — and the copy itself is never migrated. Rehearsals
run on a temporary file that is deleted afterwards. Pre-migration backups are
never touched by rolling retention.

`run_migrations()` stays the low-level primitive the migration tests drive
directly; it acquires no operational paths and reads no deployment configuration.
That is why the whole suite passes with no exemption flag. **Any future
executable migration or administration entry point must call the same preflight
function** — calling `run_migrations()` directly is not an approved production
path.

### Before a schema-changing deployment

Automation covers the backup itself. These remain human steps:

- [ ] Confirm `BACKUP_DEST_DIR` points at the recorded destination and the disk
      has room.
- [ ] Read the startup log line naming the verified backup, and record the path.
- [ ] Rehearse a restore of that backup with `--verify-only` (see Restore above).
- [ ] Note the sanitized row counts for a before/after comparison.
- [ ] User A/User B numeric-ID mapping confirmed out of band, without changing rows.
