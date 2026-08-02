# Response — Codex project review (second review)

**Reviewed:** 2026-08-02
**Baseline:** `hardening/review-fixes` at `fa30553` (46 commits ahead of `main`)
**Responder:** Claude, same day
**Suite before:** 1281 passed · **after:** 1316 passed, 0 skipped

## Verdict on the review

Accurate. Every code-level claim was checked against the source and reproduced
before anything was changed; **none was wrong**, and one was understated (see
finding 2). Two items are fairly reported but were already recorded as owner
actions in the repository itself rather than being undiscovered defects — noted
inline so the distinction stays visible.

The four typed-meal defects were reproduced with a purpose-built probe before
the fix and re-run after. The schema-manifest gap was reproduced exactly as
described: a v10 database stripped of both AI-consent columns passed
`verify_current_schema`.

## What changed

| # | Finding | Verdict | Action |
|---|---|---|---|
| 1 | Host permissions, BitLocker off | Confirmed, **owner action** | Not actionable from the repository. See "Left to the owner" |
| 2 | Backups incomplete / weakly verified | Confirmed, **worse than stated** | v10 backup created and verified; verification now checks columns |
| 3 | Startup safeguards (3 gaps) | Confirmed ×3 | All three closed |
| 4 | Rollback runbook targets v8 | Confirmed | Runbook retargeted; a real binary-rollback section added |
| 5 | Four typed-meal defects | Confirmed ×4 | All four fixed, each with a regression test |
| 6 | Voice can block both users | Confirmed | Bounded in time; model preloaded; size cap added |
| 7 | Reminder restart recovery overstated | Confirmed | Catch-up implemented; README corrected |
| 8 | Release/CI controls lag | Confirmed | Test gaps closed; PR and branch protection remain owner actions |

### 2 — Backups

The review said backups predate three accepted meal records. It is worse than
that: **all three backups on `E:` were stamped v8 while the live database is
v10**, so there was no rollback point at the current schema at all.

- Created and verified `E:\ledger-backups\ledger-v10-20260802-150734Z.db`.
- `_shape_problems` in `ledger_backup.py` now checks **columns** as well as
  tables, against the same manifest the bot enforces at startup. The review's
  own counter-example — a correctly stamped database missing required columns —
  is now refused by both verifiers, and there is a test asserting exactly that.

Row-count parity between source and destination and a cryptographic manifest
were **not** added. For a two-user household ledger, the cost outweighs the
benefit, and the runbook now states plainly what verification does and does not
prove rather than implying more.

### 3 — Startup safeguards

- **Column manifest.** Moved out of `bot/migrations.py` into `ledger_schema.py`
  and completed through v10. It is now keyed by *groups per version*, which is
  what makes a column-only migration verifiable at all — v10 adds no table, so a
  table list could never have detected it. `required_columns_for(9)` correctly
  does not demand v10's columns, so an older rollback point still verifies.
- **Unknown database.** A version-0 database holding no Ledger table but *other*
  tables is now refused instead of being treated as new. A genuinely empty file
  still creates its schema without a backup.
- **`:memory:`.** Rejected at `bot/main.py::main()` — the production entrypoint —
  rather than in `config`, which the test suite imports.

### 5 — Typed meals

- **Model assist no longer drops items.** The leftovers went to the parser as one
  joined string, so anything the model did not mention vanished from "Not
  logged". An original is now surrendered only when the model visibly spoke
  about it (shared significant token); anything unclaimed is carried through
  with the reason describing what the user actually typed. Matching is biased
  toward *not* claiming, because an unclaimed item is merely listed again while a
  wrongly claimed one disappears without trace.
- **Truncation is announced.** `parse_meal` returns a `MealTextParse` carrying
  how many items were dropped past the cap and how many were shortened; the
  confirm screen shows it. `parse_meal_text` is kept as a segments-only wrapper
  so existing callers are unaffected.
