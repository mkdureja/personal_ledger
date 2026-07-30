# Ledger implementation plan

**Status:** proposed active plan
**Prepared:** 2026-07-30
**Baseline:** `hardening/review-fixes` at `16d1f3a`
**Product boundary:** one private Telegram bot for the owner and spouse
**Canonical roadmap:** `implementation_plan.md`

## Goal

Turn the strong current build into an obvious, dependable household app:

- both users can complete normal daily tasks from visible buttons;
- every tap that immediately writes data looks like a write action;
- recovery, backup, and reminder behavior is honest;
- the current isolation and snapshot guarantees remain unchanged; and
- new features are selected from real household friction, not the old phase list.

This plan replaces the old phase roadmap that previously occupied
`implementation_plan.md`, and absorbs the reviewed Codex artifact that briefly
lived at `impl_plan_codex.md`. Both predecessors remain recoverable from Git at
`16d1f3a`. This file is the single roadmap; there is no second plan file to edit.
See [review_codex.md](review_codex.md) for the evidence and rationale, including
its second-pass addendum covering deployment safety, CI, and branch state.

## Current baseline

Updated 2026-07-30 after implementing Releases 0 and 1 and a follow-up Codex
review. The planning-time baseline (clean at `16d1f3a`, 892 tests) is history; see
the per-release status sections below for what each slice delivered.

- Releases 0 and 1 are implemented on `hardening/review-fixes`. **1036 tests
  pass**; compilation and installed dependency checks pass; GitHub CI is green on
  `windows-latest` and `ubuntu-latest`.
- The database is schema v8 with a clean integrity and foreign-key check. An
  external verified backup exists at `E:\ledger-backups`, and no database or
  backup file remains inside the repository.
- Two users are authorized and both are in the current keyboard `pilot` mode.
- Home, Quick Meal, exact Repeat, targeted Undo, current-value replay, editable
  meal drafts, suggestions, a curated catalog, atomic recipe duplication, the
  single-instance lock, and the pre-migration backup gate are implemented.
- **Still not accepted on both real Telegram clients**, and no polling process was
  running during either review. Live acceptance requires one supervised process.
- Neither ledger has a private food, recipe, or food preference yet. This blocks
  the Release 1 live gate's `⚡` case until the setup pre-step is done; it does not
  justify promoting a speculative feature or migration.
- **Open, and owner-owned:** the branch is 35+ commits ahead of `main` with no
  pull request, `main` has no branch protection, and the backup destination does
  not yet meet its own at-rest encryption policy (see
  [docs/backup_runbook.md](docs/backup_runbook.md)).

## Product rules for every slice

1. Logs and preferences remain private per user.
2. Completed nutrition stays a frozen snapshot.
3. Unknown values are shown as unknown, never guessed or converted to zero.
4. Immediate writes must be visually explicit and have a targeted recovery path.
5. Buttons are the everyday UI; commands remain shortcuts and recovery tools.
6. Schema changes are narrow, independently backed up, and justified by a real
   workflow that v8 cannot safely represent.
7. No generalized household/team platform is introduced.

## Cold-start constraint for acceptance

An `⚡` row requires a stored default in `user_food_preferences`, so the current
live data cannot exercise §1.2. Before the Release 1 live gate, each user creates
one private food and one complete usual amount using the current supported path.
Record whether that setup is genuinely awkward; that is evidence for Candidate
B, not a reason to promote it before the test.

Likewise, empty preference tables do not justify Candidate A. For a two-user
SQLite database, migrating zero rows instead of a small future row count is not a
material risk reduction. Schema work remains usage-driven and §0.5 must land
before any v9 work. The small current database makes backup and restore rehearsal
cheap, but does not make the data disposable.

## Release 0 — protect and align the baseline

This is a short safety/documentation pass before presenting the app as finished.

### 0.1 Make backup verification complete

Update `scripts/backup_db.py` and the runbooks:

- Add a dependency-free root module, `ledger_schema.py`, which imports only the
  Python standard library and publicly exposes `LATEST_SCHEMA_VERSION`,
  `is_known_schema_version(version)`, and `required_tables_for(version)`.
