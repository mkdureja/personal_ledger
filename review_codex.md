# Ledger review — Codex

**Reviewed:** 2026-07-30
**Baseline:** `hardening/review-fixes` at `16d1f3a`
**Audience:** the owner and spouse; two private, independent ledgers

## Verdict

Ledger is already a strong private household bot. Its data isolation, replay
safety, migrations, nutrition snapshots, targeted Undo, and automated tests are
well beyond what a two-user app normally needs. The Telegram + single-process
SQLite architecture is the right size and should be kept.

The app is not yet as obvious as its internals are reliable. The main remaining
work is to make everyday actions visibly discoverable and predictable, complete
a real two-device acceptance pass, and make backup/documentation claims match
reality. The next release should be a focused polish release—not the planned
USDA/LLM/voice/supplements expansion.

There is no evidence of corrupt data or a current security breach. The v8 build
is suitable for a controlled pilot only while the operator guarantees one
process and no migration is pending. Before calling it release-ready, enforce
single-instance startup and require a verified pre-migration backup. I would
call the current build **functionally ready, operationally gated, and not yet
household-accepted**.

A second pass over the repository (Claude, same day) confirmed every finding
below and added four that this review did not cover — deployment concurrency,
migration safety, CI, and branch state — plus one useful cold-start observation.
Those are recorded in the *Second-pass addendum* near the end of this document
rather than edited into the sections above, so the provenance of each finding
stays clear.

## What was verified

| Check | Result |
|---|---|
| Git state | Baseline was clean at `16d1f3a` before these artifacts were written |
| Automated tests | **892 passed** in 93.81 seconds |
| Python compilation | `compileall` passed |
| Installed dependencies | `pip check` passed |
| Database | Schema v8; `integrity_check=ok`; zero foreign-key violations |
| Config shape | Two allowed users; both enabled in keyboard pilot mode |
| Runtime | No bot polling process was running during this review |

The database was inspected only for schema/integrity and sanitized aggregate
state. No private entry text or Telegram ID is reproduced here.

This was a repository and local-state review, not a live Telegram usability
test. A real-client pass by both users remains necessary.

## What is already very good

### 1. The privacy boundary is appropriate and well tested

Handlers require both an allowed Telegram user and a private chat
(`bot/handlers/common.py`). Reads, writes, callbacks, reminders, and destructive
actions are owner-scoped. `tests/test_two_user_isolation.py` exercises the
boundary at routing and database levels.

Keep the two ledgers independent. “Used by a couple” does not require shared
nutrition totals, roles, invitations, organizations, or an admin portal.

### 2. Mutations fail safely

The app has unusually thoughtful recovery behavior:

- Telegram update re-delivery does not duplicate supported mutations.
- Repeat copies the exact historical meal rather than silently recalculating it.
- Receipt Undo targets the displayed meal, not whichever meal happens to be
  newest later.
- “Log again at today’s values” shows changes and detects values drifting between
  preview and save.
- A timed-out meal draft now says exactly what was discarded and confirms that
  nothing was logged.

These contracts in `bot/database.py`, `bot/handlers/diet.py`, and
`bot/handlers/receipts.py` are worth preserving.

### 3. Nutrition history is honest

Completed meal items are snapshots. Missing calories/macros remain unknown
rather than becoming zero, and summaries disclose incomplete coverage. The
curated catalog is deliberately small and local. That is a sound starting point
for a private bot.

### 4. The test suite is a real asset

The 892 passing tests cover migrations, tenancy, callbacks, routing,
idempotency, reminders, nutrition resolution, stale UI, and failure paths.
Tests use the actual application dispatcher for important routing cases. The
suite is slow enough that a small fast pre-commit subset would be convenient,
but there is no reason to replace or radically reorganize it.

### 5. Operations are mostly proportionate

One polling process, one WAL-backed SQLite database, a small allowlist, opt-in
reminders, and an online backup script are enough for this use case. A web
frontend, cloud database, queue, microservices, or generic multi-tenant platform
would add support burden without helping either user.

## Priority findings

### P0 — Make side effects visible before a household rollout

In Quick Meal, a saved-food button looks the same whether it opens an amount
picker or immediately writes a meal. `food_choice_keyboard()` renders only the
item name, while `choose_food()` and `_try_quick_default()` instantly commit
when a saved “usual” exists. The guided Builder shares the keyboard but
deliberately does not perform the instant write.