- **Cancel is token-strict**, exactly as Save already was.
- **`2 eggs` resolves.** This needed two fixes, not one. The bare count `2` was
  rejected for having no unit — now a definition that is *already counted*
  (`piece`, `serving`) supplies its own; a per-100g food still refuses, because
  "2" of it is far more likely a half-typed amount than two grams. Separately,
  the catalog stores `Egg` while people type `eggs`, and the substring search
  finds the plural from the singular but never the reverse — so an unresolved
  name now gets one naive singular retry. The advertised
  `/describe 2 eggs, 100g oats, coffee` now resolves eggs and oats; `coffee` is
  correctly reported as unknown, because it genuinely is not in the catalog.

### 6 — Voice

Not converted to a full job queue — disproportionate for two users. Instead the
hang is made impossible to sit through:

- every stage (lock wait, model load, decode) runs under a wall-clock ceiling;
- a timed-out load is **latched**, so the next note fails fast instead of
  queueing behind a thread that cannot be cancelled;
- the model is preloaded during startup (`VOICE_PRELOAD`, default on), so the
  first note no longer pays a ~21 s stall;
- a file-size cap complements the duration cap, since duration is only what
  Telegram *declares*.

A test asserts the property that actually matters: the event loop keeps ticking
while a decoder is wedged.

### 7 — Reminders

`run_daily` only ever schedules the *next* occurrence, so a process that was down
at the scheduled minute skipped that day entirely — the durable per-chunk state
was never consulted because the job never ran. Startup now replays a job whose
slot has passed and which recorded nothing today, once, after 30 seconds. It is
safe by two independent mechanisms: the "did it run" check, and the existing
chunk idempotency underneath. README no longer claims more than the code does.

### 8 — Tests

- The WAL test accepted `memory`, which `:memory:` always returns — so it could
  not fail. A second test now asserts WAL on a real file, where the contract
  applies.
- Rather than pin `REMINDER_HOUR` and wait for the next setting to leak,
  `LEDGER_SKIP_DOTENV=1` makes the suite read **no** deployment `.env` at all.
  That closes the class instead of one instance.

## Deliberately not done

- **Row-count parity / cryptographic manifest for backups** — see finding 2.
- **A bounded worker queue for voice** — timeouts and preload address the
  outage risk; a queue adds machinery two users will not exercise.
- **`get_macros.py`** — untracked, not mine, and the owner's scratch script. Its
  fallback query does lack a `user_id` predicate; flagged, not edited.
- **`.claude/settings.json` `Bash(python -)`** — flagged. Narrowing it is the
  owner's call, and it only matters if untrusted repository content is opened.
- **Splitting `database.py` and the diet handler** — real debt, but a
  3,000-line refactor on an unmerged 46-commit branch trades a known-good state
  for churn. Better done after merge.

## Left to the owner

None of these can be done from the repository:

1. **ACLs on the project directory, `.env`, the database, and `E:\ledger-backups`;
   BitLocker on both drives; credential rotation afterwards.** This is the
   highest-severity finding and it is entirely host-side.
2. **A scheduled, monitored backup task, and an off-host copy.** The current
   destination is a separate physical disk but the same machine, so it does not
   survive machine loss.
3. **Open the PR and enable branch protection requiring both CI jobs.** The
   workflow file already documents that CI is advisory until this is done —
   `.github/workflows/tests.yml` says so in its header comment. It is a GitHub
   settings action, not a repository change.
4. **Start the bot.** No process was running at review time. If that is
   intentional, it is deployment status, not a defect.

## Verification

- Live database re-verified against the new stricter manifest **on an
  online-backup copy**: `user_version=10`, `integrity_check=ok`, 0 FK
  violations, every required column present, preflight `current`, startup
  verifier OK. The stricter check does not refuse the real database.
- Both existing v8 backups still verify at their own version — the manifest is
  version-aware, not a blanket upgrade.
- Full suite: **1316 passed, 0 skipped.**
- Three pre-existing tests asserted an exact job list and would have started
  failing after the reminder hour once catch-up landed — the same
  frozen-literal/real-clock trap recorded in `implementation_plan.md`. They now
  pin a clock seam (`main._local_now`) instead of accommodating the extra job.