- Move the table-introduction mapping into that module. It covers every
  migration-added v2–v8 table:
  `mutation_receipts`, `user_settings`, `habit_activity_periods`,
  `reminder_deliveries`, `diet_log_items`, `user_food_preferences`,
  `catalog_foods`, `catalog_aliases`, and `catalog_portions`.
- Add a second dependency-free root module, `ledger_backup.py`, which owns the
  synchronous inspect, verify, and SQLite online-backup functions.
  `scripts/backup_db.py` becomes a thin CLI over that module, and the production
  migration preflight in §0.5 calls the same functions directly rather than
  spawning a subprocess.
- Make `bot/migrations.py::verify_current_schema()` and `ledger_backup.py` both
  call `required_tables_for(version)`. Migrations may retain `LATEST_VERSION` as
  a compatibility alias, but its value comes from `LATEST_SCHEMA_VERSION`.
  Neither root module imports `bot`, `aiosqlite`, Telegram code, or application
  configuration.
- Make `python -m scripts.backup_db` the documented invocation and add
  `scripts/__init__.py`. This lets the standalone standard-library script import
  the root schema contract without a path hack or application dependency.
- Every created copy must match its source's `user_version`, required tables,
  integrity, and foreign keys. On top of that shared rule, make the caller's
  intent explicit:
  1. routine creation requires `--expect-version latest`;
  2. migration preflight passes `--expect-version N`, where `N` is the source
     version it just read; and
  3. `--verify-only PATH` accepts any known stamped version from 1 through
     `LATEST_SCHEMA_VERSION`, with optional `--expect-version` when the operator
     wants to assert a particular restore target.
  Creation mode never silently omits `--expect-version`. This lets a v8 backup
  taken immediately before v9 migrate pass its exact-source contract without
  letting a routine v8 backup be certified as current by the v9 tool.
- Generic `--verify-only` rejects legacy version 0 and versions newer than this
  checkout with distinct messages. The populated-v0 production migration case
  has a separate, explicit contract in §0.5; it must not fall through this
  generic verifier or become impossible to migrate.
- Keep diagnostics to sanitized aggregate counts. Count every user-owned table
  required at the stamped version; shared catalog totals are optional
  diagnostics, not a substitute for schema verification.
- Require an explicit `--dest` and reject a destination that resolves inside the
  project root—the current implicit default incorrectly lands beside
  `ledger.db`;
- Add `--verify-only PATH` as the repeatable restore-verification interface;
  every newly created backup still runs verification automatically.
- Document and schedule a destination outside the repository.
- Use a small rolling retention policy.
- Restore one backup to a temporary path and verify that restored file with
  `--verify-only`.

Contract tests must prove that:

- the migration registry covers every integer from 1 through
  `LATEST_SCHEMA_VERSION`;
- startup and backup verification both use `required_tables_for(version)`;
- a complete v7 database passes `--verify-only` and pre-migration creation with
  `--expect-version 7`, but fails routine creation with
  `--expect-version latest` under the v8 tool;
- a backup whose version differs from its source fails the create-path check;
- a missing table required at the stamped version fails verification; and
- generic verification rejects version 0 and a future version for the correct
  distinct reasons.

Do not copy a live `ledger.db` by itself. Correct that instruction in
`docs/user_guide.html`. The exact creation method for the existing
`ledger.db.bak-20260729-095957` is unknown. Its name does not match the current
implicit default, although an explicit `--dest` could have produced any name.
It has since been verified read-only and is sound: schema v8,
`integrity_check=ok`, zero foreign-key violations, all nineteen v8 tables
present, and row counts identical to the live database. Treat it as a usable but
misplaced and soon-stale rollback point: create and verify the external
replacement first, then relocate or retire this file once a destination is
chosen.

One owner input blocks Release 0.1: choose the real destination and state whether
it is local-only or will be synced/moved off-host. Record one corresponding
security policy:

- a plaintext local backup may remain only on a verified device/disk-encrypted
  volume that is excluded from cloud sync; or
- any backup synced or moved off-host is encrypted at the file/archive layer
  before transfer, with its recovery key stored separately.

Never put a plaintext database backup in cloud storage. Do not prescribe 7-Zip,
`age`, or another archive dependency until the destination and key-recovery
method are chosen. The implementer must not infer this decision; the owner makes
it before the Release 0 gate.

### 0.2 Make the documentation tell one current story

