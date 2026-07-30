# Faster Logging + Nutrition Sourcing - Final Implementation Plan

**Status:** Phases 0, 1a, and 1b built and merged into `hardening/review-fixes`;
Phase 2 onward not started  
**Project:** Ledger Telegram bot  
**Plan date:** 2026-07-28 (build status updated 2026-07-30)  
**Code baseline:** `hardening/review-fixes` at `ee36415`  
**Current database version:** v8 (unchanged — Phases 0/1a/1b add no migration)  

## 0. Build status

Delivery follows the reordered sequence in §12. Phases 1a/1b were executed against a
separate unit-level contract (`impl_plan_gemini.md`, units 0, A1-A3, B1-B5) that has
since been deleted now that the surface is built. It is preserved in git history — the
last commit containing it is `78a7e26`, so `git show 78a7e26:impl_plan_gemini.md`
recovers the original unit wording if it is ever needed. Everything still relevant from
it is recorded below.

| Phase | State | Evidence |
|---|---|---|
| 0 - harden v8 baseline | **Done** | `tests/test_phase0_hardening.py`, `test_phase0_structured_history.py` |
| 1a - Home and routing (dark) | **Done** | `tests/test_phase1_routing.py`, `test_home.py`, `test_release_a.py` |
| 1b - fast local mutations | **Done** | `tests/test_repeat_undo.py`, `test_quick_defaults.py`, `test_current_values.py`, `test_picker_and_draft.py` |
| 2 - source schema and USDA lookup | Not started | needs migrations v9-v12 |
| 3 - deterministic Describe | Not started | — |
| 4 - optional external parser (Gemini) | Not started | first phase with an external dependency |
| 5 - local voice | Not started | — |
| 6 - recipe variants | **Done** | `tests/test_polish_and_variants.py` (`/recipe duplicate`) |
| 7 - Supplements | Not started | independent track, needs v13 |

Suite: 866 tests green. Phase 1 ships dark (`PHASE1_ENABLED_USER_IDS=` empty); see
`docs/operations_runbook.md` for the rollout and rollback procedure.

Phase 1b's tests were named differently from the unit contract, with equal or wider
coverage: `test_database_repeat.py` → `tests/test_repeat_undo.py`,
`test_diet_defaults.py` → `tests/test_quick_defaults.py`,
`test_suggestion_pagination.py` → `tests/test_picker_and_draft.py`.

### 0.1 Accepted deviations from this plan

These were deliberate proportionality decisions for a two-user bot, taken by the owner
during the 1b build. They are recorded here so a later reader does not mistake them for
oversights.

1. **Current-value drift detection is a field signature, not a SHA-256 canonical-JSON
   digest** (§10.5 of the unit contract). `bot/services/current_values.py` builds a
   readable delimited string over exactly the fields that would be persisted; the commit
   re-derives the preview inside its own transaction and compares. Same guarantee — a
   meal that no longer matches what the user saw cannot be saved — without the canonical
   JSON/decimal-normalization machinery. Revisit only if a second writer process appears.
2. **No p95 latency benchmark gate** for Phase 1b (§12 Phase 1b gate). Dropped as
   disproportionate; correctness gates were kept in full.
3. **No multi-connection isolation test suite.** The process holds a single SQLite
   connection behind one lock; `_write_operation(begin_immediate=True)` still opens the
   transaction before the leading receipt read, so the guarantee is implemented — only
   the multi-connection *test harness* was skipped.
4. **Repeat/Undo/current-value orchestration lives in `bot/database.py` and
   `bot/handlers/receipts.py`, not `bot/services/meal_logging.py`** (§13). The repository
   methods are the transaction boundary and the handlers are thin; introducing a third
   module would have added indirection without removing any. `bot/services/` currently
   holds the pure helpers (`meal_logging.infer_meal_type`,
   `current_values.preview_signature`). Reconsider if Phase 3's draft state machine
   needs a home.

### 0.1b Known debt — resolver layering

`bot/nutrition_resolution.py` was specified by the unit contract (§12) and never created.
The pure resolvers (`resolve_food_diet_entry`, `resolve_recipe_diet_entry`,
`resolve_catalog_food_entry`) still live in `bot/handlers/catalog.py`, and
`bot/database.py` reaches them through a **lazy import inside
`_resolve_quantity_locked`** to dodge a circular import.

This is correct and tested, but it inverts the layering: the repository depends on the
handler package. Unlike the items in §0.1 this was not a considered trade-off worth
keeping — it was the cheap path during the B2 build. Extracting those three functions
(plus `ResolvedCatalogDietEntry` and its private helpers) into a Telegram-free module,
re-exporting from `bot/handlers/catalog.py` for compatibility, is a contained refactor
worth doing before Phase 2 adds a second resolution source (USDA lookup) on top of it.

### 0.2 Carry-over tasks (small, not blocking Phase 2)

Documentation surfaces the unit contract §15 asked for that are not yet updated:

- **`docs/user_guide.html`** predates Phase 1 and documents no Home/Repeat/receipt/usual
  behavior. Nothing in it is wrong (Phase 1 is additive and dark by default), but it is
  incomplete.
- **`docs/backup_runbook.md`** lacks the "Phase 1 adds no migration; Release A is the
  rollback target; accepted rows are never discarded" note.

`README.md`, the operations runbook, `/help`, and `/settings` are current — the latter two
show the fast-logging block and the saved-"usual" count only to Phase 1-enabled users, so
neither advertises an action that would answer "not enabled". Other carry-overs:

- The §13.8 isolated-venv `pip-audit` run was not performed. `pytest`, `compileall`, and
  `pip check` all pass in place, and no runtime dependency was added.
- Two acceptance steps need a human and a Telegram client, not CI: the one-user-then-
  both-user Release B pilot, and the live-client confirmation that the persistent
  keyboard actually disappears on rollback. `tests/test_release_a.py` proves the
  CI-provable half of the latter.
- Catalog foods still have no pin/hide/default preference. This is per plan — the
  preference table is rebuilt in Phase 2's v9 migration (§12 Phase 2), not earlier.
- `/suggestions forget` and `reset-all` remain unimplemented and unadvertised, as
  specified; `reset` continues to mean pin/hide only and now says so.
- Two pre-existing unused imports (`bot/charts.py`, `bot/handlers/analytics.py`) are
  unrelated to this work and left alone.

This plan supersedes the earlier working drafts and resolves the review decisions. It
contains no unresolved product or data-model choices. Values selected by deployment
benchmarking, such as the Whisper model, are release gates with explicit pass/fail
criteria rather than open architecture questions.

## 1. Outcome and scope

The implementation will make common meal logging take one or two taps, while keeping
nutrition deterministic and historical records immutable. It adds:

- a conversational Home entry and a persistent quick-action bar;
- exact Repeat, default-quantity instant logging, and targeted Undo;
- deterministic typed-meal parsing with a safe unknown-food workflow;
- provenance-bearing USDA lookup, with Open Food Facts as a later provider adapter;
- an optional external parser behind informed, versioned consent;
- bounded local voice transcription;
- atomic recipe duplication for variants; and
- a later Supplements adherence section.

The existing slash commands and guided flows remain supported. No LLM or lookup
provider writes nutrition or database identifiers. Completed log values never change
when a source is edited, refreshed, deactivated, or deleted.

## 2. Locked product decisions

