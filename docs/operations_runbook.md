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

## Phase 1 rollout flags (Home / fast logging)

Phase 1 adds no schema migration (SQLite stays at `user_version = 8`); it is
gated entirely by three startup env vars, read once — **changing them requires a
supervised restart.**

| Var | Meaning |
|---|---|
| `PHASE1_ENABLED_USER_IDS` | Comma-separated subset of `ALLOWED_USER_IDS` with Phase 1 Home + fast mutations on. Empty = off for everyone. |
| `HOME_KEYBOARD_MODE` | One of `off` / `pilot` / `on` / `remove`. |
| `HOME_KEYBOARD_PILOT_USER_IDS` | Subset of `PHASE1_ENABLED_USER_IDS`; consulted only in `pilot`. |

Startup fails closed on a malformed ID, a duplicate, an unknown mode, or a
non-subset ID.

### Release A — dark production configuration (rollback target)

Release A installs all Home routing, disabled-label compatibility, and keyboard
removal, but keeps every fast mutation dark. **This is the only Phase 1 binary
rollback target.** Ship it with:

```text
PHASE1_ENABLED_USER_IDS=
HOME_KEYBOARD_MODE=off
HOME_KEYBOARD_PILOT_USER_IDS=
```

With these, greetings/`Home`/`Meal`/`Repeat`/`Describe` return `/menu`
compatibility guidance plus `ReplyKeyboardRemove`; arbitrary text gets no Phase 1
surface; no snapshot query or mutation runs; and the persistent keyboard is never
sent. `test_release_a.py` proves these synchronization paths in CI.

### Rollback (disable Phase 1 without a DB restore)

1. Set the exact safe block — note `remove`, which forces keyboard removal for
   **every** authorized user regardless of `PHASE1_ENABLED_USER_IDS`:

   ```text
   PHASE1_ENABLED_USER_IDS=
   HOME_KEYBOARD_MODE=remove
   HOME_KEYBOARD_PILOT_USER_IDS=
   ```

2. Restart the current compatible binary (or roll the binary back **only** to
   Release A — it still carries the label/removal handlers).
3. Have each previously enabled user send a greeting or `/keyboard hide` and
   confirm the client keyboard disappears. Rollback is **not** accepted until
   every known user has confirmed this synchronization; no proactive Telegram
   message is implied.
4. Never restore an older DB merely to disable Phase 1 — Phase 1 adds no schema
   and accepted ledger rows must be preserved. Verify `/start`, `/diet`,
   `/recent`, reminders, schema `user_version = 8`, and `PRAGMA foreign_key_check`
   afterward.

### Pilot expansion (enabling Release B fast logging)

1. Add one authorized user to `PHASE1_ENABLED_USER_IDS`, set
   `HOME_KEYBOARD_MODE=pilot`, add only that user to the pilot list, restart.
2. Smoke-test and observe sanitized errors/latency.
3. Add the second user to both lists, restart, complete acceptance.
4. Only then set `HOME_KEYBOARD_MODE=on`, clear `HOME_KEYBOARD_PILOT_USER_IDS`,
   restart, and verify both users.

`/keyboard hide` is momentary: the bar may reappear on the next eligible Home
response. There is no durable per-user hide preference on v8.