- This planning pass has already corrected the safety-critical/current-fact
  mismatches: the 15-minute timeout, WAL-safe backup guidance, Telegram's role
  in message transport, `HOME_KEYBOARD_MODE=off` versus `remove`, and the current
  single-instance/pre-migration-backup limitations. README’s architecture tree
  now includes the current Home, receipts, suggestion, service, and helper-script
  surfaces.
- During implementation, refresh the HTML guide for Home, Meal/Repeat, receipts,
  usual amounts, suggestions, keyboard controls, and recipe duplication, and
  revise the README tree again only for modules that actually land. Describe only
  behavior present in the implemented build; do not pre-advertise planned flags
  or UI.
- Keep `implementation_plan.md` as the canonical active roadmap.
- The worktree currently proposes deleting `claude_response.md`,
  `docs/tap_first_nutrition_upgrade_proposal.md`, and `impl_plan_codex.md`.
  Those deletions are **not staged and not treated as approved in this planning
  pass**, because the last explicit cleanup instruction was not to delete yet.
  Before the documentation commit, the owner either confirms the removals or
  restores the files with visible superseded banners. In either case, only
  `implementation_plan.md` is active, and Git at `16d1f3a` preserves the old
  contents.
- **Commit `review_codex.md`.** It is currently untracked, which leaves the
  evidence record for this entire roadmap outside version control.
- After Releases 0 and 1 are implemented, update the same documents again for
  the newly shipped commands, configuration, and UI.

### 0.3 Align privacy claims with diagnostic output

- Remove raw chat/user IDs from reminder and Repeat error logs.
- Make the manual-send helper’s error output token-safe and replace personal
  names in tracked operational text with User A/User B.
- Change the optional catalog-seeding catch in `bot/main.py` from
  `BaseException` to `Exception`, so cancellation and shutdown signals propagate.
  A richer degraded-catalog status is optional later work.

### 0.4 Enforce the single-instance invariant

The Definition of Done asserts "exactly one supervised polling process," but
nothing enforces it. The `post_init` guard in `bot/main.py` (`if "db" in
application.bot_data`) only prevents duplicate initialization **inside one
`Application`**; it is per-process state and says nothing across processes.

Two instances on one host is an easy accident on Windows — a Scheduled Task plus
a manual `python -m bot`. Two concrete consequences:

- `context.user_data` is per-process in-memory, so a user's taps split across
  instances lose the guided draft. That is the same failure class `16d1f3a` was
  written to fix.
- `bot/handlers/reminders.py` reads `get_delivered_chunk_indices` once, then
  sends and records per chunk. Two instances both read "not delivered" and both
  send. The `ON CONFLICT` clause in `bot/database.py` protects the *record* after
  the send; it cannot prevent the duplicate send.

Contract:

- implement `bot/instance_lock.py` as a small context manager using
  `msvcrt.locking` on Windows and `fcntl.flock` on POSIX;
- use `<resolved DB_PATH>.instance.lock` as the stable lock path and add
  `*.instance.lock` to `.gitignore`;
- acquired **before** opening or migrating the database, and therefore before the
  §0.5 preflight;
- have `main()` own the open file handle for the entire `run_polling()` lifetime,
  with explicit unlock/close in `finally`; the harmless lock file itself may
  remain on disk because ownership is the OS lock, not file existence;
- a second process exits non-zero with a message naming the lock file and the
  likely cause;
- the OS releases the lock automatically after a crash — no stale-PID file to
  reap; and
- subprocess tests cover real contention, normal release, and crash release,
  rather than mocking the lock.

**Platform decision:** support both documented deployment paths. Exercise the
Windows branch on `windows-latest` and the POSIX branch on `ubuntu-latest` in
§0.6. Do not ship an untested branch in a safety mechanism.

Scope honestly: this prevents same-host concurrency. It does not address a
multi-host deployment, and it does not close the unavoidable send-then-record
crash window.

### 0.5 Gate migrations on a verified backup

`DatabaseManager.init_db()` calls `migrations.run_migrations()` unconditionally,
which applies every pending version immediately. The pre-migration checklist in
`docs/backup_runbook.md` is manual, and the operations runbook states there is no
forward compatibility — an older build cannot be run against a migrated
database. A bad committed migration is one of the few failures that requires a
database restore rather than an application retry or feature rollback.

