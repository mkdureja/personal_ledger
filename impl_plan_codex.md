# Ledger: lean implementation plan

**Purpose:** a private Telegram ledger for a family to log meals quickly and
inspect a few useful trends. It is not a multi-tenant nutrition platform.

**Decision:** stop the old Phase 2 plan. Do not ship v9-v12 together, and do not
build immutable shared-catalog revision infrastructure unless a real usage problem
later requires it.

## Current position (verified 2026-07-30)

- `hardening/review-fixes` is at `6909fc9`, matching
  `origin/hardening/review-fixes`.
- Phases 0, 1a, and 1b are committed and pushed. The database schema remains v8.
- The committed baseline passes when the test process is isolated from deployment
  rollout values: **866 passed**.
- Release B has not had a live Telegram acceptance test and the bot is not running.
  Therefore it is code-complete, not yet operationally accepted.
- The working tree is not currently clean. While this review was in progress,
  uncommitted work appeared across configuration, database, handlers, keyboards,
  and test setup. It includes a 300-to-900-second conversation timeout, test
  environment isolation, and recipe-duplicate/UI work. Treat it as active work and
  review/test/commit it separately from this plan.
- Now that rollout IDs are populated in `.env`, plain `pytest` is not isolated:
  `tests/conftest.py` replaces `ALLOWED_USER_IDS` with its test ID but leaves the
  Phase 1 and keyboard pilot lists loaded from `.env`, causing collection to fail.
  This is test configuration leakage, not a product failure.
- The resolver layering debt is real: `bot/database.py` lazy-imports pure nutrition
  functions from `bot/handlers/catalog.py`.

This supports Claude's main assessment, with two qualifications: the tree is no
longer clean, and "delivered" should mean live-tested before Release B is called
finished.

## What to do next

### 1. Make Release B operable and test it live

This is the only immediate priority.

1. Finish the in-progress `tests/conftest.py` isolation change so it sets all
   rollout-related variables to internally consistent test values before importing
   `bot`. Run the ordinary test command without special shell overrides.
2. Decide whether the 15-minute draft timeout is wanted. If yes, add/adjust its
   focused test and commit it; otherwise restore 300 seconds. Do not leave this as
   an unrelated working-tree change.
3. Run startup/config validation, back up `ledger.db`, and start exactly one polling
   process.
4. Pilot with one family member first. Check:
   - Home keyboard and `/diet`;
   - logging a saved food with its usual amount;
   - multi-item draft, edit, remove, and pagination;
   - exact Repeat;
   - targeted Undo after an intervening log;
   - current-value preview and save;
   - catalog search;
   - `/recent`, `/summary week`, and `/chart diet`;
   - `/cancel`, timeout, and restart behavior.
5. Fix only defects found in the pilot, then enable the second user. After both
   users have used it normally, mark Release B accepted.

**Gate:** one bot process, clean startup, ordinary `pytest` green, and both users can
log/repeat/undo from real Telegram clients without confusing or lost entries.

### 2. Extract nutrition resolution

Do the small layering refactor before adding another input path:

- move the pure resolved-entry type and food/recipe/catalog resolver functions into
  `bot/nutrition_resolution.py`;
- make both database and handlers import that module;
- keep Telegram objects and database I/O out of it;
- preserve behavior and schema v8.

**Gate:** no circular/lazy handler import from `bot/database.py`, existing resolver
and integration tests unchanged or stronger, full suite green.

### 3. Use the bot before choosing more features

Run the accepted Release B for a short real-use period and keep a tiny friction list:

- foods searched for but missing;
- meals still awkward to enter;
- analysis actually consulted;
- repeated requests for voice, supplements, or recipe copying.

Choose the next item by frequency of family friction, not by its phase number.

## Likely next feature: simple typed meals on schema v8

If typing remains the main pain, add a deterministic `Describe meal` flow without
USDA, an LLM, voice, or migrations.

- Accept a bounded string such as `2 eggs, 2 toast, 200 ml milk`.
- Split only on conservative separators and parse quantities/units already supported
  by the local resolver.
- Match in this order: the user's foods and recipes, previously used curated catalog
  foods, then explicit catalog search.
- Show the resolved draft before saving. Ambiguous and unknown items are never
  guessed.
- For an unknown item, offer only:
  - search the existing catalog;
  - add a reusable personal food using the existing flow;
  - enter calories/macros for this meal as an existing `freetext` snapshot;
  - remove the item.
- Save through the existing atomic meal/item and mutation-receipt path.

This supplies most of the convenience promised by old Phase 3 while retaining v8
and deterministic nutrition.

**Gate:** common family meals take one message plus confirmation; every nutrient
equals the existing manual resolver result; unknown input cannot silently create or
misidentify food.

## Optional later additions

Build these only when usage justifies them, one small release at a time.

### Online food lookup

Add USDA lookup only if the local/personal catalog is missing foods often enough to
be annoying.

- Lookup occurs only after the user explicitly taps an online-search action that
  explains the food query leaves the bot.
- Show a small result list; validate the selected nutrients locally.
- First support `log once` as a frozen `freetext` item and `save as my food` using
  the existing personal-food model.
- Use the existing `source_provider` and `source_revision` fields on completed log
  items where applicable.
- Keep the shared bundled catalog owner-controlled. Do not let online searches
  silently mutate it.

No new schema is required for this first useful cut. If a later requirement truly
needs personal-food provenance or durable consent, add one narrowly scoped migration
for that requirement—not four speculative rebuilds.

### Analysis

The bot already has weekly summaries and diet charts. Improve them only after enough
real data exists to identify a useful question. Prefer one compact family-useful view
(for example seven-day calorie and protein averages with missing-data labeling) over
a general analytics framework.

### Recipe variants

Implement atomic `Duplicate recipe` when someone asks for it. It is small,
independent, and needs no lineage model: copy under a new unique name, then edit.

### Supplements

First try the existing habit/routine tracking if the need is simply “did I take it
today?”. Add supplement-specific tables only if dose schedules, pauses, or dedicated
analysis are genuinely needed.

### LLM parsing and voice

Defer both. Add an LLM only if deterministic typed input repeatedly fails on real
phrasing, and voice only if the family will regularly use it. Each brings dependency,
privacy, failure-mode, and support cost that is unjustified before demonstrated use.

## Explicitly removed from the active roadmap

- the combined v9-v12 cutover;
- immutable provider snapshot/head/revision/import-run machinery;
- shared-catalog override reconciliation;
- provider-wide retirement semantics;
- Open Food Facts as a second adapter;
- a general external-parser adapter framework;
- voice worker supervision and benchmark infrastructure;
- v13 supplements by default.

These ideas remain available if requirements change. They are not prerequisites for a
reliable family ledger.

## Definition of done

For each future slice:

- it solves an observed family workflow problem;
- it is usable from a real Telegram client;
- nutrition and ownership remain deterministic and user-scoped;
- completed logs remain frozen snapshots;
- ordinary tests pass without depending on `.env`;
- operational docs mention only controls that actually exist;
- the schema changes only when the feature cannot be represented safely in v8.
