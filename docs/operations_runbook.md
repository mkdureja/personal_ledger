# Ledger operations runbook

Operational guidance for running Ledger in production. For backup/restore and the
pre-migration checklist see [backup_runbook.md](backup_runbook.md).

## Running under a process supervisor

Run the bot as a supervised long-lived process so it restarts cleanly after a
crash or reboot. The bot performs an orderly shutdown (`post_shutdown` closes the
database) on `SIGTERM`.

Example systemd unit (Linux):

```ini
[Unit]
Description=Ledger Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/ledger
ExecStart=/opt/ledger/.venv/bin/python -m bot
Restart=on-failure
RestartSec=5
# Give the bot time to close the DB and flush the WAL on stop.
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

On Windows, run the same `python -m bot` command under NSSM or a Scheduled Task
set to restart on failure.

### Restart behavior

- Pending Telegram updates are intentionally **retained** across restarts
  (`drop_pending_updates=False`), so a command sent during a restart is not lost.
- Replaying a retained update is safe: study/gym/diet writes are idempotent per
  Telegram update (see `mutation_receipts`), and reminder chunk delivery resumes
  at the first undelivered chunk (see `reminder_deliveries`).
- Migrations run automatically at startup and are atomic; a failed migration rolls
  back and leaves the previous schema version usable, and the process aborts
  startup rather than serving on a half-migrated database.
- Startup also **fails closed** if the database's `user_version` is *newer* than
  the running binary understands (`UnsupportedSchemaError`) — e.g. an accidental
  rollback to an older build after a forward migration. Deploy the matching (or
  newer) application version rather than serving against an unknown schema.
- The baseline migration refuses to certify an unrecognized legacy shape: it
  verifies required columns, a clean `foreign_key_check`, and that no `habit_logs`
  row is orphaned or cross-owner, stopping with a sanitized error (no changes made)
  if any check fails.

## Data retention when a user is removed from the allowlist

Removing a numeric ID from `ALLOWED_USER_IDS` only revokes **access** — it does
**not** delete that user's rows. The safe default is **retention**: keep the data
and backups until there is an explicit, verified deletion request from that user.

- The user immediately stops being able to interact and stops receiving reminders.
- Their historical rows remain in the database (and in existing backups).
- If deletion is later requested and verified, delete owner-scoped rows in a
  reviewed, backed-up maintenance step — never as an automatic side effect of an
  allowlist edit.

Removing a user never affects the other user's data: every table is owner-scoped
by numeric `user_id`.

## Dependency audit (pre-release)

Runtime dependencies are pinned in `requirements.txt` (note: `numpy` is declared
directly because `bot/charts.py` imports it). Before a release, in a clean virtual
environment:

```bash
python -m venv .venv-audit
.venv-audit/bin/pip install -r requirements-dev.txt
.venv-audit/bin/pip check                 # no broken/again-conflicting requirements
.venv-audit/bin/python -m pytest -q       # full suite from a clean install
python -m pip install pip-audit && pip-audit -r requirements.txt   # vulnerability scan
```

`pip check` is expected to report no broken requirements. Treat any `pip-audit`
finding on its own current merits — do not copy stale vulnerability claims from
prior reviews. Upgrade only to compatible versions with the full suite still green.

## Secrets and logs

- Never commit `.env`, real Telegram IDs, the bot token, database files, backups,
  or reports containing identifying metadata (`.gitignore` covers the obvious
  cases, including the WAL sidecars).
- The bot silences HTTPX request logging (which embeds the token) and redacts the
  token from any remaining log output; a regression test guards this.
- Migration and delivery diagnostics contain only sanitized counts/categories —
  never a token, username, first name, or raw Telegram ID.