Add `bot/migration_preflight.py::prepare_database_for_startup()`. `post_init`
calls it after `connect()` and before `init_db()`. It reads the source version,
uses `ledger_backup.py` directly, and returns only after the required backup is
verified. `run_migrations()` remains the low-level migration primitive used by
migration tests; it does not acquire operational paths or read deployment
configuration.

This deliberately enforces the invariant at the production executable boundary
without adding a bypass flag to every migration test. Any future executable
migration or administration entry point must call the same preflight function;
calling the low-level primitive directly is not an approved production path.

Behavior at startup:

1. take the §0.4 instance lock;
2. connect and read `PRAGMA user_version`;
3. if it already equals `LATEST_SCHEMA_VERSION`, proceed normally;
4. if it is newer than `LATEST_SCHEMA_VERSION`, abort with the existing
   `UnsupportedSchemaError`; do not create a backup or change the schema;
5. exempt a genuinely new database only when `user_version = 0` and none of the
   application tables in `required_tables_for(LATEST_SCHEMA_VERSION)` exists;
6. for a known version from 1 through `LATEST_SCHEMA_VERSION - 1`, create and
   verify an external backup from that source with the exact version just read,
   then migrate only after verification succeeds;
7. for a populated legacy version-0 database, create a source-matching online
   backup that passes integrity and foreign-key checks, then rehearse the full
   migration on a temporary copy of that backup. Touch the live database only
   if the rehearsal reaches and verifies `LATEST_SCHEMA_VERSION`; generic
   `--verify-only` continues to reject v0 as not schema-certifiable;
8. if no owner-selected external destination is configured, **refuse to start**,
   exit non-zero, and make no schema change; and
9. keep the original pre-migration backup at its source version; never migrate
   the rollback copy itself.

The configuration mechanism is `BACKUP_DEST_DIR` in `.env.example` and
`bot/config.py`, validated with the same containment rule as `--dest`: it must
resolve outside the project root. Unset is legal while the schema is current and
becomes a hard startup error only when a non-empty migration is pending. Pin it
to an empty value in `tests/conftest.py` so a developer's real `.env` cannot leak
into the suite. Document it in `docs/backup_runbook.md` alongside the destination
and security-policy decision from §0.1, so the owner makes one choice, once.

This is Release 0 groundwork and is **mandatory before Candidate A / v9**.

### 0.6 Add continuous integration

There is no `.github/` directory. 892 tests, 26 commits, and every verification
to date has been local. `tests/conftest.py` already pins every current validated
setting; §0.5 adds the corresponding empty pin for `BACKUP_DEST_DIR`, after which
the suite is CI-ready without deployment `.env` leakage.

- One workflow running `python -m pytest -q` on push and pull request.
- Use a two-runner matrix: `windows-latest` for the deployed lock branch and
  `ubuntu-latest` for the POSIX branch documented in the operations runbook.
- CI is only a warning until its result is **required** by branch protection on
  `main`. After both checks exist, the repository owner enables protection and
  requires both before merge; this external GitHub setting is an assigned owner
  action, not an implementation detail.

### Release 0 status — implemented 2026-07-30

All six slices are implemented on `hardening/review-fixes`. **960 tests pass.**
The owner settled the blocking input: destination `E:\ledger-backups`, local-only
on a separate physical disk, excluded from cloud sync, plaintext under host disk
encryption, rolling retention of 10; the three superseded documents were confirmed
for deletion.

The gate below was executed against the **real** database (every migration case on
an online-backup copy in a temporary directory; the live file only read):
**20/20 checks passed.** Recorded results:

- A new external backup at `E:\ledger-backups` verified at v8: `integrity_check =
  ok`, zero foreign-key violations, all v8 tables, sanitized row counts.
- That backup, copied to a temporary path, passed `--verify-only` — restore
  rehearsed, not assumed.
- The historical in-tree rollback point was verified, relocated to
  `E:\ledger-backups\ledger-legacy-manual-v8-*.db`, and confirmed. No database or
  backup file remains inside the repository. Because it is itself v8, the
  "rejected as non-current" half of this check is proven by a v7 source in
  `tests/test_backup_contract.py` and by the live v7 rehearsal below.
- An in-repository destination is refused (exit 2), including a path beside
  `ledger.db`.