That is fast, but not self-evident. A user should never have to remember which
ordinary-looking rows are write buttons.

Recommended contract:

- `⚡ Banana · 1 medium` means “tap logs this now.”
- `⚡ Chicken curry (recipe) · 1 serving` keeps recipe identity visible.
- `🥗 Paneer` means “tap to choose an amount.”
- `🔁 Repeat last meal` should use its full name, and Home should show what the
  last meal was before offering the action. During rollout, continue accepting
  the old `Repeat` text from stale persistent keyboards.
- Every instant write keeps the existing targeted Undo receipt.

Decorate a whole picker from one `get_food_preferences(user_id)` read; do not
query twice per row for complete and partial defaults. The suggestions-enabled
path already has this preference map and can reuse it. Search and
suggestions-disabled paths need at most one equivalent read. Compose the final
button label with space reserved for every fixed component: `⚡ `, the
` (recipe)` marker when applicable, and ` · amount unit`. Only the dynamic name
may be truncated, for example
`⚡ Chicken curr… (recipe) · 1 serving`.

Schema v8 does not permit catalog usual amounts, so a catalog item cannot be an
instant row in this release. Preserve the recipe suffix and settle one compact
label form; adding multiple source/consequence emojis is not required.

Evidence: `bot/keyboards.py::food_choice_keyboard`,
`bot/handlers/diet.py::choose_food`, and
`bot/handlers/diet.py::_try_quick_default`.

### P0 — Make Home the obvious entry point

`/start` sends a welcome paragraph and tells the user to use `/menu`; the richer
Home snapshot requires knowing to type `hi` or `home`. `/menu` then shows a
different, smaller surface. Telegram’s command menu is not registered by the
application.

For two non-technical users, there should be one mental model:

- While idle, `/start`, `/home`, `/menu`, and a greeting all open Home.
- During a guided flow, those entries preserve the draft and keep the existing
  finish-or-cancel response.
- A first-ever `/start` still calls `ensure_user()` and
  `ensure_user_settings(default_enabled=False)` before rendering Home. A missing
  settings row is already treated as reminder opt-out, but preserving explicit
  initialization keeps onboarding and future settings behavior consistent.
- Home shows Today plus the primary actions.
- The Telegram command picker contains only a small everyday set; `/help` keeps
  expert commands.
- Screens changed in this release keep a clear route back to Home. Retrofitting
  every secondary screen is follow-up work only if the two-client pass exposes
  real navigation friction.

Evidence: `bot/handlers/start.py`, `bot/handlers/home.py`,
`bot/keyboards.py::main_menu_keyboard`, and `bot/main.py`.

### P0 — Correct unsafe and stale documentation

At review time, the HTML guide said to copy the live `ledger.db` file to cloud
storage. In WAL mode, copying that file alone can omit current state. That
directly contradicted `docs/backup_runbook.md` and `scripts/backup_db.py`.

The same guide and README described a five-minute timeout, while the code uses
15 minutes. The guide also omits Home, Meal/Repeat, receipts, usual amounts,
keyboard controls, suggestions, and recipe duplication.

There was also a rollout wording mismatch: `.env.example` said `off` removes a
stale keyboard on Home, while `show_home()` intentionally sends no removal in
`off`; only `remove` forces synchronization. Tests encode the latter behavior.

**Status after this planning pass:** the unsafe live-file/cloud instruction, the
timeout mismatch, the misleading privacy claim, and the `off`/`remove` wording
are corrected in the current docs. README and the runbooks now also state the
present single-instance and pre-migration-backup gaps. The broader feature-guide
refresh remains part of plan §0.2 and must describe only behavior that has
actually shipped. Backup prose stays a release gate because an outdated
instruction there can cause data loss.

### P1 — Remove the remaining false targets and dead ends

- The Habits prompt says “Tap to check off,” but tapping the habit-name button
  is a no-op; only the smaller Done/Undo button changes it. New checklists should
  use one full-width habit button with the existing date-bearing check/uncheck
  action. Existing `habit_noop_*` callbacks must remain inert—a stale message
  must not acquire a write side effect—and may answer with a refresh hint. Date
  and setup labels sharing that prefix also remain no-ops.
