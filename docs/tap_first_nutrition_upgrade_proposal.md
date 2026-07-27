# Tap-First Logging and Nutrition Catalog Upgrade Proposal

**Status:** Proposal for Claude review  
**Project:** Ledger Telegram bot  
**Date:** 2026-07-27  
**Scope:** Product and architecture proposal only; no implementation is included

## Executive summary

Ledger should become **tap-first**, while retaining slash commands as a
power-user and recovery interface. Its existing nutrition subsystem should be
extended into a searchable shared catalog rather than replaced.

The codebase already provides:

- Inline-keyboard menus and guided conversations.
- Per-user foods, named portions, recipes, and recipe ingredients.
- Exact quantity parsing and unit normalization.
- Internal calorie and protein/carbohydrate/fat scaling.
- Nutrient snapshots in completed diet logs.
- Versioned SQLite migrations and a strong automated test suite.

The most important missing capability is **structured item-level meal history**.
The current `diet_logs` table stores a free-text food description and aggregate
nutrients, but not the selected food or recipe, entered quantity, or unit.
Without that provenance, suggestions such as “usual breakfast” or “normal apple
quantity” would require unreliable parsing of presentation text.

The recommended sequence is:

1. Make existing category buttons enter guided flows directly.
2. Add tap-based selection for existing saved foods, recipes, and portions.
3. Introduce structured meal items beneath the current meal record.
4. Rank suggestions from completed structured logs.
5. Add a curated, versioned nutrition catalog and indexed search.

## Goals

- Let a user perform common logging through taps instead of remembering command
  grammar.
- Calculate calories and macros internally after the user selects an
  unambiguous food and quantity.
- Suggest likely foods and quantities based on the user’s completed history.
- Support multi-item meals without inflating meal counts.
- Preserve historical nutrient values when catalog data changes.
- Retain strict per-user isolation, replay safety, and existing analytics.
- Keep commands available for quick entry, unusual inputs, administration, and
  recovery.

## Non-goals

- Removing all slash commands.
- Using machine learning in the initial suggestion system.
- Inferring nutrition from ambiguous free text.
- Guessing mass/volume/count conversions.
- Automatically matching historical free-text logs to catalog foods.
- Importing an entire external food dataset before validating the user
  experience.
- Providing medical advice or treating nutrition values as exact clinical
  measurements.

## Current-state findings

### The menu is only partially interactive

`bot/keyboards.py` defines an inline main menu, but Study, Gym, and Diet menu
callbacks in `bot/handlers/start.py` currently reply with instructions to send
`/study`, `/gym`, or `/diet`. They do not enter the corresponding guided flow.

The three conversation handlers also use command-only entry points:

- `bot/handlers/study.py`
- `bot/handlers/gym.py`
- `bot/handlers/diet.py`

Handler registration in `bot/main.py` places the conversation handlers before
the global menu callback. This provides a clean extension point: each
conversation can accept its matching menu callback as an entry point before the
generic menu handler sees it.

### Diet already has part of the desired flow

The guided diet flow already provides a meal-type keyboard. After choosing a
meal, however, the user must type:

1. A food description.
2. Calories or `/skip`.
3. Protein, carbohydrates, and fat or `/skip`.

A catalog reference such as `food:apple 1 medium` or
`recipe:curry 1 serving` already bypasses manual calorie and macro entry.

### Nutrition calculation is already implemented

The existing schema includes:

- `foods`
- `food_portions`
- `recipes`
- `recipe_ingredients`

`bot/nutrition.py` already:

- Normalizes metric unit aliases.
- Converts kilograms to grams and litres to millilitres.
- Keeps mass, volume, count, and serving dimensions separate.
- Resolves explicit food-specific portions.
- Scales food nutrients from a configured basis.
- Aggregates recipe nutrients.
- Propagates unknown nutrient values conservatively.
- Rounds once when finalizing a completed diet log.

This arithmetic should be reused unchanged wherever possible.

### Current history cannot reliably power suggestions

`diet_logs` stores:

- Meal type.
- Free-text food description.
- Aggregate calories.
- Aggregate protein, carbohydrates, and fat.
- Timestamp.

It does not store:

- Food or recipe identity.
- Entered quantity and unit.
- Resolved canonical quantity.
- Individual item nutrient contributions.
- Catalog provider or revision.

`ResolvedCatalogDietEntry` similarly returns display text and calculated
nutrients but discards structured source and quantity information after
calculation.