- A second real `python -m bot` exited 1 naming the lock file, never reached
  polling, and left the live database byte-identical.
- A v7-stamped copy of the real database: refused with no destination
  (`user_version` unchanged), then produced a backup that verifies at v7 and is
  rejected as current, migrated to v8 with no user rows lost, and left the
  rollback copy still at v7.
- A populated legacy v0 copy was backed up at v0, rehearsed on a throwaway copy,
  and only then migrated; an empty new database started with no backup and no
  destination; a v9-stamped copy aborted with `UnsupportedSchemaError` creating no
  backup.

**Owner actions still open:** enable branch protection on `main` requiring both CI
jobs (an external GitHub setting), and confirm both matrix runners are green on the
pushed branch.

### Release 0 gate

- A new off-repository backup passes full v8 schema/integrity verification.
- A known historical rollback point passes version-aware `--verify-only`, while
  `--expect-version latest` rejects it as non-current.
- The new v8 backup restores successfully to a temporary database and that
  restored file passes `--verify-only`.
- The backup command rejects an in-repository destination, and the chosen
  backup-security policy is documented and exercised.
- A second `python -m bot` on the same host exits non-zero without touching the
  database; normal shutdown and forced process death both release the lock.
- With a pending migration and no configured backup destination, startup refuses
  and leaves `user_version` unchanged; with a destination configured, it creates
  and verifies the backup before migrating.
- A populated v0 legacy database is backed up and successfully rehearsed on a
  temporary copy before the live migration; a genuinely empty v0 database starts
  without creating a meaningless backup.
- A database newer than this checkout still fails with `UnsupportedSchemaError`
  without creating a backup or changing the schema.
- The full suite passes unchanged with the migration gate in place — no test
  needed an exemption flag.
- CI runs green on both matrix runners for a push and a pull request.
- User-facing docs contain no raw live-database copy instruction and describe
  current behavior.
- Logs used by these paths contain no raw Telegram ID or bot token.
- Targeted backup/logging/startup-cancellation/instance-lock tests and the full
  suite pass.

## Merge and branch hygiene

`hardening/review-fixes` is 26 commits ahead of `main`, `main` has no unique
commits, and the branch tip matches `origin/hardening/review-fixes`. The branch
now contains the entire product — Phases 2a through 6, tap-first, Home,
Release B — so its name no longer describes its contents, and an indefinitely
long-lived feature branch is the wrong home for the mainline.

Execute in this order:

1. Finalize and commit the documentation cleanup (§0.2). Include
   `review_codex.md`; include or restore the three currently unstaged deletions
   according to the owner's explicit cleanup decision.
2. Add and push CI (§0.6), then confirm both matrix jobs run successfully.
3. Open a pull request from `hardening/review-fixes` to `main`.
4. The repository owner enables branch protection on `main` and requires both CI
   jobs before merge.
5. Implement the instance lock (§0.4) and migration backup gate (§0.5) on that
   pull-request branch.
6. Run the full suite plus a startup smoke test on a copy of the real database;
   merge only after the pull request and both required checks are green.
7. Delete the merged `hardening/review-fixes` branch and start future work from
   short-lived branches. `origin/feature/routine-anchors`, which still points at
   the old `main`, can be retired separately with the owner's approval.

## Release 1 — make daily use obvious

No schema change.

### 1.1 Create one idle Home

While no guided flow is active, make `/start`, `/home`, `/menu`, and supported
greetings render the same core Home surface. `/start` may prepend a one-time
welcome, but it must still show the buttons immediately.

Before rendering a first-ever `/start`, preserve both current onboarding calls:
`ensure_user()` and `ensure_user_settings(default_enabled=False)`. A missing
settings row is already treated as reminder opt-out, but keeping explicit
initialization avoids changing onboarding or future settings behavior.

During Study, Gym, Diet, or Habit Setup, all of those entries must preserve the
draft and return the existing finish-or-cancel hint. They must not silently
replace or end a live flow.

Recommended Home actions:

```text
[🍽 Log meal]   [✅ Habits]
[📖 Study]      [🏋 Workout]
[🗒 Recent]     [📊 Analytics]
```

After the `/home` handler exists, register a small Telegram command menu during
application initialization:

- `/home` — Today and actions
- `/recent` — recent entries
- `/undo` — recover the latest supported entry
- `/help` — full reference