- A catalog search with no results offers only another typed query or `/cancel`.
  Add Back and “Type it instead” buttons.
- Unknown text is ignored when fast logging is off but opens Home when it is on.
  Return one concise, consistent recovery reply carrying the Home inline menu,
  rather than silence or a separate multi-message Home refresh.

Evidence: `bot/handlers/habits.py::show_habits_checklist`,
`bot/keyboards.py::habit_checklist_keyboard`,
`bot/handlers/diet.py::receive_search_query`, and
`bot/handlers/home.py::home_text_router`.

### P1 — Make backups operational, not merely documented

The online backup mechanism is correct, but the observed backup is inside the
repository directory even though the runbook says backups belong outside it.
Its provenance is unknown: its `ledger.db.bak-*` name does not match the current
script default, `ledger-backup-<timestamp>.db`.

Independently, the current default is still wrong:
`_default_dest(source)` places a backup beside the source, which is inside this
repository for the normal `ledger.db` configuration. There is no evidence here
of a scheduled external backup or a restore rehearsal.

The verifier in `scripts/backup_db.py` checks SQLite integrity and foreign keys,
but its expected/count table list omits migration-added tables from v2 through
v8. A structurally incomplete v8 copy could still be described as healthy.

Right-sized fix:

- require an explicit destination and reject destinations inside the project
  root;
- create a dependency-free root `ledger_schema.py` exposing
  `LATEST_SCHEMA_VERSION`, `is_known_schema_version(version)`, and
  `required_tables_for(version)`;
- move the table-introduction mapping there, and make both
  `bot/migrations.py::verify_current_schema()` and the backup verifier call the
  public helper;
- extract the standard-library backup/verification engine to
  `ledger_backup.py`; keep `scripts/backup_db.py` as its thin CLI and let the
  production migration preflight call the same engine directly;
- document `python -m scripts.backup_db` as the canonical invocation so the
  standard-library-only script can import the root contract cleanly;
- create a fresh verified external backup before relocating or retiring the
  existing in-repository file;
- schedule the same off-repository command;
- retain a small rolling set;
- always require a created copy to match its source version and schema;
- require routine creation to expect `latest`, require migration preflight to
  expect the exact source version it just read, and let `--verify-only` accept
  known versions 1 through the current version;
- reject legacy version 0 in generic verification while handling a populated v0
  production upgrade through the explicit backup-and-rehearsal contract in plan
  §0.5;
- include all user-owned tables required at the stamped version in sanitized
  counts; and
- add `--verify-only PATH`, then restore one backup to a temporary path and run
  that same verifier. This flag makes restore rehearsal repeatable; it does not
  replace or strengthen normal creation-time verification, because every new
  backup is already verified during creation.

This distinction lets a legitimate v7 rollback point pass historical or exact
pre-migration verification without allowing it to satisfy a routine
`--expect-version latest` backup under the v8 tool.

One owner decision blocks this gate: name the actual destination and whether it
will ever be synced or moved off-host. The runbook then records the matching
security policy:

- a plaintext local backup may remain only on a verified device/disk-encrypted
  volume that is excluded from cloud sync; or
- anything synced or moved off-host is encrypted at the file/archive layer
  before it leaves the machine, with its recovery key stored separately.

Do not place a plaintext backup in cloud storage. No high-availability or cloud
database project is needed, and the plan need not prescribe a specific archive
tool before the destination is chosen.

### P1 — Align diagnostics and startup cancellation

Some reminder and Repeat error logs include raw Telegram IDs even though the
operations prose promises sanitized diagnostics. Log category/count only and
sanitize the manual-send helper’s exception output.

Catalog seeding catches and swallows `BaseException`, including cancellation
and shutdown signals. Change that catch to `Exception` in the baseline safety
release. A richer degraded-catalog status can wait until this path otherwise
needs work.

### P1 — Replace the roadmap, not the architecture

At review time, the planning documents disagreed with the current repository:

- the former `implementation_plan.md` pointed at `ee36415`, said 866 tests
  passed, and called a combined v9–v12 provider overhaul “NEXT”;
- the older `impl_plan_codex.md` claimed HEAD was `6909fc9`, the tree dirty, test
  environment isolation unfinished, the timeout undecided, and recipe duplication
  future work — all of which were already false;