| Area | Final decision |
|---|---|
| Home | `Hi`, `hello`, or `hey` at Home shows today's snapshot and section actions. `/start`, `/menu`, and `/help` remain available with their existing distinct meanings. |
| Persistent bar | `[Meal] [Repeat] [Describe]`. `Describe` is hidden until deterministic Describe ships. Exact labels are reserved in every conversation state. |
| Meal inference | Breakfast 04:00-10:59, Lunch 11:00-15:59, Dinner 16:00-21:59, Snack 22:00-03:59 in `LOCAL_TZ`. The inferred type is visible and changeable. |
| Repeat | Atomically copy the previous meal header and item snapshots, including its original meal type. No re-resolution and no confirmation. |
| Recalculation | A separately named `Use current values` action re-resolves all live sources, shows the delta, and requires confirmation. It never partially saves. |
| Instant logging | A default-quantity suggestion at Home creates one single-item meal. Inside the explicit builder, the same selection adds to the visible draft instead. |
| Idempotency | Replaying the same Telegram `update_id` returns the original mutation. Distinct physical presses produce distinct Telegram updates and are treated as distinct user actions. Every instant result has targeted Undo. |
| Calories | Guided, tap, lookup, and parsed known-food flows never request calories. The existing `/diet <meal> <description> <calories>` grammar remains as a documented compatibility exception. |
| Food model | Shared catalog plus private foods. Both allowlisted users may edit shared effective values through audited revisions. Provider snapshots are never edited in place by users. |
| Matching | Explicit selections win. A single exact private match outranks catalog matches. Cross-type or multiple exact matches are shown as ambiguity; fuzzy/substring candidates are never silently chosen. |
| Lookup | USDA FoodData Central is the first provider. Open Food Facts is added only after the same adapter, attribution, and conformance gates pass. |
| LLM | Optional Gemini Flash adapter, configured by exact model ID at deployment. Deterministic parsing runs first; only unresolved syntax may be sent externally after consent. |
| Voice | Telegram delivers audio to the Ledger host. A bounded local Whisper worker transcribes it; audio is never sent to the parser provider. |
| Recipes | Variant creation is an atomic `Duplicate Recipe` followed by normal edits. No lineage table in this delivery. |
| Suggestions reset | Existing preference reset remains separate from forgetting learned history. Learning uses a diet-log ID watermark, not a timestamp. |
| Supplements | Daily adherence and streaks only, after the core logging work. No nutrition effect and no reminder integration in the first release. |

The persistent `Repeat` button is ordinary reply-keyboard text and cannot contain a
per-render callback token. Therefore its exactly-once boundary is the Telegram update:
delivery replay is deduplicated, while two deliberate presses are two logs. This is
explicit, tested behavior; targeted Undo is the recovery path for an accidental second
press. Inline Save/default buttons continue to retire their current UI after the first
successful mutation so stale callbacks fail closed.

## 3. Non-negotiable invariants

1. **Deterministic nutrition.** Only the local resolver calculates calories/macros.
   LLM and provider payloads are untrusted candidate data.
2. **Immutable history.** `diet_logs` and `diet_log_items` snapshot the effective
   nutrition and provenance used at write time. Source changes never rewrite history.
3. **Owner isolation.** Every private-source read and write is scoped by
   `(source_id, user_id)` inside the same transaction as the mutation.
4. **Atomic meals.** A multi-item meal, Repeat, recipe clone, catalog edit, and provider
   import either commit in full or make no domain change.
5. **Replay safety.** The same Telegram update and operation key cannot create a second
   domain row.
6. **No hidden draft mutation.** Instant actions create a complete single-item meal;
   builder actions modify only the visible draft.
7. **No external work under the DB lock.** HTTP, model parsing, downloading, and
   transcription complete before the short validating write transaction.
8. **Consent at the call boundary.** External calls are gated again immediately before
   construction/execution, not only when a flow begins.
9. **Fail closed.** Invalid schema, ownership, callback, provider response, revision, or
   bounds checks cause no partial header, child, audit, or receipt.
10. **Forward-only production schema.** After accepting writes on a new schema, recovery
    is by compatible feature disable or roll-forward, not an older incompatible binary.

## 4. Source and provenance model

### 4.1 Capability matrix

| Capability | Shared catalog | Personal food | Recipe | Adhoc/freetext |
|---|---|---|---|---|
| Ownership | Shared | One user | One user | Snapshot only |
| Editable by | Either allowlisted user, audited | Owner | Owner | Not reusable |
| Provider provenance | Required snapshot; manual shared rows use the Ledger manual provider | Optional immutable origin snapshot | Immutable batch-resolution snapshot of ingredient revisions | None |
| Local revision | Shared append-only effective revision | Monotonic owner revision | Monotonic owner revision | None |
| Recipe ingredient | Yes | Yes | No nested recipes | No |
| Pin/hide/default | Per user | Per user | Per user | No |
| Suggestions | Per-user history/preferences | Per-user history/preferences | Per-user history/preferences | Never |
| Matching | After private exact matches | Private exact tier | Private exact tier | Never matched |

Shared editing is intentionally shared: after explicit delta confirmation, a revision
affects both users' future direct logs and future recipe resolutions. Past logs remain
unchanged. The edit screen states this consequence and exposes the revision audit.

### 4.2 Immutable provider snapshots

Add append-only `food_source_snapshots`:

- `id`;
- `provider`, `provider_food_id`, nullable `provider_revision`;
- non-null `revision_key`;
- canonical `content_hash`;
- normalized display name, name key, brand, category, base unit, basis amount;
- calories, protein, carbs, and fat, preserving unknown as `NULL`;
- nullable provider retrieval timestamp and source URL;
- attribution text and nullable license identifier/URL;
- canonical quality flags, including `provider_reported`, `derived`,
  `incomplete`, `approximate`, and `user_entered`; and
- `provenance_version` (`0` for migrated starter data, `1` for validated provider/manual
  imports); and
- non-null Ledger capture timestamp.

For migrated v0 starter data, retrieval time, source URL, and license may truthfully be
null; migration time is recorded only as Ledger capture time and is never presented as
provider retrieval time. Validated v1 adapters must populate every field their provider
contract supplies and apply explicit `incomplete` quality flags for permitted omissions.
After the migration backfill, an insert trigger rejects any new snapshot with
`provenance_version = 0`.

The unique identity is `(provider, provider_food_id, revision_key)`. For a provider with
a stable revision, `revision_key` is that normalized revision. If the provider exposes no
stable revision, it is the SHA-256 content hash. The content hash covers every normalized
profile field, provenance/quality field, alias, and portion in canonical order. Receiving
different content for an existing stable revision is a provider-consistency failure, not
an in-place update. Migrated starter rows use `legacy:<content_hash>` so the first
validated provider import cannot collide with them.

Hash input is UTF-8 canonical JSON with sorted object keys, normalized decimal strings,
and aliases/portions sorted by normalized key. Local database IDs and timestamps not
supplied by the provider are excluded. In particular, Ledger capture time and retrieval
observation time never affect identity/idempotency; only substantive provider content,
stable source URL identity, attribution/license identity, and quality classification are
hashed. Transient/signed query parameters, request IDs, cache metadata, and observation
timestamps are excluded.

Snapshot aliases and portions live in child tables keyed to `snapshot_id`, so a new
snapshot has a complete, independent child set. Add `UNIQUE(id, provider,
provider_food_id)` to support identity-preserving composite foreign keys.

Database triggers reject update or delete of snapshots and their children. Application
code can only append them.

### 4.3 Shared catalog identity and local overrides

Rebuild `catalog_foods` as a stable shared identity/head:

- existing `id` preserved;
- unique `(provider, provider_food_id)`;
- `current_snapshot_id`;
- nullable `created_by` for provider data, required for manual shared data;
- `is_active`, nullable `withdrawn_at`, and timestamps.

`created_by` references `users`; a CHECK requires it when
`provider = 'ledger-manual'`.

User edits do not alter the provider snapshot. They create append-only full-state
`catalog_food_revisions` with:

- `catalog_food_id`, monotonic `local_revision`, and `base_snapshot_id`;
- `override_active`;
- the complete effective display/nutrition profile;
- complete revision-scoped aliases and portions;
- `edited_by`, `edited_at`, and a bounded reason; and
- a unique `(catalog_food_id, local_revision)`.

`catalog_food_revision_heads` selects the current local revision. Resolution uses the
active local head; otherwise it uses `current_snapshot_id`. Full-state revisions avoid
confusing an intentionally unknown nutrient with an inherited value.

The catalog head uses composite FK
`(current_snapshot_id, provider, provider_food_id) -> food_source_snapshots(id, provider,
provider_food_id)`. Revision-head rows have a composite FK
`(catalog_food_id, local_revision) -> catalog_food_revisions`. Insert/update triggers
verify that each revision's `base_snapshot_id` has the same provider identity as its
catalog food. These checks apply to inactive identities too, so a corrupt withdrawn row
cannot become valid merely by reactivation.

Every edit is optimistic:

1. the UI carries the expected `local_revision`;
2. the transaction verifies the current head;
3. it appends revision `expected + 1`;
4. it moves the head only if the expected revision still matches; and
5. a stale edit returns a conflict and current delta without overwriting.

Provider head movement is also compare-and-swap on the expected
`current_snapshot_id`, so a response based on a stale head cannot overwrite a concurrent
refresh. Each adapter additionally rejects a provider revision that its provider-specific
ordering proves older than the current revision. Providers without ordered revisions use
content-addressed revisions and make no stronger chronology claim.

Removing an override appends an audited inactive revision. Provider refresh moves only
the provider head. If an override is based on an older snapshot, it remains effective
but is marked as needing review; the user may explicitly rebase or remove it.

Manual shared foods use `provider='ledger-manual'`, a random opaque UUID as
`provider_food_id`, initial revision `1`, user-entered attribution, and the
`user_entered` quality flag. Identity is never derived from a mutable name. If a user
edits an online prefill before first save, the untouched provider snapshot is stored and
the edited effective values become local revision 1.

### 4.4 Personal foods and recipes

Add `local_revision INTEGER NOT NULL DEFAULT 1` to `foods` and `recipes`. Edits use
compare-and-swap and increment the revision. A recipe revision describes its structure
(name, yield, and ingredient rows); any ingredient change increments it in the same
transaction. A referenced food changing later does not pretend that the structural
recipe revision changed.

The base-unit dimension of every reusable food/catalog identity is immutable after
creation. A g/ml/piece dimension change creates a new identity and deactivates the old
one; a provider refresh requesting a different dimension is stored as a conflict and
does not move the head. Stored recipe amounts are never silently reinterpreted.

Add one-to-one `food_provenance`:

- `(food_id, user_id)` with the existing owner-preserving food FK;
- `source_snapshot_id`;
- nullable source catalog ID and applied source catalog local revision; and
- import timestamp.

A lookup saved privately points to the immutable selected snapshot. A personal copy of
a shared override also records the catalog identity and applied revision. Later personal
edits increment `foods.local_revision`; the provenance remains an origin record, not a
claim that current values still equal the provider. Manually entered personal foods have
no provenance row.

`source_snapshot_id` is `ON DELETE RESTRICT`. When a catalog revision is recorded, the
pair `(source_catalog_food_id, source_catalog_local_revision)` references that immutable
revision; otherwise both fields are null. A both-null-or-both-non-null CHECK rejects
either half-populated form.

### 4.5 Recipe ingredient references

Rebuild `recipe_ingredients`; do not use an unenforceable generic polymorphic ID:

- nullable `personal_food_id`;
- nullable `catalog_food_id`;
- non-null `resolved_base_unit` captured when the ingredient is added;
- `CHECK` that exactly one is non-null;
- composite FK `(recipe_id, user_id) -> recipes(id, user_id)`;
- composite FK `(personal_food_id, user_id) -> foods(id, user_id)`;
- FK `catalog_food_id -> catalog_foods(id)`;
- partial unique indexes for `(recipe_id, personal_food_id)` and
  `(recipe_id, catalog_food_id)`; and
- `ON DELETE RESTRICT` for ingredient sources.

Existing `food_id` values backfill into `personal_food_id`. Deactivation, not deletion,
is the normal source-removal path. Existing recipes may continue resolving a deactivated
ingredient's last effective values with an archived-source warning; new ingredients may
only select active sources. Resolution verifies that the live source dimension still
matches `resolved_base_unit`; a mismatch is a corruption/repair state, never an implicit
conversion.

Add append-only `recipe_resolution_snapshots` and ordered child rows. Each resolution
snapshot records:

- user ID, recipe ID, and structural `local_revision`;
- the recipe display name, yield amount, and yield unit at that structural revision;
- a canonical content hash;
- every ingredient's source type/ID, applied provider snapshot/local revision, and base
  unit;
- entered/resolved amounts and per-ingredient nutrition; and
- final nutrition totals for one complete recipe yield.

Preview computes the would-be content hash in memory. Final meal Save creates or reuses
the content-addressed resolution snapshot in the same transaction as the diet log after
all live sources are revalidated. This means the same structural recipe revision may
correctly have different resolution snapshots after an ingredient source changes,
without losing the provenance of either result or writing audit data before confirmation.
The resolution snapshot is a batch/composition snapshot, not a per-log serving. The
`diet_log_items` row holds the user's entered quantity, resolved fraction of the recipe
yield, and the scaled nutrient snapshot.

Resolution snapshots have owner-preserving FK `(recipe_id, user_id)`, unique
`(id, user_id, recipe_id, recipe_local_revision)`, and content-address uniqueness on
`(user_id, recipe_id, recipe_local_revision, content_hash)`. Child rows carry `user_id`
and use a composite FK to the resolution owner tuple. Personal ingredient references use
the existing composite food-owner FK; provider snapshot references use the same
snapshot/provider-identity composite FK as catalog heads.

### 4.6 Completed-log provenance

One column-aware `diet_log_items` rebuild adds:

- `source_snapshot_id`;
- `recipe_resolution_snapshot_id`;
- `source_provider_food_id`;
- `source_local_revision`;
- `provenance_version`, with `0` for unprovable legacy rows and `1` for new writes; and
- `adhoc` in the `source_type` constraint.

`source_revision` means provider revision. A catalog item snapshots provider identity,
provider revision/content snapshot, and the applied shared local revision. A personal
food snapshots its local revision and origin snapshot when present. A recipe snapshots
its structural recipe revision plus `recipe_resolution_snapshot_id`. Adhoc and legacy
freetext rows have no live source identity.

When a shared override is active, the log records that revision's `base_snapshot_id`,
not a newer provider head that the override has not been rebased onto. This makes the
provider/local pair describe the actual effective values used.

`source_id` intentionally has no live-source FK so completed history survives
deactivation. `source_snapshot_id`, when present, references immutable data with
`ON DELETE RESTRICT`. Enforce composite provenance FKs:

- `(source_snapshot_id, source_provider, source_provider_food_id) ->
  food_source_snapshots(id, provider, provider_food_id)` when a food snapshot is present;
  and
- `(recipe_resolution_snapshot_id, user_id, source_id, source_local_revision) ->
  recipe_resolution_snapshots(id, user_id, recipe_id, recipe_local_revision)` for recipe
  rows.

Add unique `(diet_log_id, item_order)` and source-dependent nullability checks.

For `provenance_version = 1` writes, the row-shape checks are explicit:

| `source_type` | Required live fields | Required-null fields |
|---|---|---|
| `catalog` | `source_id`, food snapshot/provider/provider-food-ID tuple, entered quantity/unit, resolved base amount/unit | recipe resolution; local revision is null when no override applied |
| `food` | `source_id`, `source_local_revision`, entered quantity/unit, resolved base amount/unit | recipe resolution; origin snapshot/provider/provider-food-ID is all-null for manual foods or all-present for imported foods |
| `recipe` | `source_id`, `source_local_revision`, `recipe_resolution_snapshot_id`, entered quantity/unit, resolved base amount/unit | food snapshot/provider/provider-food-ID tuple |
| `adhoc` | calorie snapshot | every live/provenance field, recipe resolution, and resolved-base fields |
| `freetext` | display and any user-entered nutrient snapshots | every live/provenance field and recipe resolution |

For catalog snapshots whose provider exposes no revision, `source_revision` may be null;
`source_snapshot_id` and its content hash remain the authoritative version. Service-layer
validation additionally proves that a `food`/`recipe` source belongs to the acting user
and that a catalog source is the active shared identity selected by the user.
Reusable entered/resolved amounts must be finite, positive, and within the existing
catalog limits; units are bounded and compatible through the deterministic resolver.
CHECK constraints enforce the all-null/all-present origin tuple and reject a
recipe-resolution ID on non-recipe rows.
Migrated `provenance_version = 0` rows preserve their existing source type/ID/provider
values but may leave every newly added provenance field null; the CHECK constraints
explicitly allow that legacy shape. After copying, an insert trigger rejects new
`provenance_version = 0` rows.
They still require the owner/header FK, non-negative item order, bounded non-empty
display, and existing nutrient bounds.

Adhoc rows require:

- null source/provenance identifiers;
- calories in `0..MAX_LOG_CALORIES`;
- optional macros in `0..MAX_LOG_MACRO_GRAMS`;
- null resolved-base fields; and
- bounded display text. Entered amount/unit may be null because the value is explicitly
  a one-off total, not a reusable composition.

## 5. Catalog import and reconciliation

Replace `seed_catalog()` with two explicit import modes.

### 5.1 Complete bundled manifest

The bundled starter manifest is authoritative only for its own provider namespace:

1. validate and normalize the entire manifest outside the DB lock;
2. stage its identities and immutable snapshots;
3. in one transaction, append new snapshots, move provider heads, and reactivate seen
   identities;
4. soft-deactivate identities absent from that complete manifest;
5. use snapshot-scoped aliases/portions, so stale child rows cannot remain current; and
6. leave manual identities and local revisions untouched.

Record `catalog_import_runs` with provider, manifest revision/hash, completeness flag,
counts, timestamps, and sanitized outcome. Re-importing the identical manifest is a
no-op. A malformed bundled manifest or failed authoritative reconciliation aborts
startup rather than serving a partially reconciled catalog.

### 5.2 Partial online lookup

USDA/OFF searches are partial and never imply withdrawal. Selection fetches and validates
the complete candidate outside the DB lock, but only final food-save confirmation appends
its snapshot/catalog or personal row in the same transaction. Cancellation leaves no
orphan provider row. Absence from search never deactivates anything. A selected identity
is deactivated only when an explicit by-ID provider fetch reports it withdrawn/deleted
under that provider's contract.

All HTTP and payload validation occurs outside the database lock. The short import
transaction revalidates the expected catalog/local revision before moving a head.

## 6. Logging semantics

### 6.1 Exact Repeat

Repeat finds the acting user's most recent completed diet log overall and, in one
transaction:

- creates a new header with the current timestamp;
- copies the original meal type and description;
- copies every item snapshot and provenance field verbatim; and
- records the new mutation receipt.

For a legacy header with no children, Repeat copies the header exactly and does not
invent child provenance. Archived/deleted sources do not matter because no source is
resolved. A failure copies nothing.

`Use current values` is a different action. It attempts to resolve every structured live
source, displays per-item and total deltas, and saves only after confirmation. If any
source is unavailable or ambiguous, it shows repair choices and writes nothing. Adhoc
and freetext snapshots are carried forward unchanged while live structured sources are
re-resolved. A legacy header with no children has no current-value action because there
is no trustworthy source to resolve.

### 6.2 Instant default and builder

- At Home, selecting a food/recipe with a valid per-user default amount/unit immediately
  creates one single-item meal with the inferred meal type.
- Without a default, the flow asks quantity and then confirms or offers `Set as default`.
- Inside the explicit multi-item builder, selections add to the visible draft regardless
  of defaults; only final Save mutates the ledger.
- The receipt includes exact meal ID, summary, `Undo`, `Log another`, and
  `Use current values` when applicable.

### 6.3 Targeted Undo

Receipt Undo calls the owner-scoped exact-ID deletion path, checks the existing 24-hour
limit, and atomically deletes the selected header and children. It is idempotent if that
meal was already removed. An intervening log cannot change the target.

### 6.4 Suggestion preferences and learning

Keep these commands distinct:

- `/suggestions reset`: clear pins and hides only; preserve saved default quantities.
- `/suggestions forget`: atomically set
  `user_settings.suggestions_after_log_id` to that user's current maximum `diet_logs.id`.
- `/suggestions reset-all`: clear pins, hides, and defaults and set the watermark in one
  transaction.

Ranking and learned-quantity queries filter owner-scoped logs with
`diet_log_id > suggestions_after_log_id`. Ledger rows are not deleted; this is
behavioral forgetting, not data erasure. Undoing an older meal cannot make pre-watermark
learning visible again.

Catalog ranking considers only active catalog identities present in that user's
completed history or explicit preferences; it never enumerates the full provider
catalog. Hide wins over pin; pinning an item clears its hidden flag.

## 7. Home and routing contract

### 7.1 Home choreography

At Home, a greeting sends:

1. today's snapshot with one inline section/action keyboard; then
2. a short quick-action message carrying the persistent reply keyboard.

One Telegram message never attempts to carry two reply markups. Section actions and
disabled features are generated from feature flags. `/start`, `/menu`, and `/help`
remain distinct read-only commands and do not silently clear an active conversation.

At Home:

| Input | Behavior |
|---|---|
| `Meal` | Start the tap builder with inferred meal type selected |
| `Repeat` | Exact Repeat immediately; one mutation per distinct Telegram update |
| `Describe` | Enter empty Describe input; no external consent required |
| `Hi`/`hello`/`hey` | Show Home |
| Other non-command text | When deterministic Describe is enabled, parse immediately; before Phase 3, show Home plus `Use Meal or /diet` without consuming it as diet data |
| Voice | Start local transcription if locally enabled; external consent is irrelevant until parser escalation |

### 7.2 Reserved labels inside conversations

Reserved bar labels, current/previously shipped labels, and greetings use trimmed,
case-folded exact matching. They are registered before generic text handlers in every
state.

Inside any active flow:

- `Meal` in a Diet/Describe state re-renders the current step/draft without changing it.
- `Repeat`, `Describe`, Home/greetings, and section switches respond with
  `Finish this flow or /cancel first`; they do not mutate state, draft, DB, or network.
- In Study, Gym, or Habit setup, all bar labels/greetings use the same active-flow hint.
- Voice is rejected before download unless the user is at Home or in an empty
  Describe-input state.
- `/cancel` is the only implicit flow switch; it invalidates the draft/job and returns
  Home.

State-specific ordinary input is fixed:

| State group | Ordinary text | Voice |
|---|---|---|
| Diet `MEAL_TYPE`, `FOOD_CHOICE`, `PORTION_CHOICE`, `CONFIRM_ITEM`, `LOG_ANOTHER`, draft review, ambiguity, unknown-food, lookup results | `Use the buttons or /cancel`; preserve state | Reject before download |
| Diet `SEARCH` | Catalog query | Reject before download |
| Diet `CUSTOM_AMOUNT` or missing-amount | Validated amount/portion | Reject before download |
| Legacy Diet `FOOD_ITEMS`, `CALORIES`, `MACROS` | Existing prompt input | Reject before download |
| Empty `DESCRIBE_INPUT` | Parse as new draft | Accept local voice |
| Provider/transcription pending | `Still working`; `/cancel` and matching `/settings ... off` allowed | Reject second job |
| Study `SUBJECT`, `DURATION`, `NOTES` | Existing prompt input | Reject before download |
| Gym `EXERCISE`, `SETS`, `REPS`, `WEIGHT` | Existing prompt input | Reject before download |
| Gym `MORE` | `Use the buttons or /cancel` | Reject before download |
| Habit `ADDING_HABIT` | Habit name | Reject before download |

Callback-only states receive explicit text/voice catchalls so updates cannot fall
through to Home handlers. Home text/voice entry points are registered after the existing
ConversationHandlers in the same handler group. Unauthorized updates remain handled by
the existing authentication boundary and produce no state or external call.

After timeout/cancel, the next update follows Home rules. Stale inline callbacks retire
their markup. Previously shipped reply labels remain recognized even when their feature
is disabled; the compatibility handler makes no mutation/network call and sends the
current keyboard or `ReplyKeyboardRemove`.

The reply keyboard rolls out in two releases: first ship the disabled-label
compatibility/removal handlers, then enable the keyboard by flag. A rollback target must
contain those compatibility handlers.

## 8. Parser, matching, and draft lifecycle

### 8.1 Single schema

Both deterministic and external parsers use exactly:

```text
MealParseResponse {
  segments: ordered list[MealSegment]
}

MealSegment {
  segment_id: non-negative integer,
  input_start: non-negative integer,
  input_end: integer greater than input_start,
  raw_text: string,
  food_guess: string | null,
  quantity: finite positive number | null,
  unit: string | null
}
```

The deterministic parser assigns stable, non-overlapping Python Unicode-code-point spans
over the normalized input and preserves their original order. `food_guess = NULL` marks
syntactically unresolved text. The external
adapter receives only those unresolved segments and must echo the exact `segment_id` and
span; local validation rejects missing, duplicate, added, reordered, or changed spans
before merging results back into the original list. This preserves interleaving and
duplicate mentions.

Segments never contain calories, macros, provider IDs, source IDs, or DB IDs.
Validation rejects extra fields, booleans as numbers, NaN/infinity, non-positive or
over-limit quantities, unsupported units, and oversized values.

Concrete limits:

- input: 500 Unicode characters;
- items: 20;
- response: 16 KiB;
- `raw_text`: 200 characters;
- `food_guess`: 100 characters;
- unit/portion: 50 characters;
- quantity: `0 < quantity <= 1,000,000`; and
- provider candidates shown: 8.

For a parsed segment, `quantity = NULL, unit != NULL` is invalid. An unresolved segment
must have null food/quantity/unit. A quantity without a unit remains unresolved
unless the selected source has exactly one valid count mapping. Standard aliases reuse
the existing nutrition unit parser; named portions are validated only against the
selected food. Ranges such as `2-3` require user choice. Duplicate mentions remain
separate and retain input order.

### 8.2 Matching order

1. Explicit callback/reference ID, owner-validated.
2. Exact normalized private food/recipe names. One result is selected; a food/recipe
   collision is ambiguous.
3. One exact normalized catalog canonical name, unless a private exact match exists.
4. Exact alias candidates.
5. Bounded SQL `LIKE` candidates.

Multiple candidates at any tier are shown; fuzzy/substring matching never silently
selects. Only active sources appear for new selection. FTS is deferred until a measured
catalog-size/latency threshold justifies a migration.

### 8.3 Deterministic-first disclosure

The deterministic parser runs first. It retains every syntactically parsed item. Only
syntactically unparsed segments may be sent to the external parser, and only after the
call-boundary consent check. Missing quantity, ambiguous local match, and unknown food
are local resolution states and never invoke the LLM.

Simple rules include standard quantity/unit aliases and obvious count forms. `a/an`
becomes one piece only after the matched source validates a piece/count mapping;
otherwise amount remains missing. The parser never invents a midpoint for ranges or a
unit conversion across dimensions.

### 8.4 Draft lifecycle

1. Normalize and bound input.
2. Run deterministic parsing.
3. Optionally parse unresolved syntax externally.
4. Match candidates locally.
5. Store ordered draft items with `draft_id`, `ui_revision`, and status:
   `RESOLVED`, `NEEDS_AMOUNT`, `AMBIGUOUS`, or `UNKNOWN`.
6. Resolve items one at a time without removing completed items.
7. Lookup/add/adhoc returns to the same item and then advances.
8. Edit/remove/change-quantity increments `ui_revision`; old callbacks become stale.
9. Save is enabled only when every item is resolved and item/aggregate bounds pass.
10. Final Save snapshots every item atomically with its mutation receipt.

Back returns to the draft without discarding resolved items. Removing the current
unknown item is explicit. Cancel or the existing 300-second timeout discards the entire
in-memory draft and invalidates callbacks/jobs. Drafts are intentionally not durable
across process restart; after restart, an old callback says the draft expired and starts
a fresh flow. No partial meal is ever persisted.

### 8.5 Unknown-food flow

For an unmatched food:

1. `Look it up online` - after lookup consent, show up to eight USDA candidates, then
   store the selected immutable provider snapshot.
2. `Add my values` - enter one reusable food manually.
3. `Just this once` - enter required calories and optional macros as adhoc.
4. `Remove item` or `Back`.

Online and manual reusable foods default to the shared catalog, with an explicit
`Save privately` alternative. The confirmation states who can edit the result.
Provider-prefilled values are validated through the same nutrition bounds. Saving the
selected online record privately creates `food_provenance`; manual private values do not.

## 9. Provider, consent, privacy, and failure policy

### 9.1 Configuration boundary

Deployment configuration owns global kill switches:

- `LOOKUP_ENABLED`;
- `EXTERNAL_PARSE_ENABLED`;
- `VOICE_ENABLED`;
- provider endpoint/model identifiers; and
- API keys.

Per-user `user_settings` owns:

- online lookup consent, consented provider/terms identity, disclosure version, and
  consent/revocation timestamps;
- external parsing consent, consented provider, disclosure version, and timestamps; and
- local voice enablement/disclosure acknowledgement.

Effective permission is global flag AND current per-user consent/preference. A new
provider/terms identity or disclosure version invalidates prior consent until the user
accepts again. Adding Open Food Facts therefore cannot reuse USDA consent. Local
deterministic text parsing never needs consent. Local voice enablement is separate from
permission to transmit its transcript.

User controls are explicit:

- `/settings lookup on|off`;
- `/settings external on|off`;
- `/settings voice on|off`; and
- the interactive disclosure/confirm action before each first `on`.

The active `WAITING` handlers permit the matching `off` command and `/cancel` while a
request/job is pending. Revocation invalidates its token immediately; all other ordinary
input receives the bounded busy response.

### 9.2 Provider adapters

Adapters implement typed contracts rather than relying on base-URL compatibility:

- `FoodLookupProvider.search(query) -> bounded candidates`;
- `FoodLookupProvider.fetch(id) -> validated source snapshot`; and
- `MealParserProvider.parse(unresolved_segments) -> MealParseResponse`.

USDA is the first lookup adapter. Open Food Facts and additional OpenAI-compatible model
providers are not declared supported until their conformance suites pass, including
structured-output subset behavior.

External request policy:

- connect timeout 3 seconds;
- total lookup timeout 8 seconds;
- total parser timeout 15 seconds;
- at most one retry for idempotent transient timeout/429/5xx;
- honor `Retry-After` up to 5 seconds, otherwise fail to local/manual flow;
- response-byte and candidate limits enforced before parsing; and
- no retry on validation, authentication, or consent failure.

Lookup and external-parse handlers use PTB `block=False` plus
`ConversationHandler.WAITING`, not a blocking await under the application's sequential
update processor. A small async request manager allows at most two provider requests
globally and one per user; excess work fails immediately to local/manual flow. Each
request carries user, draft ID/revision, request token, provider/terms identity, and
consent version. `/cancel` or consent revocation invalidates the token. The coroutine
checks permission immediately before the HTTP call and checks token/consent/draft again
before applying the result. Late/stale results are discarded.

Provider errors preserve the current draft and offer retry/manual/back. Lookup and model
work never occurs under the DB connection lock.

### 9.3 Disclosure and logging

The settings disclosure says:

- Telegram already transports messages and voice notes to the Ledger host.
- Online lookup sends the current search text to the selected food provider.
- External parsing sends only unresolved text from the current meal/transcript.
- The selected unpaid model tier may use submitted content for product improvement or
  human review; users should avoid sensitive information.
- History, stored nutrition, source IDs, and audio are not sent to the parser provider.

Revocation takes effect before the next call. If consent changes during a pending request,
the result is discarded.

Extend secret redaction to every provider key. HTTP/provider logs must not include meal
text, transcript text, query URLs, response bodies, Telegram IDs, or keys. INFO logs use
only sanitized provider, latency bucket, count, and outcome category.

Free tiers are an operational cost target within current quotas, not a guarantee.

## 10. Voice workflow

Keep `Application.concurrent_updates=False`. Use PTB's public nonblocking conversation
mechanism:

- voice `MessageHandler(..., block=False)` at Home or empty `DESCRIBE_INPUT`;
- `ConversationHandler.WAITING` handles `still transcribing`, rejects a second voice,
  and permits `/cancel`;
- the pending coroutine awaits the transcription manager, checks user/job/draft
  revision, creates the parsed draft, sends the next card, and returns the next normal
  state; and
- never mutate private ConversationHandler internals or inject synthetic updates.

The WAITING `/cancel` handler invalidates the job token and requests cancellation; it
does not attempt to force a state transition itself. The pending nonblocking coroutine
observes cancellation and returns `ConversationHandler.END`. It catches every failure so
the active-flow marker cannot remain orphaned.

The transcription manager is a supervised preloaded worker process:

- one global transcription runs at a time;
- one additional bounded queue slot;
- at most one running/queued job per user;
- correlation by user, chat, job token, draft ID, and draft revision;
- 60-second voice-duration limit;
- 10 MiB declared and downloaded-byte limit;
- 60-second hard transcription timeout;
- 5-second shutdown grace; and
- no runtime model download.

Validate duration and declared size before `get_file`; enforce bytes again during/after
download. Use a randomized owner-private temp directory/file. Clean it on success,
validation failure, download failure, transcription/parser failure, timeout,
cancellation, send failure, and shutdown. Sweep stale Ledger temp files at startup.

Cancellation invalidates the token. A queued job is removed. A running hung/cancelled job
terminates and recreates the worker process, because abandoning a thread would not stop
CPU inference. Late results never send messages or touch drafts.

Failure to load the pinned model marks voice unhealthy and disables only the voice flag;
text/tap logging and the bot process remain available. Runtime worker crashes are retried
once by recreating the worker, then voice is disabled until an operator health check
passes.

Shutdown stops voice admission, invalidates and removes queued jobs, waits up to the
5-second grace for the active job, terminates it if needed, suppresses completion
messages, sweeps temp files, and only then allows the normal database shutdown to finish.

The feature remains off until a host benchmark selects and pins the smallest multilingual
model meeting:

- a committed set of at least 20 representative voice fixtures preserves every expected
  quantity and food-name token in at least 19 fixtures;
- p95 transcription time for a 30-second fixture is at most 20 seconds;
- peak worker RSS is at most 2 GiB and at most 50% of host RAM; and
- model load and one transcription survive a clean restart.

`faster-whisper` uses PyAV's bundled FFmpeg libraries; no separate system `ffmpeg`
dependency is added unless future external transcoding explicitly requires it.

After local transcription, deterministic parsing always runs. External parsing is
offered only for unresolved syntax and only with separate current consent. Without that
consent, the flow continues through local missing/ambiguous/unknown/manual resolution.

## 11. Ordered migrations

Version numbers are now locked because the delivery order is locked. Every migration is
transactional, preserves IDs/timestamps, runs the verifier manifest for the version it
just created, and finishes with `integrity_check` and `foreign_key_check`. After the last
step, startup runs the full verifier for the target/latest version; an intermediate v9
verifier does not require v10-v13 objects.

### v9 - user controls, revisions, and preferences

- Add `foods.local_revision` and `recipes.local_revision`, backfilled to 1.
- Add `user_settings.suggestions_after_log_id`, default 0.
- Add versioned lookup consent, external-parser consent, and local-voice preference
  fields described above, all default-off.
- Rebuild `user_food_preferences` to allow `catalog`.
- Add checks that default amount/unit are both null or both valid; preserve every current
  row.
- Update reset behavior so pin/hide reset does not delete saved defaults.

Before rebuilding preferences, preflight source ownership/existence and half-populated or
out-of-bounds defaults. Any invalid row aborts with count-only diagnostics for reviewed
repair; migration never silently deletes or rewrites it. Runtime inserts/updates use the
same transactional target validation, and startup verifies the polymorphic references.

### v10 - immutable source snapshots and shared catalog heads

- Add `food_source_snapshots` plus immutable alias/portion children.
- Rebuild `catalog_foods` into stable identity/head rows while preserving every catalog
  ID.
- Backfill each existing curated row and its child sets into provenance version 0
  snapshots marked `approximate` with explicit Ledger starter-data attribution.
- Add append-only shared revisions, revision children, revision heads, and import runs.
- Add `food_provenance`.
- Replace startup seed behavior with complete-manifest reconciliation.
- Verify that every catalog identity, active or inactive, has one valid current snapshot
  and matching provider identity.

The migration first materializes/validates snapshots, then rebuilds catalog identities
and children, and only drops old tables after counts, IDs, timestamps, identity mappings,
and references match. With foreign keys enabled, it drops the migrated old alias/portion
child tables before the old catalog parent; deferred foreign keys do not substitute for
safe drop order.

### v11 - recipe ingredient sources and resolution snapshots

- Rebuild `recipe_ingredients` with dual personal/catalog FKs and exactly-one constraint.
- Backfill existing food IDs into `personal_food_id` and capture each existing food's
  validated base unit as `resolved_base_unit`.
- Preserve ingredient IDs, order-equivalent behavior, amounts, units, and timestamps.
- Add partial unique indexes and validate owner isolation.
- Add immutable `recipe_resolution_snapshots` and ordered ingredient children.
- Enforce source base-unit stability once referenced.

### v12 - structured-log provenance and adhoc

- Rebuild `diet_log_items` once to add food snapshot/provider/local-revision provenance,
  recipe resolution provenance, and `provenance_version`, and to allow `adhoc`.
- Copy every existing column, including the v8 provider fields, by explicit
  column-aware mapping.
- Mark old rows provenance version 0; do not invent missing provenance.
- Preflight every newly imposed legacy constraint because v8 did not enforce all of
  them: duplicate `(diet_log_id, item_order)`, owner/header mismatch, negative order,
  empty/oversized display, non-finite/out-of-bound nutrients, and impossible existing
  source-type/nullability combinations. Any violation aborts with count-only diagnostics
  for reviewed repair.