Keep all existing commands functional; the picker is only the short everyday
set.

### 1.2 Label instant writes honestly

Extend the ranked meal-choice view with default-quantity information:

- in **Quick Meal only**, a private food with a valid usual renders as
  `⚡ Banana · 1 medium`;
- a recipe renders as `⚡ Chicken curry (recipe) · 1 serving`, preserving the
  existing recipe distinction;
- tapping that Quick row may keep the current immediate-write behavior;
- in the guided Builder, the same source renders normally and continues to open
  amount selection;
- a source without a usual renders normally and opens amount selection; and
- a broken usual is shown as needing repair, never as an instant action.

Schema v8 does not permit catalog usual amounts, so catalog results cannot be
instant rows in this release. Do not add double source/consequence emoji solely
for a future catalog migration.

Decorate all choices from one batched `get_food_preferences(user_id)` result:

- reuse the map `_ranked_choices()` already obtains when suggestions are on;
- fetch it once for suggestions-off and search-result paths; and
- derive complete versus partial defaults from each row’s amount/unit pair.

Do not call `get_default_quantity()` and `has_partial_default()` per displayed
row. The keyboard receives already-decorated choices and performs no I/O.

Compose the final button label with one source-aware budget. Preserve every
fixed component: the `⚡ ` prefix, the ` (recipe)` source suffix when applicable,
and the ` · amount unit` quantity suffix. Allocate only the remaining characters
to the dynamic name and truncate only that name. For example:
`⚡ Chicken curr… (recipe) · 1 serving`.

Apply the distinction consistently to the initial picker, pagination, search
returns, and every re-render.

Rename the persistent action to `Repeat last meal`. Show a bounded last-meal
summary on Home when Repeat is available. Keep exact-copy semantics and the
existing targeted Undo receipt.

For at least one compatibility release, accept both the old `Repeat` text and the
new label. Update `HOME_ACTIONS`, active-flow interceptors, routing tests, and
keyboard-removal behavior so an old persistent keyboard cannot bypass or
misroute. Keep the `Meal` label unchanged in this slice.

Do not add confirmation dialogs to every fast action; clear labeling plus Undo
keeps the flow fast.

### 1.3 Fix false targets and exits

- For new habit checklists, collapse each habit row to one full-width button and
  give that button the existing date-bearing `habit_c_*` or `habit_u_*` action.
- Keep all existing `habit_noop_*` callbacks inert. An old checklist label may
  answer with a short “refresh this checklist” hint, but it must never start
  writing data. The date label and Habit Setup labels sharing that prefix remain
  no-ops.
- On zero catalog search results, render Back, Search again, and Type it instead.
- For unsupported idle text in either flag state, send one concise recovery
  reply carrying the Home inline menu, rather than silence or a separate
  multi-message Home refresh.
- Keep `/cancel` as the Release 1 recovery control and make sure every guided
  prompt exercised by the live gate visibly teaches it. A universal Cancel
  button remains follow-up work.
- Retire stale inline keyboards using the existing ownership/revision rules.

### Release 1 status — implemented 2026-07-30

All three slices are implemented; no schema change. **1028 tests pass.**

- §1.1 `/start`, `/home`, `/menu`, and supported greetings render one Home
  through `home.open_home`. `/start` prepends a one-time welcome (detected by an
  absent settings row) and preserves both onboarding calls. Every entry returns
  the finish-or-cancel hint during any guided flow. The action grid is
  `[🍽️ Log meal][✅ Habits] / [📖 Study][🏋️ Workout] / [🗒️ Recent][📊 Analytics]`,
  reusing the existing `menu_*` callbacks plus a new `menu_recent`. The command
  picker (`/home`, `/recent`, `/undo`, `/help`) is published only on a real run.
- §1.2 `suggestions.annotate_defaults` decorates choices from the one batched
  `get_food_preferences` read the ranking already needs — no per-row query, no
  keyboard I/O. `keyboards.choice_button_label` spends a source-aware budget on
  the name alone, so `⚡`, ` (recipe)`, and ` · amount unit` are never displaced.
  Quick-only; the Builder and catalog rows render plainly; a half-stored default
  renders `🛠 … — fix usual`. The bar is now `Repeat last meal`, with the legacy
  `Repeat` label accepted for one release, and Home names the meal Repeat would
  copy (read with the same `ORDER BY` as `repeat_last_meal`).