`list_foods()` sorts alphabetically. There is currently no favorite, recent,
frequency, or meal-specific ranking mechanism.

### A diet row currently represents a meal

Daily analytics and routine helpers treat each `diet_logs` row as one meal.
Therefore, saving each selected food as its own `diet_logs` row would make a
three-item meal appear as three meals. Structured items must sit beneath one
meal header.

### Conversation state is transient

Guided-flow state currently lives in `context.user_data`, and conversations
time out after 300 seconds. No application persistence is configured. A short
single-food flow can continue using this model, but a multi-item meal draft may
need durable storage so a restart does not lose it.

## Proposed user experience

### Primary diet flow

```text
Home
  → Log Food
  → Suggested meal type, with an option to change it
  → Pinned/recent/frequent foods
  → Quantity or named portion
  → Nutrition preview
  → Add another item / Save meal / Edit / Cancel
  → Confirmation
  → Log another / Home / Undo
```

### Food suggestion screen

Show four to six high-confidence choices, followed by escape paths:

- Pinned foods.
- Recent foods for the selected meal type.
- Recency-weighted frequent foods for that meal type.
- Search.
- More suggestions.
- Custom food.
- Back and Cancel.

Do not record every viewed or tapped suggestion as a preference signal. Learn
from completed saved meals. An explicit pin or default quantity is a stronger
signal and should rank first.

### Quantity screen

For the selected item, show:

- Configured named portions such as `1 medium`, `1 bowl`, or `1 serving`.
- The user’s recent quantities for that exact item.
- Suitable metric choices within the item’s supported dimension.
- Custom amount.
- Back and Cancel.

Never offer an inferred cross-dimension conversion. A `piece`, cup, or
millilitre option is valid only when the catalog provides an explicit mapping
for that food.

### Nutrition preview

Before persisting, show:

- Food and preparation/brand variant.
- Entered quantity.
- Calculated calories.
- Protein, carbohydrates, and fat.
- Any unavailable nutrient fields.
- Add another item.
- Save meal.
- Edit quantity.
- Remove item.
- Cancel.

The final Save action is the mutation boundary. Repeated delivery of the same
final callback should remain idempotent.

### Other logging categories

The same tap-first approach can later be applied to:

- Recent study subjects and usual durations.
- Recent gym exercises, sets, reps, and weights.
- Habit setup actions.

Diet should be the first implementation because it receives the largest
benefit from structured catalog data and quantity presets.

## Command policy

Commands should remain supported but move out of the primary path.

Reasons to retain them:

- Experienced users can log faster with a single message.
- Commands are useful for unusual or custom input.
- `/cancel`, `/help`, `/recent`, and `/undo` are important recovery tools.
- Commands simplify operations, testing, and troubleshooting.
- Existing users and documentation remain backward compatible.

The intended product behavior is **tap-first, not tap-only**.

## Proposed data model

Exact naming can be adjusted during implementation. The important boundary is
between shared reference data, private user definitions, meal headers, and
structured meal items.

### Keep existing private food tables

Keep `foods`, `food_portions`, `recipes`, and `recipe_ingredients` user-owned.
Do not make `user_id` nullable and do not weaken their current ownership
constraints.

These tables continue to support:

- Personal foods.
- Home recipes.
- User corrections and overrides.
- Portions that are meaningful only to one user.

### Add a shared catalog

Suggested tables:

#### `catalog_foods`

- `id`
- `provider`
- `provider_food_id`
- `provider_revision`
- `display_name`
- `name_key`
- `brand`
- `preparation_state`
- `category`
- `basis_amount`
- `basis_unit`
- `calories`
- `protein_g`
- `carbs_g`
- `fat_g`
- `is_active`
- `created_at`
- `updated_at`

The combination of provider and provider food ID should be unique. Provider
revision must be retained so future changes are auditable.

#### `catalog_aliases`

- `id`
- `catalog_food_id`
- `alias`
- `alias_key`
- Optional locale or language.

Aliases support regional naming without duplicating nutrient profiles.

#### `catalog_portions`

- `id`
- `catalog_food_id`
- `name`
- `name_key`
- `base_amount`
- `base_unit`
- Provider provenance where applicable.

All portions must resolve explicitly to the food’s canonical dimension.

### Add structured meal items

Suggested `diet_log_items` fields:

- `id`
- `user_id`
- `diet_log_id`
- `item_order`
- `source_type`: shared catalog, user food, or user recipe.
- `source_id`
- `source_provider`
- `source_revision`
- `display_name_snapshot`
- `entered_amount`
- `entered_unit`
- `resolved_base_amount`
- `resolved_base_unit`
- `calories_snapshot`
- `protein_g_snapshot`
- `carbs_g_snapshot`
- `fat_g_snapshot`
- `created_at`

Owner-preserving foreign keys should prevent attaching one user’s item to
another user’s meal. Deleting a meal through Undo should cascade to its items.

Keep the existing nutrient columns on `diet_logs` as meal totals. Existing
analytics can remain compatible while item-aware features use the child rows.

### Optional user preferences

Suggested `user_food_preferences` fields:

- `user_id`
- `source_type`
- `source_id`
- `is_pinned`
- `default_amount`
- `default_unit`
- `hidden_from_suggestions`
- `updated_at`

This table is optional for the first structured-logging migration. Suggestions
can initially be derived directly from completed `diet_log_items`.

### Optional durable drafts

If multi-item composition must survive restarts, add a small draft model:

- `meal_drafts`
- `meal_draft_items`

Alternatively, ship a single-item tap MVP using `context.user_data`, measure
abandonment, and add durable drafts only when multi-item logging is introduced.

## Catalog lookup and storage

For a small curated MVP, reference tables may live in the main database.

For a substantial external dataset, prefer a separate read-only SQLite
catalog and connection because:

- Ledger currently serializes reads and writes through one connection lock.
- Large search queries should not delay user-log mutations.
- A full catalog would unnecessarily enlarge every ledger backup.
- Reference data can be rebuilt from its source, while the ledger contains
  irreplaceable user history.

Use indexed normalized keys and SQLite FTS for name and alias search. Telegram
callback payloads should contain short numeric identifiers rather than food
names or provider metadata.

## Suggestion algorithm

Begin with transparent deterministic ranking.

Candidate generation:

1. Pinned foods.
2. Foods previously completed for the selected meal type.
3. Recently completed foods from other meal types.
4. Curated fallback foods when the user has little history.

Example ranking components:

- Explicit pin bonus.
- Same-meal-type frequency over a bounded recent window.
- Recency decay.
- General frequency.
- Optional day-of-week bonus after enough history exists.

Tie-breaking must be deterministic, for example by normalized display name and
stable ID.

Do not introduce a materialized statistics table until query measurements show
it is needed. For the expected personal-ledger scale, indexed aggregation over
structured log items should be sufficient.

Provide controls to:

- Pin or unpin an item.
- Hide an item from suggestions.
- Reset learned suggestions.
- Disable personalized ranking if desired.

## Nutrition source strategy

### USDA FoodData Central

USDA FoodData Central is the recommended general starting point because its
data is public-domain/CC0 and available through both an API and downloadable
datasets.

Official documentation:

- <https://fdc.nal.usda.gov/api-guide/>
- <https://fdc.nal.usda.gov/data-documentation/>
- <https://fdc.nal.usda.gov/download-datasets/>

Import only a curated subset for the initial release. Store the FoodData
Central ID, data type, and release/revision.

### Indian Food Composition Tables

IFCT is highly relevant for Indian foods, but the official publication states
that storing or reproducing its data electronically to create a product
requires prior written permission from the National Institute of Nutrition.
Do not ingest IFCT into the product until permission and usage terms are
resolved.

Official publication:

- <https://www.nin.res.in/ebooks/IFCT2017_16122024.pdf>

### Open Food Facts

Open Food Facts may supplement packaged or barcoded products. Its database is
community-contributed, carries accuracy/completeness disclaimers, and uses the
Open Database License.

Official documentation:

- <https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/>

If used, retain source attribution and quality indicators. Do not silently mix
community-contributed values with curated analytical data.

### User-owned foods remain essential

No external source will fully cover:

- Home recipes.
- Local restaurant portions.
- Household-specific preparation.
- Regional dishes.
- User corrections.

The existing private catalog must remain a first-class source.

## Nutrition and ambiguity rules

“Food + quantity + unit” is sufficient only after selecting a canonical food
variant. The UI must distinguish, where relevant:

- Raw versus cooked.
- Drained versus undrained.
- Brand or product.
- Recipe or preparation method.
- Edible versus as-purchased quantity.
- Fortified versus unfortified.

Continue the existing behavior of allowing unknown nutrient fields. Do not
invent values to make every meal appear complete.