- Add unique item order and the provenance-version-dependent nullability/bounds
  constraints defined above.
- Preserve all IDs and timestamps.

### v13 - Supplements, after core logging phases

- `supplements`: owner-scoped identity, normalized active-name uniqueness,
  `UNIQUE(id, user_id)`, soft deactivation, timestamps.
- `supplement_activity_periods`: composite `(supplement_id, user_id)` ownership FK,
  `start_date <= end_date` when closed, at most one open period by partial unique index,
  and trigger/service checks preventing overlaps; start/end local dates make adherence
  denominators exclude inactive periods.
- `supplement_checkins`: composite owner-preserving FK, unique
  `(user_id, supplement_id, checkin_date)`, cascade only from supplement identity.
- Period endpoints are inclusive in `LOCAL_TZ`, matching habits: activation opens today,
  deactivation closes at `MAX(start_date, today)`, and reactivation starts a new period.
  Streaks count consecutive scheduled local dates inside those non-overlapping periods.

No recipe-lineage migration is included. Duplicate Recipe provides independent variants.

### Schema verifier

Run on every startup, including when `user_version` already equals latest. Verify:

- required tables, columns, defaults, nullability, checks, FKs, and indexes;
- immutable-snapshot/revision triggers;
- current-head/provider-identity invariants;
- ownership composites;
- `integrity_check`;
- `foreign_key_check`; and
- sanitized invariant queries for orphaned heads, revisions, preferences, and ingredient
  sources.

A current-version-but-invalid database aborts startup. Catch only documented optional
provider/feature failures; never hide schema errors with `BaseException`.

## 12. Delivery sequence and build gates

### Phase 0 - harden the v8 baseline — DONE

No new product feature begins until all are complete:

1. Unify typed and tapped paths in one draft/save service.
2. Make every resolved typed/tap path write structured `diet_log_items`; legacy manual
   text becomes an explicit `freetext` child.
3. Fix `Add another -> Type it` so existing draft items survive.
4. Add current-version schema verification and column-aware v8 rebuild preservation.
5. Pass item totals through the nutrition finalizer and enforce
   `MAX_MEAL_ITEMS = 20` at the DB/service boundary.
6. Keep Save retryable: retire UI only after commit or re-render on failure.
7. Require the current UI message for inline Cancel.
8. Enforce private source/preference ownership inside each write transaction.
9. Make the current bundled seed an exact provider-scoped snapshot: deactivate removed
   starter rows and replace stale aliases/portions atomically.
10. Preserve existing `/suggestions reset` meaning. The new `forget`/`reset-all`
    commands ship with the v9 watermark in Phase 2.

**Gate:** all reproduced regressions have tests; the full existing suite is green;
malformed/mis-stamped schemas fail closed; no cross-owner direct DB write succeeds.

### Phase 1a - Home and routing, dark first — DONE

- Add Home snapshot and distinct greeting handling.
- Add every reserved-label/state interceptor and stale-label compatibility handler.
- Ship with persistent keyboard disabled; run routing integration tests.
- Enable `[Meal] [Repeat]` after the rollback target includes keyboard removal support.
- Keep `Describe` hidden.

**Gate:** every real conversation state consumes every reserved label/text/voice category
according to the currently enabled phase, with no draft loss or accidental external
call; pre-Describe arbitrary text produces only the documented Home hint.

### Phase 1b - fast local mutations — DONE

- Clock-inferred meal type.
- Default quantity UI for private foods/recipes using existing preference columns;
  catalog defaults/pins become available after the v9 preference rebuild.
- Exact Repeat, targeted Undo, and `Use current values`.
- Catalog history participates in per-user ranking.
- Add suggestion pagination and edit/remove/change-quantity controls.

**Gate:** a private food/recipe with a default is two taps from Home; Repeat is one tap
from Home; ~~local handler processing p95 is below 500 ms~~ (benchmark gate dropped, see
§0.1); replay and intervening-log Undo tests pass. **Met**, with a saved "usual" making a
private food *one* tap from Home rather than two.

### Phase 2 - source schema and USDA lookup — NEXT

- Ship v9-v12 with all external feature flags off.
- Back up, migrate, verify, and smoke-test before polling resumes.
- Add catalog snapshot/revision service and refactor every existing catalog
  search/resolve/log path to the effective-head model in the same release; flags-off
  behavior must still support today's catalog commands and tap flow.
- Add the USDA adapter.
- Add lookup consent/settings and unknown-food reusable/adhoc paths.
- Enable catalog pin/hide/default preferences and the `forget`/`reset-all` learning
  commands after v9 is active.
- Enable lookup for one user, then both.

**Gate:** provenance survives personal/shared save and edits; complete manifest and
partial search semantics pass; no provider call occurs without consent; restart cannot
clobber an override or revive stale children.

### Phase 3 - deterministic Describe

- Add the single parser schema and rule parser.
- Add ordered draft/resolution state machine.
- Enable Home arbitrary-text parsing and the `Describe` bar label.
- No LLM dependency or call.

**Gate:** representative typed meals resolve to the same nutrition as manual selection;
unknown/missing/ambiguous flows preserve prior items; all provider flags may be off.

### Phase 4 - optional external parser

- Add pinned OpenAI-compatible client dependency and Gemini adapter.
- Add adapter conformance tests, consent UI, disclosure, timeouts, and redaction.
- Use external parsing only for unresolved syntax.
- Roll out to one explicitly consented user, then the other if desired.

**Gate:** disabled/unconsented/revoked paths construct no client; malformed and
prompt-injected output cannot add nutrition/IDs or bypass bounds; provider outage falls
back locally.

### Phase 5 - local voice

- Add lazy optional `faster-whisper` dependency and supervised worker.
- Run and record the host/model benchmark.
- Add PTB nonblocking/WAITING integration, cleanup, cancellation, and shutdown.
- Enable for one opted-in user, then both.

**Gate:** stalled/hung transcription does not block the other user, reminders, Undo, or
shutdown; every temp-file path cleans up; audio is never sent externally.

### Phase 6 - recipe variants — DONE

Shipped as `/recipe duplicate <recipe> <new-key>` (alias `copy`), out of sequence because
it is an independent track needing no migration.

- Implement atomic Duplicate Recipe: reject normalized-name collisions, copy recipe and
  all ingredients in one transaction, then allow normal edits.
- A duplicate is independent; duplicating a prior duplicate is allowed.

**Gate:** forced failure leaves no partial recipe/ingredients; the source recipe is
unchanged; new recipe revision and owner checks pass.

### Phase 7 - Supplements

- Ship v13 and the independent check-off/streak handler.
- Hide the Home heading until enabled.
- No reminder integration in this release.

**Gate:** active-name, deactivate/reactivate, timezone boundary, activity-period,
owner-isolation, and streak denominator tests pass; diet totals are unchanged.

## 13. Module boundaries

Keep orchestration out of the existing large handler/database modules:

- `bot/services/meal_logging.py` - draft finalization, Repeat, current-value replay, Undo
  orchestration;
- `bot/services/catalog.py` - effective source resolution, edits, revisions, imports;
- `bot/providers/food/base.py` and `usda.py` - typed lookup adapter;
- `bot/parsing/schema.py` and `deterministic.py` - strict candidates and local rules;
- `bot/providers/llm/base.py` and `gemini.py` - optional parser adapter;
- `bot/voice/manager.py` and worker entry point - bounded transcription;
- `bot/handlers/home.py`, `describe.py`, and `supplements.py` - UI/state handling; and
- focused database repository methods that own short transactions.