- §1.3 Each habit is one full-width toggle carrying the date-bearing action;
  `habit_noop_*` stays inert and an old checklist's habit label now answers with
  a refresh hint. Zero-result search offers Back / Search again / Type it
  instead, handled inside the `SEARCH` state and draft-preserving.

**Still open, and not implementable from here:** the Release 1 live gate below
requires both real Telegram clients, including its setup pre-step (each user
creates one food and one complete usual so an `⚡` row can render at all).

### Release 1 tests

- Real-dispatcher tests for `/start`, `/home`, `/menu`, greetings, unknown text,
  and the new Home callbacks, including every entry during every active flow.
- A first-ever `/start` creates the user/settings row with reminders off and
  still renders Home; command-menu registration contains only the declared
  everyday commands.
- A Quick instant-default row is visually distinct and logs once; the same
  source in Builder opens amount selection. Cover initial, paginated, search,
  and re-rendered pickers.
- Choice decoration performs one batched preference read per render path. Long
  food and recipe names cannot displace the consequence prefix, recipe marker,
  or visible amount/unit suffix.
- Repeat identifies the last meal and remains exact-copy/replay-safe; both old
  and new persistent labels route safely during the compatibility window.
- Tapping the habit label toggles only the acting user’s correct date/habit.
- A stale `habit_noop_*` label remains non-mutating, including date/setup labels.
- Zero-result catalog search can return to the picker or free text without
  losing the draft.
- Existing two-user isolation, stale-callback, and full-suite tests remain green.

### Release 1 live gate

**Setup step, required before the gate can run at all.** Both ledgers currently
hold 0 private foods, 0 recipes, and 0 preference rows, so `_ranked_choices()`
returns an empty list and no instant-usual row can render for anyone. Before the
gate, each user must create at least one food and set a usual on it — today that
means the `/food add …` grammar plus the `⚙️` default menu, which is exactly the
command-heavy path the review criticizes. Record how that setup felt; it is the
first real evidence for the Candidate B decision below.

Then, from both real Telegram clients, without consulting `/help`, verify:

1. open Home;
2. log one normal meal and one explicit instant-usual meal;
3. Repeat and targeted Undo;
4. build, edit, remove from, and save a multi-item meal;
5. check and uncheck a habit by tapping its name;
6. open Recent and Analytics; and
7. recover from an unknown message, zero-result search, a visibly prompted
   `/cancel`, and timeout.

After both users accept the behavior, change the deployed keyboard mode from
two-person `pilot` to the simpler steady-state `on` configuration and clear the
pilot list. Keep the rollback flags documented.

### Follow-up polish backlog

Only promote an item below when the two-client test shows that it causes real
friction:

- add Back/Home actions to secondary screens;
- add a visible Manage Habits action;
- add Skip for study notes and diet calories/macros;
- add Bodyweight for gym weight;
- make Cancel visible in every guided flow;
- render reminder/suggestion settings as idempotent buttons; and
- add Home after completed Study/Gym actions.

Reuse current keyboard and callback-validation patterns. Do not build a generic
UI framework for this backlog.

## Conditional maintenance — only before the relevant change

These are not a scheduled release. Take an item only when its triggering
condition is true.

### Before adding another nutrition input

Move the pure resolved-entry types and these functions out of
`bot/handlers/catalog.py`:

- `resolve_food_diet_entry`
- `resolve_recipe_diet_entry`
- `resolve_catalog_food_entry`
- their pure helpers

Place them in `bot/nutrition_resolution.py` (or a clearly named service module).
Both handlers and `DatabaseManager` import that module. It must have no Telegram
dependency and no database I/O.

Do not split every large file during this extraction. Later, when a diet section
is changed for product reasons, extract along the existing picker/draft,
quick/default, and replay/current-values seams.

### Before adding a script or other non-handler write path

Add the same simple bounds already enforced by handlers to public study, gym,
and legacy diet write methods:

- bounded non-empty text;
- positive bounded duration, sets, and reps;
- finite bounded weight, calories, and macros; and
- legal meal type.

This prevents a future script or alternate entry point from bypassing product
invariants. Preserve current exception and transaction behavior.