Store source-provided calories when available. Any fallback calculation from
macronutrients should be clearly defined, consistently applied, and marked as
derived.

## Migration and backward compatibility

The current latest schema version is 5. This work should use one or more new,
ordered, atomic migrations.

Requirements:

- Upgrade a valid v5 database without losing rows.
- Keep old `diet_logs` valid with zero child items.
- Do not parse or auto-link historical `food_items` text.
- Add owner-preserving foreign keys and appropriate indexes.
- Ensure `PRAGMA foreign_key_check` and integrity checks pass.
- Fail closed on malformed schemas or unsupported newer versions.
- Back up the production database before migration.
- Keep the existing quick `/diet` grammar working.

If catalog data is stored separately, its schema/version should be checked at
startup independently of the user-ledger migration version.

## Handler and service design

Refactor command entry functions into shared flow starters rather than
duplicating command and callback implementations.

Illustrative structure:

```python
async def begin_diet_flow(update, context, *, source: str) -> int:
    ...

diet_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("diet", diet_command, filters=AUTH_FILTER),
        CallbackQueryHandler(diet_menu_callback, pattern=r"^menu_diet$"),
    ],
    ...
)
```

The shared starter must use `update.effective_message` or an explicitly passed
message because callback updates do not populate `update.message`.

Reuse existing patterns for:

- Callback authorization.
- Owner-scoped database queries.
- Expected message-ID validation.
- Stale keyboard retirement.
- Bounded Telegram output.
- Mutation receipts.
- Failure-consistent state cleanup.

Keep callback data short and use numeric stable IDs. Never trust an ID solely
because it came from a button; every lookup must still include the acting
`user_id`.

Avoid continuing to grow `bot/database.py` and
`bot/handlers/catalog.py` indefinitely. New catalog search, meal-item
operations, and ranking logic should be separated into focused repository or
service modules where practical.

## Privacy considerations

Food history can reveal sensitive health and behavioral patterns.

Requirements:

- Keep suggestion history per-user.
- Derive patterns from completed logs rather than recording every exploratory
  tap.
- Provide reset, hide, and personalization-disable controls.
- Do not send history to an external recommendation service.
- Document that application-level user isolation does not hide data from the
  machine or database operator.
- Avoid logging selected foods, quantities, or nutrition details at INFO level.

## Reliability considerations

- A final confirmation must precede the database mutation.
- Repeated final callbacks must be idempotent.
- Stale or tampered buttons must not mutate data.
- A callback belonging to another user must fail closed.
- After persistence, failure to send a confirmation must not invite an
  accidental duplicate; `/recent` should remain the reconciliation path.
- Undo must delete the meal header and its structured items atomically.
- Catalog updates must not rewrite nutrient snapshots in completed logs.
- Unknown nutrients must continue to be distinguishable from numeric zero.

## Testing requirements

### Routing and UX

- `/start` includes the main menu.
- Study, Gym, and Diet category taps enter their guided flows directly.
- Commands still enter the same flows.
- Menu callbacks do not escape or corrupt another active conversation.
- Back, Skip, Cancel, and stale-button behavior are covered.
- Handler-order tests exercise real `Application.process_update()` routing.

### Nutrition selection

- Tapping a food and portion produces the same nutrients as direct resolver
  calls.
- Metric aliases normalize correctly.
- Cross-dimension quantities fail unless an explicit mapping exists.
- Unknown nutrient fields remain unknown.
- Food and recipe snapshots remain unchanged after later catalog edits.

### Structured meals

- Multiple items create one `diet_logs` meal and multiple ordered child rows.
- Meal totals equal the sum of known child snapshots under the documented
  missing-value policy.
- Analytics count the meal once.
- Undo deletes the meal and all child rows.
- Replayed final updates do not duplicate the meal or its items.

### Personalization

- Rankings are isolated by user.
- Meal-type filtering works.
- Pins rank above inferred preferences.
- Recency/frequency ordering and tie-breaking are deterministic.
- Quantity suggestions come only from the same selected item.
- Abandoned taps do not affect ranking.
- Reset and hide controls work.

### Security and Telegram constraints

- Cross-user catalog, preference, and item IDs fail closed.
- Tampered callback IDs cannot expose another user’s data.
- Callback payloads remain within Telegram limits.
- Long lists paginate and messages stay within Telegram limits.

### Migrations

