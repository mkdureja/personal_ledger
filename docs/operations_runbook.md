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
- Startup **refuses to migrate without a freshly verified backup** of the exact
  source it is about to change. The preflight runs after connecting and before
  `init_db()`; with a pending migration and no `BACKUP_DEST_DIR` configured, the
  process exits non-zero and leaves `user_version` unchanged. See
  `backup_runbook.md` for the per-case table (new database, known version,
  populated legacy version 0, future version).
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

## Optional external parsing (Gemini)

`GEMINI_API_KEY` is unset by default and unset is fully supported: meal parsing
stays local and deterministic. The key is a live credential — treat it exactly
like `BOT_TOKEN`. It belongs only in the gitignored `.env`, travels in an HTTP
header rather than a URL, and is never logged. **If it is ever pasted into a
chat, an issue, or a terminal transcript, revoke it and issue a new one;
generating a second key does not disable the first.**

Configuring a key only makes the capability available. Each user must still opt
in with `/aiparse on`, stored per user and defaulting to off, and only the part
of a `/describe` message the local parser could not resolve is ever sent.

`GEMINI_MODEL` defaults to `gemini-flash-latest`, chosen by measuring the live
free tier rather than by reputation — and then re-measured when the first choice
degraded in service on the same day:

| Model | Result |
| --- | --- |
| `gemini-flash-latest` | 3.6 s, correct, 5 requests/minute free — **default** |
| `gemini-flash-lite-latest` | measured 1.6 s, then hung past 45 s hours later |
| `gemini-2.0-flash` | free-tier quota of zero — unusable |
| `gemini-2.5-flash`, `-lite` | HTTP 404 on this tier |

**If parsing quietly stops helping, suspect a stalled model first.** A hung
provider looks identical to "the AI didn't add anything", because the call fails
soft by design. Change `GEMINI_MODEL` and restart; no code change is needed.
That a `-latest` alias can degrade under you is the reason the model is
configuration rather than a constant.

`thinkingConfig: {thinkingBudget: 0}` is rejected with HTTP 400 by the newer
Flash models, so reasoning cost is avoided by choosing a non-thinking tier. A
Google One AI Premium ("Gemini Plus") subscription does **not** raise API limits;
the 429s name `generate_content_free_tier_requests`. Higher limits require
billing on the key's Cloud project.

Quota exhaustion is not an incident: the call fails soft and the deterministic
result stands.

## Optional local voice notes

`VOICE_ENABLED` defaults to false and `faster-whisper` is **not** installed by
`requirements.txt`. Enable it only on a host that should transcribe:

```bash
pip install -r requirements-voice.txt   # then set VOICE_ENABLED=true and restart
```

- **Disk:** the model is downloaded on first use into the Hugging Face cache
  (`~/.cache/huggingface`, or `%USERPROFILE%\.cache\huggingface` on Windows).
  `base` is the default and the smallest that handles ordinary meal dictation;
  larger sizes cost proportionally more disk, memory, and time per note. Size the
  volume before enabling, and note the cache is *outside* the project directory,
  so it is not covered by the backup destination.
- **CPU:** transcription runs on CPU in `int8` and is serialized by a lock, so
  two notes queue rather than competing. It is the most CPU-intensive thing this
  bot does.
- **Python version:** verified installing on 3.14 (`ctranslate2` 4.8.1). Those
  are compiled wheels, so a future interpreter could outpace them; a failed
  install is not an outage, since without the package voice notes reply "type it
  instead" and everything else is unaffected.
- **First note is slow.** Loading the model took ~21 s the first time, including
  the download; afterwards it stays in memory and a short note takes ~1 s. The
  first voice note after a restart therefore pauses noticeably.
- **Privacy:** audio never leaves the host. It is written to a temporary
  directory that is removed whatever happens, is never stored, and is never
  attached to a log row. There is deliberately no consent switch, because nothing
  is transmitted.
- **Durability:** there is no persisted job queue. A note being transcribed when
  the process stops is lost, not resumed. That is intentional for a two-user
  deployment; the user simply re-sends it.

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

> **Schema note.** This section was written when the database was at
> `user_version = 8`. Later work has migrated it forward to **13** (supplements
> at v9, AI-parsing consent columns at v10, per-set gym logging at v11, meal
> shortcuts at v12, daily weight at v13; `ledger_schema.LATEST_SCHEMA_VERSION`
> is the source of truth). Phase 1 itself still adds no migration and its flags
> are unchanged — but **any binary predating v9 will refuse to start against the
> current database**, by design, so the Release A rollback target below is no
> longer usable. See "Binary rollback" before rolling anything back.

Phase 1 is gated entirely by three startup env vars, read once — **changing them
requires a supervised restart.**

| Var | Meaning |
|---|---|
| `PHASE1_ENABLED_USER_IDS` | Comma-separated subset of `ALLOWED_USER_IDS` with Phase 1 Home + fast mutations on. Empty = off for everyone. |
| `HOME_KEYBOARD_MODE` | One of `off` / `pilot` / `on` / `remove`. |
| `HOME_KEYBOARD_PILOT_USER_IDS` | Subset of `PHASE1_ENABLED_USER_IDS`; consulted only in `pilot`. |