- `docs/tap_first_nutrition_upgrade_proposal.md` described pre-v6 behavior that
  had already been replaced; and
- `claude_response.md` preserved old decisions about Gemini, voice, and a
  shared-editable provider catalog that should not be presumed active work.

**Canonical-roadmap issue resolved; cleanup decision pending.**
`implementation_plan.md` now holds the one active roadmap. The worktree currently
shows the three superseded documents as unstaged deletions, but this planning pass
does not treat those deletions as approved because the last explicit cleanup
instruction was not to delete yet. Before the documentation commit, the owner
either confirms removal or restores them with superseded banners. Git preserves
all three at `16d1f3a` either way. Plan §0.2 also requires committing this review
so the evidence record is itself under version control.

### P2 — Reduce the remaining command dependence only where observed

The main menu exposes the four logging areas and Analytics, but Recent, Settings,
reminder controls, and habit setup require remembered commands. Study notes,
bodyweight gym entries, and unknown diet values ask users to type `/skip`.
Settings displays command syntax instead of controls.

Release 1 already addresses the highest-value discovery problems: one idle Home,
a short Telegram command picker, honest write labels, false habit targets, and
dead-end recovery. Do not bundle every possible control into that release.

After the two-client pass, promote only the controls that caused real friction:

- Back/Home actions on secondary screens;
- inline Skip, Bodyweight, Cancel, and Manage Habits buttons where relevant;
- idempotent reminder/suggestion toggles in Settings; and
- a consistent Home action after completion.

Slash commands remain useful power-user and recovery shortcuts. Any promoted
item can use the existing keyboard/callback patterns without a new framework or
schema.

### P2 — Pay down only the technical debt that affects the next change

`bot/handlers/diet.py` and `bot/database.py` are each over 3,000 lines. More
importantly, `DatabaseManager._resolve_quantity_locked()` lazily imports pure
nutrition functions from the Telegram handler layer. That dependency direction
will make the next nutrition input harder to change safely.

First extract the pure resolution types/functions to
`bot/nutrition_resolution.py` (or a service module) and keep behavior identical.
Split the large modules only along real feature seams when those areas are next
touched. Do not launch a repository/framework rewrite based on line count alone.

Also add bounded validation to the public study/gym/diet database write methods;
today most invariants live only in handlers. That is adequate for current
Telegram paths but fragile for future scripts or entry points.

### P2 — Keep reminder durability claims precise

Reminder chunks resume if the same dated job runs again, but there is no
automatic same-day retry job; “next scheduled run” usually means a new date. If
reminders become relied on or show failures, add one bounded same-day retry or
startup grace-window catch-up, and isolate each user’s delivery-state failure so
one cannot stop the other.

This is a small reliability correction, not a reason to add a queue or
monitoring platform.

## Second-pass addendum

**Added by:** Claude, 2026-07-30, same baseline `16d1f3a`
**Status:** all findings above independently re-verified against the code; the
items below are additions, not corrections.

### What the second pass verified

| Check | Result |
|---|---|
| Automated tests | **892 passed** in 87.27s, exit 0 — confirms the headline figure |
| Every P0/P1/P2 finding above | Confirmed at the cited file and line |
| v2–v8 table list used in §0.1 | Exactly matches `_TABLE_INTRODUCED` in `bot/migrations.py` — nine tables, complete, no extras |
| Existing in-repo backup | Sound: v8, `integrity_check=ok`, 0 FK violations, all 19 tables, row counts identical to live |
| Code hygiene | Zero `TODO`/`FIXME`/`HACK` markers across `bot/`, `scripts/`, `tests/`; runtime dependencies pinned to exact versions |

### A1 — Nothing enforces the single-instance invariant

The Definition of Done asserts one polling process; no code enforces it. The
`post_init` guard in `bot/main.py` is per-process `Application` state and does
not span processes. Two instances on one host split in-memory conversation state
(losing guided drafts) and duplicate reminders, because
`bot/handlers/reminders.py` reads delivery state, then sends, then records — the
`ON CONFLICT` clause in `bot/database.py` protects the record after the send, not
the send itself. Plan §0.4 now fixes the lock lifecycle explicitly and exercises
both the Windows and POSIX implementations in CI.

### A2 — Migrations run unconditionally with no automated pre-migration backup