### If reminder delivery becomes relied on or shows failures

- put each reminder user behind an independent delivery-state failure boundary;
- add one bounded same-day retry or a short startup catch-up window; and
- document the unavoidable send-then-record crash window.

Do not add a queue. If catalog startup handling is later expanded, expose a
clear degraded-catalog status without swallowing cancellation or shutdown.

### Gate for any selected maintenance item

- The stated dependency or invariant is demonstrably improved.
- Existing behavior and two-user isolation remain unchanged.
- Focused tests and the full suite pass.

## Household trial — choose, do not accumulate

Use the accepted app for a short normal-use period. Keep a tiny friction log,
without building telemetry:

- missing foods searched for;
- repeated amount/setup work;
- meals typed free-form because taps were slower;
- commands the spouse had to ask about;
- reports actually opened; and
- requests to reuse a food/recipe across the two private ledgers.

Select **one** next slice by repeated frequency.

Do not promote Candidate B merely because the current ledgers are empty. The
Release 1 setup step deliberately creates the minimum saved-food state needed to
test the product. If both users find that supported setup materially obstructive,
record it and select Candidate B through the same evidence rule; otherwise keep
it conditional with the other candidates.

### Candidate A — catalog usuals and pins

Choose this when both users repeatedly log the same curated catalog items and
re-enter amounts.

Implement one migration, v9:

- rebuild `user_food_preferences` only to allow `source_type='catalog'`;
- preserve every existing row and primary key;
- validate catalog sources as active shared rows;
- allow per-user pin/hide/usual values for catalog items;
- keep preferences private even though the source definition is shared; and
- show a small curated starter set when a user has no history.

This does **not** require provider snapshots, revisions, online lookup, or a
catalog edit/audit framework.

### Candidate B — guided My Foods

Choose this when `/food add` and `/recipe ...` syntax is the repeated problem.

Add a bounded tap-and-text editor for:

- name;
- basis amount and unit;
- known calories/macros;
- common named portions; and
- archive/duplicate.

Use the existing v8 data model and database methods. Do not create a general
admin panel.

### Candidate C — deterministic typed meals

Choose this when users repeatedly type comma-separated meals instead of using
saved/catalog choices.

- Parse only quantities/units already supported locally.
- Match private exact names first, then known catalog aliases/search.
- Never invent nutrition or select an ambiguous food.
- Keep unresolved items in a visible draft with Search, Free-text snapshot, or
  Remove, plus optional guidance to the existing `/food add` command.
- Confirm the resolved draft before saving through the existing atomic
  meal/item path.

Keep this local and deterministic. No LLM, online provider, or schema migration
is needed for the first useful version. Do not silently bundle Candidate B’s
guided food editor into this slice.

## Explicitly deferred

Until repeated household use proves otherwise:

- combined v9–v12 provider/source infrastructure;
- USDA or Open Food Facts integrations;
- shared-catalog local revisions, override reconciliation, and import audits;
- Gemini/LLM parsing and consent infrastructure;
- voice transcription and worker supervision;
- supplement-specific tables and screens;
- shared couple totals, comparison dashboards, or automatic data visibility;
- roles, invitations, organizations, a web UI, cloud sync, microservices, and
  high-availability infrastructure.

Simple supplement adherence can remain a Habit. Food/recipe reuse between users
can remain separate until duplicate setup becomes frequent enough to justify one
explicit Copy to spouse action.

## Definition of done

The planned work is complete when:

- both users can discover and complete normal daily workflows from buttons;
- no identical-looking control sometimes previews and sometimes writes;
- every mutation remains owner-scoped, replay-safe, and recoverable as designed;
- the deployed bot runs as exactly one supervised polling process — **enforced by
  the §0.4 lock, not merely asserted**;
- except for creating a genuinely empty new database, no production startup
  migration can run without a freshly verified backup of the source it is about
  to change, and every future executable migration entry point is required to
  use the same preflight;
- a recent external backup has been successfully restored and verified;
- user and operations documentation match the deployed build;
- the mainline lives on `main`, with both Windows and Linux CI checks required
  before merge;
- ordinary `python -m pytest -q` continues to pass without `.env` leakage; and
- the next feature, if any, is one response to observed friction rather than a
  revived speculative roadmap.
