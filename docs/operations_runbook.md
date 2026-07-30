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

### Single-instance enforcement

Exactly one polling process may run per database, and startup now **enforces**
it. `main()` takes an exclusive OS lock on `<DB_PATH>.instance.lock`
(`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) *before* anything opens or
migrates the database. A second process logs a message naming the lock file and
exits non-zero without touching a byte of data.

So: stop the supervised service before running `python -m bot` by hand — the
manual run will refuse to start otherwise. Two processes would split guided
drafts held in per-process `user_data` and could each send the same reminder
chunk, because delivery state is read once before the send.

The lock is held by the running process's open handle, so the operating system
releases it automatically if that process is killed. The leftover lock *file* is
not a stale marker and must not be deleted to "fix" a refusal — if a start is
refused, another process really is running. Scope: same-host only. It does not
address a multi-host deployment, and it does not close the send-then-record
crash window in reminder delivery.

### Restart behavior

- Pending Telegram updates are intentionally **retained** across restarts
  (`drop_pending_updates=False`), so a command sent during a restart is not lost.
- Replaying a retained update is safe: study/gym/diet writes are idempotent per
  Telegram update (see `mutation_receipts`), and reminder chunk delivery resumes
  at the first undelivered chunk (see `reminder_deliveries`).
- Migrations run automatically at startup and are atomic; a failed migration rolls
  back and leaves the previous schema version usable, and the process aborts
  startup rather than serving on a half-migrated database.
- The current build does **not** create an automatic pre-migration backup. Before
  starting a build whose `LATEST_VERSION` exceeds the live `user_version`,
  complete the manual checklist in `backup_runbook.md`. The active implementation
  plan replaces this manual-only gap with a fail-closed production preflight.
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
- Migration, reminder, and Repeat diagnostics contain only sanitized
  counts/categories — never a token, username, first name, or raw Telegram ID. A
  chat ID is the user's identity, so delivery failures are logged by category and
  attributed per user only in the private `reminder_deliveries` table:

  ```sql
  SELECT job_key, local_date, chunk_index, delivered, error_category
  FROM reminder_deliveries WHERE user_id = ? ORDER BY id DESC LIMIT 20;
  ```

- The manual send helper (`scripts/send_bot_message.py`) redacts the token from
  Telegram error output, which can otherwise carry the Bot API request URL.

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

With these, greetings and `Home` still render the read-only Today snapshot plus
the inline main menu; the persistent quick-action keyboard is never sent. The
disabled `Meal` label returns `/diet` guidance and removes a stale keyboard,
while `Repeat`/`Describe` return disabled guidance. Arbitrary text gets no Phase
1 surface and no fast mutation runs. `off` does not force keyboard removal on
every ordinary Home response; use the `remove` rollback mode below when every
user must clear a previously sent bar. `test_release_a.py` proves the relevant
paths in the local suite; the active plan adds required GitHub CI.

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

### Release B fast mutations

Enabling `PHASE1_ENABLED_USER_IDS` turns on the whole fast-logging surface.
Schema stays at `user_version = 8` throughout. What to check when smoke-testing:

**Repeat and receipts**

- **Repeat** re-logs the most recent meal as an *exact copy* — same meal type,
  description, calories, macros, and item snapshots, with only a new id and
  timestamp. Nothing is re-resolved, so editing a food afterwards never changes
  what Repeat writes. Repeating with no history answers `Nothing to repeat yet`
  and writes nothing.
- Every fast log renders a **receipt**: the exact meal id plus `↩️ Undo`,
  `🍽️ Log another`, and — when the meal has a structured item —
  `🔄 Log again at today's values`. Receipts are durable: an older receipt keeps
  working after newer meals are logged, and its `Undo` removes *that* meal.
- **Undo** works during another guided flow (it only touches a completed meal);
  the other two do not, because they open a flow. Undo is refused past 24h and
  is a harmless no-op when pressed twice.

**Quick mode and "usual" amounts**

- Tapping 🍽️ **Meal** opens a one-item Quick log. A food with a saved *usual*
  amount logs in a single tap; otherwise the user picks an amount and gets
  `Log it` / `Log + set as my usual` / `Change amount` / `Cancel`.
- `Log + set as my usual` commits the meal and the preference in one
  transaction — you never get one without the other.
- ⚙️ beside a saved item opens its default menu (set/change/repair/remove). A
  default that no longer resolves (portion renamed, food archived) is reported
  as needing repair; it is never silently ignored or approximated.
- `/suggestions reset` still clears only pins/hides. Saved usual amounts survive.
- Shared catalog items have no usual amount and no pin/hide on v8.

**Log again at today's values**

- Re-prices each structured item from the live source and shows old → new per
  item and in total, then writes only on confirmation.
- If an item cannot be re-priced, the user must choose `Keep as logged` or
  `Drop` for it; `Log it` does not appear until every issue is answered and at
  least one item remains.
- If the underlying values change between preview and Save, the save is refused
  and the fresh preview is shown for another confirmation.

**Picker**

- Suggestions paginate at 8 per page, and include shared-catalog foods the user
  has actually logged before (never the whole catalog — that stays behind
  Search). With `/suggestions off` the list is the user's own foods/recipes
  alphabetically, with no learned catalog history.
- `🕒 Change meal type` reopens the meal picker without disturbing the draft.
- In the Builder, each drafted item has `✍️ amount`, `🔁 replace`, and `🗑`. Any
  draft change bumps the UI revision, so a keyboard left on screen stops acting.

**Replay and rollback**

- Redelivered updates (the bot restarts with `drop_pending_updates=False`) replay
  their recorded outcome instead of logging a second meal — including "there was
  nothing to repeat", "that repeat was already undone", and the Quick/current
  variants, which each carry their own operation key.
- After a rollback to `PHASE1_ENABLED_USER_IDS=`, receipt and Phase 1 buttons
  stop mutating: they answer, retire themselves, and point at the commands.
  Meals already written stay written. The Builder falls back to its pre-Phase-1
  decimal keyboard and remains fully saveable.