Handlers pass user intent, IDs, expected revisions, and `MutationSource`; services
re-read authoritative rows, derive nutrition/provenance server-side, and write
atomically. Provider modules never receive a database connection.

Declare every direct runtime dependency explicitly and pin it. Optional features lazy
import their dependency and remain disabled with a clear health/status reason when it is
unavailable.

## 14. Verification matrix

### Baseline/data integrity

- Exact mixed-draft reproduction preserves every item.
- Command, typed, tap, parser, catalog, recipe, adhoc, and Repeat paths write the expected
  structured history.
- Aggregate equality/overflow and item-count boundary tests.
- Save failure then retry; stale Cancel; callback replay.
- Direct cross-user source/preference tests roll back header, children, audit, and receipt.

### Migration/provenance

- Upgrade a populated v8 fixture through v12 with all IDs, timestamps, macros,
  preferences, portions, aliases, ingredients, and provider fields preserved.
- Forced failure at every migration step leaves the previous version intact.
- Current-version corrupted/mis-stamped fixtures fail verification.
- Snapshot/revision update/delete triggers reject mutation.
- Complete import removal deactivates unseen identities; partial lookup never does.
- Provider refresh under active override leaves effective values stable and marks rebase.
- Two edits with one expected revision yield one success and one conflict.
- Personal import retains origin after edit; manual personal food has none.
- Both-null, both-set, missing, inactive-new-selection, and cross-owner ingredients fail.
- Completed logs retain the exact provider/local revisions after every source edit.

### Repeat/instant/Undo

- Repeat structured, legacy/no-child, freetext, adhoc, archived, revised, and
  provenance-v0 meals.
- Repeat copies meal type and snapshots; current-value replay shows changed values.
- Same update replay creates one log; distinct Repeat messages create distinct logs.
- Rapid stale inline callbacks cannot double-save the same draft.
- Undo after an intervening log deletes only the receipt ID; replay is harmless.

### Routing and UX

- Use real `Application.process_update()` for every reserved label, greeting, arbitrary
  text, command, callback, and voice update in every listed state.
- Verify labels never become subject, exercise, habit, search, amount, calories, macros,
  or food text.
- Exact clock tests at 03:59/04:00, 10:59/11:00, 15:59/16:00, and 21:59/22:00.
- Feature-disabled and stale-keyboard behavior removes/replaces controls safely.
- `/start`, `/menu`, `/help`, and `/cancel` preserve their specified semantics.

### Parser/provider/privacy

- Strict rejection: extra fields, booleans, NaN/infinity, invalid null combinations,
  units, lengths, response bytes, candidate count, and item count.
- Duplicate items and source order preserved.
- Missing amount, ambiguity, and unknown states never call the LLM.
- Only unresolved segments are sent; deterministic results survive provider failure.
- Timeout, 429, 5xx, authentication, malformed JSON, missing nutrients, duplicates, and
  withdrawal behavior.
- A stalled lookup/model request does not block the other user, `/recent`, Undo,
  reminders, `/cancel`, or consent revocation; queue-limit behavior is explicit.
- Disabled/unconsented/unauthorized paths instantiate no external client.
- Consent revocation/version change during a request discards the result.
- New provider/parser/voice logs contain no keys, meal/search/transcript text, URLs,
  response body, or user ID; broader legacy logging cleanup is outside this feature.

### Voice

- Reject duration/declared size before download and enforce downloaded-byte limit.
- One active + one queued globally; second job per user and queue overflow reject.
- Stalled work leaves `/recent`, Undo, reminders, and the other user's logging responsive.
- Cancel queued/running, hard timeout, late result, model failure, send failure, restart,
  and shutdown all suppress stale transitions and clean temp files.
- Hung worker is terminated/recreated and shutdown finishes within the declared bound.

### Supplements/recipes

- Duplicate Recipe collision, nested duplicate, forced rollback, and ingredient
  preservation.
- Supplement duplicate active names, owner FK, deactivate/reactivate periods, local-date
  boundaries, streak/adherence denominators, and no diet impact.

Every phase must keep `python -m pytest -q` green. Before release, install into a clean
environment, run `pip check`, and run the existing dependency audit procedure.

## 15. Rollout, backup, and rollback

Before the first schema release, add a polling-free migration/verification command so
the bot never begins accepting updates merely to run migrations:
`python scripts/migrate_db.py --database <path> --verify`.

For each production migration:

1. disable/revoke the supervisor start and stop polling;
2. wait for handlers, DB writes, provider calls, and voice jobs to drain, then shut down
   cleanly;
3. create a verified SQLite online backup outside the repository with
   `scripts/backup_db.py`;
4. restore that backup to a temporary path and run schema/integrity verification;
5. require free temporary disk of at least three times the live DB plus WAL size;
6. run migrations and smoke tests with polling still disabled;
7. start the new compatible binary with new feature flags off;
8. verify `/start`, `/diet`, a write+targeted Undo, schema version, and sanitized row
   counts; then
9. enable the phase flag for one user before both.

Planned-cutover RPO is zero: no writes are accepted between the verified backup and the
decision to resume polling. Restore RTO target is 30 minutes and must be demonstrated in
the rehearsal.

Rollback rules:

- Before any post-cutover write: stop, restore the verified backup, and run the previous
  schema-compatible release.
- After any post-cutover write: do not restore the old backup and lose accepted data;
  disable the feature in the current compatible binary or roll forward.
- An older binary that rejects the newer schema is not a rollback option.
- Down-conversion is not an option unless a separately implemented and tested tool exists.
- Preserve failed/suspect DB and sidecars for diagnosis; never delete them during restore.

Update `docs/backup_runbook.md` and `docs/operations_runbook.md` with the drain command,
polling-free migration command, flags, RPO/RTO result, disk measurement, voice shutdown,
and persistent-keyboard removal procedure.

## 16. Documentation and definition of done

Update:

- README: Home/bar behavior, exact Repeat versus current-value replay, default quantities,
  builder semantics, lookup provenance, shared-edit impact, parser fallback, voice, and
  suggestion reset/forget commands;
- `/help` and `/settings`: distinct command behavior, consent/revocation, provider
  disclosure, feature status, and data-retention wording;
- `.env.example`: global flags, exact provider/model settings, keys, limits, and safe
  defaults;
- privacy text: Telegram transport, host-local transcription, lookup disclosure, external
  unresolved-text disclosure, and free-tier warning; and
- operations docs described in the rollout section.

The implementation is complete only when:

- Phase 0 gates pass before feature work;
- each migration and rollback rehearsal passes on a production-shaped copy;
- every phase-specific acceptance test passes;
- full clean-environment tests and dependency checks pass;
- no feature is exposed before its flag, dependency, schema, consent, and operational
  gates pass; and
- the final deployed behavior matches this document without an unresolved design marker.

## 17. Explicitly deferred

- IFCT until licensing is resolved.
- FTS until measured catalog size/search latency justifies it.
- Automatic provider-wide retirement from partial USDA/OFF search results.
- LLM-generated nutrition or automatic historical free-text catalog matching.
- Durable in-progress meal drafts across process restart.
- Recipe ancestry/lineage.
- Supplement reminder integration.
- More external providers until adapter conformance, terms, attribution, and operational
  gates pass.

## 18. Primary implementation references

- [USDA FoodData Central API guide](https://fdc.nal.usda.gov/api-guide/)
- [Open Food Facts API documentation](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/)
- [Gemini API terms](https://ai.google.dev/gemini-api/terms)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [python-telegram-bot ConversationHandler](https://docs.python-telegram-bot.org/en/v22.8/telegram.ext.conversationhandler.html)
- [faster-whisper requirements](https://github.com/SYSTRAN/faster-whisper#requirements)