`DatabaseManager.init_db()` calls `run_migrations()`, which applies every pending
version immediately. The pre-migration checklist is manual, and the operations
runbook states an older build cannot run against a migrated database. A bad
committed migration is one of the few failures that requires a database restore
rather than a retry or feature rollback.

The operational gate belongs at the production application boundary, while the
low-level migration primitive remains directly testable. Plan §0.5 now names a
shared preflight function, binds it to the same dependency-free backup engine as
the CLI, requires future executable migration paths to use it, and handles both
empty and populated version-0 databases without weakening generic verification.

### A3 — No continuous integration

No `.github/` directory. `tests/conftest.py` already pins every validated
current setting, so the suite is CI-ready. The planned `BACKUP_DEST_DIR` must be
pinned empty there too. CI is only a warning until its result is required by
branch protection. Plan §0.6 chooses Windows and Linux matrix jobs and assigns
enabling both required checks to the repository owner.

### A4 — The mainline lives on a stale feature branch

`hardening/review-fixes` is 26 commits ahead of `main`; `main` has no unique
commits; the tip matches origin. The branch holds the entire product, so its name
no longer describes its contents. The committed tip matches origin, but the
working tree contains documentation edits and unstaged deletions. Plan *Merge
and branch hygiene* now chooses a pull request, required CI, and an explicit
documentation-cleanup decision before merge.

### A5 — Current data cannot exercise the instant-usual path yet

Sanitized counts: 2 users, 6 diet logs, 1 structured item, 0 study, 0 gym,
1 habit check, 6 habits, 16 catalog foods, and **0 private foods, 0 recipes,
0 preference rows**. Phase 1 is enabled for both users in `pilot` mode.

This does not weaken the P0 write-visibility finding — it is a real latent hazard
and cheap to fix — but it does mean the `⚡` row cannot currently render for
anyone and the Release 1 live gate needs a setup step. It does **not** justify
promoting Candidate A or B before evidence: a small future row count does not
materially change SQLite migration risk, and the existing setup path is adequate
for a two-user acceptance test. Addressed as plan *Cold-start constraint for
acceptance*.

## Cold-start observation

The reusable-food workflow is still command-heavy. Creating a personal food or
recipe requires grammars such as `/food add ...` and `/recipe ingredient ...`.
Catalog foods cannot currently have a per-user usual amount or pin because the
v7 preference table accepts only private foods and recipes.

Do not solve this with the old v9–v12 provider design. After the clarity release,
observe both users for a short period and choose one:

1. a guided My Foods editor;
2. one narrow migration allowing per-user catalog pins/usual amounts; or
3. deterministic typed-meal parsing using existing local sources.

Build only the option that removes repeated real friction.

**Qualified by A5.** The live gate must first create the minimum saved-food state
needed to render an instant-usual row. Record that setup experience as part of
the trial, but keep all three follow-up options conditional until the two users
actually demonstrate repeated friction.

## Target household experience

```text
Today — Jul 30
Meals 2 · 1,450 kcal
Study 45 min · Gym 4 exercises
Habits 3/5

[🍽 Log meal]   [✅ Habits]
[📖 Study]      [🏋 Workout]
[🗒 Recent]     [📊 Analytics]
```

If the persistent keyboard remains:

```text
[🍽 Meal] [🔁 Repeat last meal]
```

And the meal picker should expose consequences:

```text
[⚡ Banana · 1 medium]   <- logs immediately
[🥗 Paneer]             <- opens amount selection
[🔎 Search] [✍ Type it]
```

## Scope recommendation

Keep:

- Telegram as the UI;
- two explicit allowlisted users;
- independent data and preferences;
- one supervised process and SQLite;
- Study, Gym, Diet, Habits, simple trends, and opt-in reminders;
- local deterministic nutrition and frozen completed-log snapshots.

Defer until repeated household use proves a need:

- USDA/Open Food Facts integrations and provider revision infrastructure;
- LLM parsing and consent machinery;
- voice transcription workers;
- supplement-specific tables;
- household comparison dashboards or automatic sharing;
- roles, invitations, organizations, cloud sync, a web UI, and microservices.

The app will feel “really good” when both users can find every normal daily
action, understand every tap that writes data, recover from mistakes, and trust
that a restorable backup exists. It does not need more feature categories to
reach that bar. The proposed sequence is in
[implementation_plan.md](implementation_plan.md).