- Fresh databases reach the new latest version.
- Valid v5 databases upgrade successfully.
- Old free-text diet rows remain intact and unlinked.
- Forced migration failure rolls back fully.
- Foreign-key and integrity checks pass.
- An older binary refuses a newer schema.

## Rollout plan

### Phase 1: Direct category taps

- Show the main menu from `/start`.
- Make Study, Gym, and Diet taps enter existing flows.
- Add visible Skip, Back, and Cancel controls where practical.
- Keep commands and current database schema unchanged.

**Success criterion:** common guided logs can begin without typing a slash
command.

### Phase 2: Tap existing saved nutrition

- Show existing user foods and recipes after meal selection.
- Add portion and quantity buttons.
- Preview internally calculated nutrition.
- Support a single selected food initially if necessary.

**Success criterion:** a user can log an existing saved food through taps without
entering calories or macros.

### Phase 3: Structured meal items

- Add `diet_log_items`.
- Extend resolved entries with source identity and resolved quantity.
- Aggregate multiple items under one meal.
- Update Undo, Recent, summaries, and tests.

**Success criterion:** completed meals retain queryable item identity and
quantity while preserving historical nutrient snapshots.

### Phase 4: Personalized suggestions

- Add pins and deterministic ranking.
- Suggest recent quantities and named portions.
- Add reset/hide controls.

**Success criterion:** top suggestions and quantities are derived exclusively
from the acting user’s completed structured history.

### Phase 5: Curated shared catalog

- Select and document permitted data sources.
- Import a small, relevant catalog with provenance.
- Add indexed name/alias search.
- Retain user foods and recipes as equal or higher-priority results.

**Success criterion:** users can find common foods without manually entering
their nutrient profiles.

### Phase 6: Expansion based on usage

- Evaluate durable meal drafts.
- Expand catalog coverage.
- Add packaged-food/barcode support only if useful.
- Apply recent-value suggestions to Study and Gym.

## Acceptance criteria

The upgrade is complete when:

- A new user can start common logging from visible buttons.
- Existing commands remain backward compatible.
- A known food and quantity calculate nutrition internally.
- A multi-item meal is stored as one meal with item-level snapshots.
- Suggestions are owner-scoped, deterministic, resettable, and based on
  completed history.
- Nutrition records include provider and revision provenance.
- Historical totals do not change when catalog data changes.
- Unknown nutrients are clearly represented.
- No implicit cross-dimension conversions are introduced.
- Migrations are atomic and covered by rollback tests.
- Undo, Recent, summaries, charts, routines, and reminders remain correct.
- The full existing and new test suite passes.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Ambiguous food names | Require selection of a clear preparation/brand variant |
| Inaccurate or incomplete source data | Retain provider, revision, quality indicators, and unknown values |
| Licensing violations | Review each source before ingestion; obtain IFCT permission if desired |
| Inflated meal counts | Store foods as child items beneath one meal header |
| Historical totals changing | Snapshot nutrients into every completed item and meal |
| Poor early suggestions | Use pins and curated fallbacks during cold start |
| Sensitive behavioral tracking | Learn only from completed logs; provide reset/disable controls |
| Large catalog slowing Ledger | Use indexed search and a separate read-only database/connection |
| Restart losing a long draft | Persist multi-item drafts or initially ship a short single-item flow |
| Stale or tampered callbacks | Reuse owner checks, message validation, and short stable IDs |

## Questions for Claude’s review

Please review this proposal with particular attention to:

1. Is `diet_logs` as a meal header plus `diet_log_items` the correct backward-
   compatible boundary?
2. Should structured item logging and the shared catalog ship in separate
   migrations?
3. Should the first tap-based release support one food per meal or introduce
   multi-item drafts immediately?
4. Is a separate read-only catalog database justified from the first curated
   import, or only after the dataset reaches a measured size?
5. Should recipe ingredients continue using current live food definitions for
   future logs, or be pinned to explicit source revisions?
6. What is the simplest robust callback-routing design for direct menu entry
   into the existing conversation handlers?
7. Which database/service boundaries should be introduced before adding more
   catalog and ranking behavior?
8. Are there additional privacy, licensing, or nutrition-provenance risks that
   should block implementation?
9. Which phase should be reduced or deferred to keep the first release small
   and reversible?

## Verification performed for this proposal

- Reviewed the current interaction, conversation, catalog, nutrition, database,
  migration, analytics, and test code.
- Ran the full automated test suite against the current working tree:
  `448 passed`.
- No implementation files were changed as part of the review.