Startup fails closed on a malformed ID, a duplicate, an unknown mode, or a
non-subset ID.

### Release A — dark production configuration (historical)

Release A installs all Home routing, disabled-label compatibility, and keyboard
removal, but keeps every fast mutation dark. It was the Phase 1 binary rollback
target **while the database was at v8**; it is a v8 binary and will now refuse to
start against the v10 database (see "Binary rollback"). The configuration below
still describes what "Phase 1 dark" means on a current binary. Ship it with:

```text
PHASE1_ENABLED_USER_IDS=
HOME_KEYBOARD_MODE=off
HOME_KEYBOARD_PILOT_USER_IDS=
```

With these, greetings, `/home`, `/menu`, `/start`, and `Home` still render the
read-only Today snapshot plus the inline Home actions; the persistent
quick-action keyboard is never sent, and Home omits the last-meal line because
Repeat is not available. The disabled `Meal` label returns `/diet` guidance and
removes a stale keyboard, while `Repeat last meal` (and the legacy `Repeat`) and
`Describe` return disabled guidance. No fast mutation runs and no picker row is
marked `⚡`.

Unrecognized idle text is answered with one short recovery reply carrying the
inline Home actions. That is deliberate as of Release 1 and safe with Phase 1
off: one message, no snapshot query, no mutation, and no persistent keyboard.

`off` does not force keyboard removal on every ordinary Home response; use the
`remove` rollback mode below when every user must clear a previously sent bar.
`test_release_a.py` and `test_release_1.py` prove these paths, and
`.github/workflows/tests.yml` runs the whole suite on both `windows-latest` and
`ubuntu-latest` for every push and pull request.

**Label compatibility window.** The bar now renders `Repeat last meal`; the old
`Repeat` text keeps routing to the same action for one release, because a
persistent keyboard already on a client sends the old label until it receives a
new one. Both are intercepted during an active guided flow. Do not remove the
legacy label until both users have received a Home response from this build.

### Rollback (disable Phase 1 without a DB restore)

1. Set the exact safe block — note `remove`, which forces keyboard removal for
   **every** authorized user regardless of `PHASE1_ENABLED_USER_IDS`:

   ```text
   PHASE1_ENABLED_USER_IDS=
   HOME_KEYBOARD_MODE=remove
   HOME_KEYBOARD_PILOT_USER_IDS=
   ```

2. Restart the **current** binary. Do not roll the binary back: every build that
   predates v9 refuses to start against this database, so an incident rollback to
   Release A would fail startup rather than disable Phase 1. The flags above do
   the whole job on the current build.
3. Have each previously enabled user send a greeting or `/keyboard hide` and
   confirm the client keyboard disappears. Rollback is **not** accepted until
   every known user has confirmed this synchronization; no proactive Telegram
   message is implied.
4. Never restore an older DB merely to disable Phase 1 — Phase 1 adds no schema
   and accepted ledger rows must be preserved. Verify `/start`, `/diet`,
   `/recent`, reminders, schema `user_version = 10`, and `PRAGMA foreign_key_check`
   afterward.

### Binary rollback (when the code, not the config, is at fault)

Older binaries fail closed against newer schemas — `migration_preflight` raises
`UnsupportedSchemaError` when `user_version` exceeds what the build knows. That
is a safety property, not a bug: a v8 build has no idea what v9/v10 rows mean.
It also means **a binary rollback below the database's version is not available
without a database restore**, and a restore discards every row accepted since.

1. Prefer a fix-forward commit. It is almost always faster than a restore.
2. If you must go back, roll back to a build at or above `user_version = 10`.
   Confirm before restarting:

   ```powershell
   .\.venv\Scripts\python.exe -c "from ledger_schema import LATEST_SCHEMA_VERSION as v; print(v)"
   ```

   A number below the live `user_version` means that build will not start.
3. Only if no such build exists: restore the matching pre-migration backup from
   `BACKUP_DEST_DIR`, verify it first, and accept the data loss explicitly.

   ```powershell
   .\.venv\Scripts\python.exe -m scripts.backup_db --verify-only E:\ledger-backups\<file>.db
   ```

   Verification checks integrity, foreign keys, and the required tables **and
   columns** for the stamped version. It does not compare row counts against the
   live database — read the sanitized counts it prints and confirm they match
   what you expect to lose.

### Pilot expansion (enabling Release B fast logging)

1. Add one authorized user to `PHASE1_ENABLED_USER_IDS`, set
   `HOME_KEYBOARD_MODE=pilot`, add only that user to the pilot list, restart.
2. Smoke-test and observe sanitized errors/latency.
3. Add the second user to both lists, restart, complete acceptance.
4. Only then set `HOME_KEYBOARD_MODE=on`, clear `HOME_KEYBOARD_PILOT_USER_IDS`,
   restart, and verify both users.

`/keyboard hide` is momentary: the bar may reappear on the next eligible Home
response. There is still no durable per-user hide preference.

### Release B fast mutations

Enabling `PHASE1_ENABLED_USER_IDS` turns on the whole fast-logging surface.
These flags trigger no migration of their own. What to check when smoke-testing:

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
