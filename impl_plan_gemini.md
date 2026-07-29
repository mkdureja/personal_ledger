# Phase 1a + Phase 1b - Final Gemini Execution Plan

**Status:** ✅ **Executed — all units (0, A1-A3, B1-B5) are built.** Kept as the
as-built reference for this surface; it is not pending work.  
**Project:** Ledger Telegram bot  
**Plan date:** 2026-07-28 (executed 2026-07-29/30)  
**Code baseline:** `hardening/review-fixes` at `a0a7204`  
**Database before and after this phase:** v8  
**Schema migrations in this phase:** none  

This document supersedes the earlier partial `impl_plan_gemini.md`. It is the complete
execution contract for Phase 1a and Phase 1b. Do not substitute a different handler
layout, transaction boundary, callback shape, feature flag, or product behavior without
updating `implementation_plan.md` and obtaining review.

Four requirements here were built differently on purpose; see
`implementation_plan.md` §0.1 for the reasoning. In short: the current-value digest
(§10.5) is a readable field signature rather than SHA-256 canonical JSON, the §13.9
performance gate and the multi-connection isolation *test harness* were dropped, and
Repeat/Undo/current-value orchestration sits in `bot/database.py` +
`bot/handlers/receipts.py` instead of `bot/services/meal_logging.py`. Everything else
below matches the code.

## 1. Required outcome

Deliver both releases:

1. **Release A - dark routing and rollback compatibility**
   - close the remaining Phase 0 structured-history gap;
   - install every Home label, active-state, stale-callback, voice-rejection, and
     keyboard-removal path;
   - keep Phase 1 disabled for production users; and
   - prove the real dispatcher routing matrix before exposing a persistent keyboard.
2. **Release B - Phase 1a Home plus all Phase 1b local features**
   - Home snapshot and `[Meal] [Repeat]`;
   - inferred/changeable meal type;
   - private food/recipe default quantities and two-tap quick logging;
   - exact Repeat, targeted Undo, and confirmed `Use current values`;
   - catalog-history ranking;
   - suggestion pagination; and
   - draft edit, remove, and change-quantity controls.

At the Phase 1b gate:

- a private food or recipe with a valid default is logged in two taps from Home
  (`Meal`, then the source);
- Repeat is one tap from Home;
- the same Telegram update never creates a duplicate;
- a distinct second press is a distinct user action;
- builder selections never create hidden writes;
- every receipt Undo targets the displayed meal ID; and
- local handler/service/SQLite p95 is below 500 ms on the deployment host, excluding
  Telegram network time.

## 2. Scope and non-negotiable decisions

### 2.1 Included

- Greeting-driven Home using `hi`, `hello`, or `hey`.
- A two-message Home response: inline section actions, then quick actions.
- Exact reply labels `Meal` and `Repeat`.
- `Describe` reserved everywhere but hidden and disabled.
- `LOCAL_TZ` meal inference:
  - Breakfast: 04:00 through 10:59;
  - Lunch: 11:00 through 15:59;
  - Dinner: 16:00 through 21:59;
  - Snack: 22:00 through 03:59.
- Home quick mode and the existing explicit multi-item builder as distinct modes.
- Defaults for private `food` and `recipe` only.
- Exact snapshot Repeat with the original meal type and a new timestamp.
- Targeted 24-hour Undo.
- `Use current values` as a separate preview-and-confirm action.
- Active catalog foods drawn only from that user's completed catalog history.
- Suggestion pagination and deterministic ordering.
- Current-draft replace, remove, and change-quantity controls.
- No-op/tombstone mutation receipts for exact replay behavior.
- User-scoped, default-off rollout flags.

### 2.2 Deferred

- `Describe` parsing, arbitrary-text meal parsing, and external parsing.
- Voice download or transcription.
- USDA/Open Food Facts lookup.
- Catalog pin/hide/default preferences; v8 preferences allow only private food/recipe.
- `/suggestions forget` and `/suggestions reset-all`; these need the v9 watermark.
- Adhoc items, v9-v12 provenance, recipe variants, and Supplements.
- Any schema migration.

### 2.3 Invariants

1. Nutrition is resolved locally from authoritative rows.
2. Completed header/item snapshots are never re-resolved by exact Repeat.
3. Private source reads/writes are acting-user scoped.
4. Header, children, optional default update, and mutation receipt commit atomically
   where the UI presents them as one action.
5. No feature mutation, including user/settings bootstrap, occurs through a Phase 1
   handler when its user-scoped flag is disabled.
6. Ephemeral conversation callbacks require owner, current message, and revision where
   present. Durable receipt callbacks instead require owner, exact target, feature
   eligibility, and the target's own validity/age; an intervening receipt does not
   invalidate them.
7. Each receipt-backed create/no-op operation (`diet_repeat`, `diet_quick`,
   `diet_current`) has one permanent outcome per `(telegram_update_id, operation_key)`,
   including empty Repeat and replay-after-Undo tombstones. Targeted Undo itself has no
   mutation receipt: it is idempotent, returning `DELETED` first and
   `ALREADY_REMOVED` thereafter.
8. A builder tap changes only the visible in-memory draft until Save.
9. A Home quick tap creates one complete single-item meal or writes nothing.
10. Provider/network work is absent from this phase.
11. SQLite remains at `PRAGMA user_version = 8`.

## 3. Execute these build units in order

| Unit | Work | Required gate before continuing |
|---|---|---|
| 0 | Close the remaining Phase 0 structured-history gap | Phase 0 regressions and the full suite pass |
| A1 | Domain types, flags, normalized text filters, sequential updates, rollback-safe preference/reset writes | Config/filter/preference compatibility tests pass |
| A2 | All-state routing, Home surface, disabled-label behavior, keyboard removal | Complete real-dispatcher matrix passes with production flags off |
| A3 | Deploy Release A dark | Real-client removal rehearsal passes; retain this artifact as rollback target |
| B1 | Transaction primitives, exact Repeat, targeted Undo, receipts | DB failure/replay/isolation suite passes |
| B2 | Quick/builder modes and private defaults | Two-tap/default/reset/builder suite passes |
| B3 | Current-value replay | Preview/repair/confirm/replay suite passes |
| B4 | Catalog ranking, pagination, and draft controls | Ranking/paging/edit stale-callback suite passes |
| B5 | Full verification, benchmark, docs, Release B pilot | Every Definition of Done item passes |

Do not expose Release B controls while any earlier gate is red.

## 4. Feature flags and rollout behavior

### 4.1 Configuration

Add to `bot/config.py` and `.env.example`:

```text
PHASE1_ENABLED_USER_IDS=
HOME_KEYBOARD_MODE=off
HOME_KEYBOARD_PILOT_USER_IDS=
```

Contracts:

- `PHASE1_ENABLED_USER_IDS` is a comma-separated subset of `ALLOWED_USER_IDS`.
  Empty means Phase 1 Home and fast mutations are disabled for everyone.
- `HOME_KEYBOARD_MODE` is exactly one of `off`, `pilot`, `on`, or `remove`.
- `HOME_KEYBOARD_PILOT_USER_IDS` is a comma-separated subset of both
  `ALLOWED_USER_IDS` and `PHASE1_ENABLED_USER_IDS`. It is used only in `pilot`.
- Malformed IDs, duplicates, unknown modes, or non-subset IDs fail startup.
- Configuration is read at startup; changing it requires a supervised restart.
- `.env.example` contains no real Telegram IDs.

Effective keyboard behavior:

| Mode | Behavior |
|---|---|
| `off` | Never send the persistent keyboard. Home/compatibility responses remove a stale one. |
| `pilot` | Send it only to `HOME_KEYBOARD_PILOT_USER_IDS`; remove it for other authorized users. |
| `on` | Send it to every Phase 1-enabled user; remove it for other authorized users. |
| `remove` | Send `ReplyKeyboardRemove` to every authorized user on Home/compatibility synchronization. |

Every Home mutation calls `phase1_enabled_for(user_id)` immediately before its DB write.
A stale callback received after flag-off/restart performs no mutation.

### 4.2 Two-release sequence

**Release A production configuration**

```text
PHASE1_ENABLED_USER_IDS=
HOME_KEYBOARD_MODE=off
HOME_KEYBOARD_PILOT_USER_IDS=
```

All Release A routing, removal, and final-shape compatibility handlers are installed;
Release B feature handlers are not claimed complete. Disabled labels are consumed
without mutation and carry `ReplyKeyboardRemove`. Integration tests may enable test
users through patched config; production users remain dark.

**Release B pilot**

1. Add one authorized user to `PHASE1_ENABLED_USER_IDS`.
2. Set `HOME_KEYBOARD_MODE=pilot` and add only that user to the pilot list.
3. Restart, run the pilot smoke tests, and observe sanitized errors/latency.
4. Add the second user to both `PHASE1_ENABLED_USER_IDS` and
   `HOME_KEYBOARD_PILOT_USER_IDS`, restart, and complete second-user acceptance.
5. Only then set `HOME_KEYBOARD_MODE=on`; clear
   `HOME_KEYBOARD_PILOT_USER_IDS`, restart, and verify both users.

**Rollback**

1. Set the exact safe combination:

   ```text
   PHASE1_ENABLED_USER_IDS=
   HOME_KEYBOARD_MODE=remove
   HOME_KEYBOARD_PILOT_USER_IDS=
   ```

2. Restart the current compatible binary.
3. Have every previously enabled user send a greeting or `/keyboard hide`; verify the
   client keyboard disappears. Rollback acceptance remains pending until each known
   user has performed and confirmed this synchronization; no proactive Telegram
   message is implied.
4. If a binary rollback is required, roll back only to Release A, which contains label
   compatibility and removal handlers.
5. Never restore an older DB merely to disable Phase 1; Phase 1 adds no schema and
   accepted ledger rows must be preserved.

## 5. Build Unit 0 - finish the Phase 0 gate first

### 5.1 Route every new manual/resolved Diet write through structured history

Modify `bot/handlers/diet.py`:

- In `/diet <meal> food:<key>|recipe:<key> <quantity>`, replace `log_diet()` with:

```python
await db.log_diet_with_items(
    user.id,
    meal_type,
    [entry.as_item()],
    source=mutation_source(update),
)
```

- For ordinary `/diet <meal> <description> [calories/macros]`, write one `freetext`
  child with null source/quantity fields and the entered nutrient snapshot.
- For the legacy guided `FOOD_ITEMS -> CALORIES -> MACROS` path with no existing draft,
  also write one `freetext` child. Existing database rows with zero children remain
  untouched and are the legacy Repeat case.
- Keep the documented command grammar and confirmations unchanged.

Do not reinterpret ordinary descriptions as known sources.

### 5.2 Phase 0 gate

Add regressions for:

- `food:apple 200 g` and a recipe reference each writing exactly one structured child;
- manual command/guided input writing one freetext child;
- provider/source/quantity fields preserved;
- same-update replay creating one header and one child;
- cross-owner/missing private references writing nothing; and
- confirmation-delivery failure followed by update replay creating no duplicate.

Then require:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Also assert `PRAGMA user_version = 8` and rerun the existing schema/cross-owner Phase 0
tests. Stop if this gate fails.

## 6. Domain and service boundaries

### 6.1 New files

Create:

- `bot/services/__init__.py`
- `bot/services/meal_logging.py`
- `bot/meal_models.py`
- `bot/nutrition_resolution.py`
- `bot/callback_data.py`
- `bot/handlers/home.py`

`bot/meal_models.py` owns dependency-neutral enums/dataclasses used by the database,
service, and handlers. `database.py` must not import a module that imports
`DatabaseManager` at runtime.

`bot/callback_data.py` owns strict unsigned lowercase base-36 integer encode/decode
helpers and rejects empty, signed, non-canonical, out-of-range, or non-ASCII tokens.

`bot/nutrition_resolution.py` is Telegram- and database-class-free. Move/refactor
`ResolvedCatalogDietEntry`, `resolve_food_diet_entry`,
`resolve_recipe_diet_entry`, and `resolve_catalog_food_entry` out of
`bot/handlers/catalog.py` into this module. Keep/refactor the async
`resolve_catalog_diet_entry(db, user_id, reference, tokens)` lookup/orchestration wrapper
outside the pure module; after its DB reads it calls `resolve_catalog_food_entry`.
`catalog.py`, Diet, the service, and transaction-scoped database methods all call these
single pure implementations; do not copy resolver logic into `database.py`.

Required types:

```python
from __future__ import annotations

class DietEntryMode(StrEnum):
    QUICK = "quick"
    BUILDER = "builder"

class DietItemSourceType(StrEnum):
    FOOD = "food"
    RECIPE = "recipe"
    CATALOG = "catalog"
    FREETEXT = "freetext"

class QuickMealStatus(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    REPLAYED_REMOVED = "replayed_removed"
    QUANTITY_REQUIRED = "quantity_required"
    QUANTITY_INVALID = "quantity_invalid"
    DEFAULT_INVALID = "default_invalid"
    SOURCE_UNAVAILABLE = "source_unavailable"

class RepeatStatus(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    EMPTY = "empty"
    REPLAYED_REMOVED = "replayed_removed"

class CurrentCommitStatus(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    REPLAYED_REMOVED = "replayed_removed"
    REVIEW_REQUIRED = "review_required"
    SOURCE_MEAL_REMOVED = "source_meal_removed"

class UndoStatus(StrEnum):
    DELETED = "deleted"
    ALREADY_REMOVED = "already_removed"
    EXPIRED = "expired"

class CurrentValueDecision(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVE_CURRENT = "resolved"
    KEEP_ORIGINAL = "keep_original"
    REMOVE = "remove"

class CurrentValueIssueCode(StrEnum):
    SOURCE_MISSING = "source_missing"
    QUANTITY_MISSING = "quantity_missing"
    QUANTITY_INVALID = "quantity_invalid"
    RECIPE_EMPTY = "recipe_empty"

@dataclass(frozen=True)
class DefaultQuantity:
    amount: float
    unit: str

@dataclass(frozen=True)
class NutrientValues:
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None

@dataclass(frozen=True)
class SourceNutrients:
    calories: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None

@dataclass(frozen=True)
class DietLogItemInput:
    source_type: DietItemSourceType
    source_id: int | None
    source_provider: str | None
    source_revision: str | None
    display_name: str
    entered_amount: float | None
    entered_unit: str | None
    resolved_base_amount: float | None
    resolved_base_unit: str | None
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None

@dataclass(frozen=True)
class DietHeaderSnapshot:
    meal_id: int
    user_id: int
    meal_type: str
    food_items: str
    nutrients: NutrientValues
    logged_at_utc: datetime | None

@dataclass(frozen=True)
class DietItemSnapshot:
    child_id: int
    user_id: int
    meal_id: int
    item_order: int
    source_type: DietItemSourceType
    source_id: int | None
    source_provider: str | None
    source_revision: str | None
    display_name: str
    entered_amount: float | None
    entered_unit: str | None
    resolved_base_amount: float | None
    resolved_base_unit: str | None
    calories: int | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    created_at_utc: datetime | None

@dataclass(frozen=True)
class MealReceipt:
    header: DietHeaderSnapshot
    items: tuple[DietItemSnapshot, ...]

@dataclass(frozen=True)
class QuickMealResult:
    status: QuickMealStatus
    receipt: MealReceipt | None

@dataclass(frozen=True)
class RepeatResult:
    status: RepeatStatus
    receipt: MealReceipt | None

@dataclass(frozen=True)
class FoodPortionSnapshot:
    portion_id: int
    name: str
    name_key: str
    base_amount: float

@dataclass(frozen=True)
class FoodResolutionSource:
    food_id: int
    user_id: int
    name: str
    base_unit: str
    basis_amount: float
    nutrients: SourceNutrients
    is_active: bool
    portions: tuple[FoodPortionSnapshot, ...]

@dataclass(frozen=True)
class RecipeIngredientSnapshot:
    ingredient_id: int
    food_id: int
    base_amount: float
    display_amount: float
    display_unit: str
    food: FoodResolutionSource

@dataclass(frozen=True)
class RecipeResolutionSource:
    recipe_id: int
    user_id: int
    name: str
    yield_amount: float
    yield_unit: str
    is_active: bool
    ingredients: tuple[RecipeIngredientSnapshot, ...]

@dataclass(frozen=True)
class CatalogResolutionSource:
    catalog_id: int
    provider: str
    provider_food_id: str
    provider_revision: str | None
    display_name: str
    base_unit: str
    basis_amount: float
    nutrients: SourceNutrients
    is_active: bool
    portions: tuple[FoodPortionSnapshot, ...]

@dataclass(frozen=True)
class CurrentValueSourceBundle:
    header: DietHeaderSnapshot
    items: tuple[DietItemSnapshot, ...]
    foods: tuple[FoodResolutionSource, ...]
    recipes: tuple[RecipeResolutionSource, ...]
    catalogs: tuple[CatalogResolutionSource, ...]

@dataclass(frozen=True)
class CurrentValueIssue:
    source_child_id: int
    code: CurrentValueIssueCode

@dataclass(frozen=True)
class CurrentValueItemProposal:
    source_child_id: int
    decision: CurrentValueDecision
    original: DietItemSnapshot
    persisted_item_order: int | None
    proposed: DietLogItemInput | None
    issue: CurrentValueIssue | None

@dataclass(frozen=True)
class CurrentValuePreview:
    source_meal_id: int
    meal_type: str
    items: tuple[CurrentValueItemProposal, ...]
    original_totals: NutrientValues
    proposed_totals: NutrientValues
    delta: NutrientValues
    digest: str
    can_save: bool

@dataclass(frozen=True)
class CurrentValueCommitResult:
    status: CurrentCommitStatus
    receipt: MealReceipt | None
    preview: CurrentValuePreview | None

@dataclass(frozen=True)
class UndoResult:
    status: UndoStatus
    meal_id: int
    deleted_header: DietHeaderSnapshot | None
```

Construct these models at the DB boundary; no `Mapping[str, Any]` crosses into the
service. `DietLogItemInput` is the strict new-write type and enforces source/null
combinations and current bounds. `DietItemSnapshot` is a permissive typed historical
row: missing quantities and units that no longer resolve remain representable so the
service can emit `QUANTITY_MISSING`/`QUANTITY_INVALID` and offer Keep/Remove. Snapshots
normalize parseable timestamps to aware UTC and use `None` for a malformed legacy
timestamp without rejecting a row that is valid under v8, so legacy Repeat stays exact.
Use deterministic ordering. The result type, not a free-form string, controls every
handler branch. Do not pass provider payloads, SQLite rows, or Telegram objects into
the service.

For `CurrentValueItemProposal`, `RESOLVE_CURRENT` carries a strict `proposed` item;
`KEEP_ORIGINAL` carries `proposed=None` and persists fields from `original`; `REMOVE`
carries `proposed=None` and no persisted order; and `UNRESOLVED` is non-saveable. The
digest serializer follows those same four cases.

### 6.2 `bot/services/meal_logging.py`

Own:

- `infer_meal_type(local_time: datetime.time)`;
- server-side resolution of stored private defaults;
- quick-versus-builder orchestration;
- receipt eligibility and rendering data;
- current-value preview, repair decisions, canonical digest, and confirmation
  revalidation; and
- pure total/delta helpers using the existing conservative null propagation.

Its `MealRepository` `Protocol` repeats the exact public signatures declared in
sections 7.2-7.6 (`repeat_last_meal`, default set/clear, `create_quick_meal`,
`delete_meal_if_recent`, `get_current_value_bundle`, and
`commit_current_value_meal`). It exposes no generic SQL/row method. The module must not
import Telegram or `DatabaseManager`.

`infer_meal_type` uses half-open boundaries:

```text
[04:00, 11:00) breakfast
[11:00, 16:00) lunch
[16:00, 22:00) dinner
otherwise snack
```

Handlers pass `now_local().time()`. Never use the host's naive local clock.

## 7. Database transaction and repository contracts

### 7.1 Explicit immediate transactions

Extend the existing context manager without changing existing callers:

```python
@asynccontextmanager
async def _write_operation(
    self,
    *,
    begin_immediate: bool = False,
) -> AsyncIterator[None]:
```

When `begin_immediate=True`, after acquiring `_conn_lock` and before yielding:

1. explicitly check `conn.in_transaction`; if true, raise a fail-closed
   `RuntimeError` (never use `assert`, which disappears under `python -O`);
2. execute `BEGIN IMMEDIATE`;
3. commit on success; and
4. roll back on every `BaseException`.

The leading receipt/source `SELECT` must occur after `BEGIN IMMEDIATE`; the existing
implicit transaction begins only on a write and is insufficient for these read-modify-
write operations.

Also add:

```python
@asynccontextmanager
async def _read_snapshot(self) -> AsyncIterator[None]:
    ...
```

It acquires `_conn_lock`, explicitly rejects an already-active transaction with
`RuntimeError`, executes `BEGIN` before the first read, commits on success, and rolls
back on every `BaseException`. Public multi-query snapshot readers call only locked
helpers inside this context; they do not call `_query_one/_query_all` methods that would
reacquire the lock. `get_current_value_bundle` uses this context so one header, child,
and source bundle is from one SQLite snapshot even if another connection is writing.

Add locked helpers:

```python
_get_mutation_receipt_locked(source, user_id, operation_key)
_record_receipt_locked(...)
_insert_diet_meal_locked(
    user_id,
    meal_type,
    items: Sequence[DietLogItemInput | DietItemSnapshot],
    logged_at,
)
_get_owned_meal_locked(user_id, meal_id)
```

`_get_mutation_receipt_locked` returns `entity_type` and `entity_id`, validates owner,
and never returns another user's outcome.

`_insert_diet_meal_locked` uses the same nutrition finalizer, meal-item cap, source-owner
checks, header display bounding, and child insertion rules as `log_diet_with_items`.
Refactor rather than duplicate those rules. A `DietLogItemInput` is a strict new
resolved write. A `DietItemSnapshot` is accepted only for an explicit
`KEEP_ORIGINAL` current-value decision after validating
`snapshot.user_id == user_id` and source-meal ownership; copy only its persisted item
payload fields, never old IDs/parent/order/timestamps. The helper assigns consecutive
`item_order` from the new sequence, matching `persisted_item_order` in the preview/
digest. Exact Repeat remains the separate path that preserves historical `item_order`.
This trusted snapshot-copy path permits a missing historical quantity to remain missing
without weakening validation for new inputs.

### 7.2 Exact Repeat

Add:

```python
async def repeat_last_meal(
    user_id: int,
    source: MutationSource,
) -> RepeatResult
```

Production Home calls `ensure_user()` first and always supplies a real
`MutationSource`. Execute entirely inside `_write_operation(begin_immediate=True)`:

1. Read receipt `(source.update_id, "diet_repeat")`.
2. If it is `diet_repeat_empty/0`, return `EMPTY`.
3. If it points to an owner-scoped existing meal, load that exact meal/items and return
   `REPLAYED`.
4. If it points to a deleted meal, return `REPLAYED_REMOVED`; never recreate it.
5. Unexpected receipt types fail closed.
6. If there is no receipt, select:

```sql
SELECT id, meal_type, food_items, calories,
       protein_g, carbs_g, fat_g
FROM diet_logs
WHERE user_id = ?
ORDER BY logged_at DESC, id DESC
LIMIT 1
```

7. If absent, record `operation_key='diet_repeat'`,
   `entity_type='diet_repeat_empty'`, `entity_id=0`, and return `EMPTY`. This makes a
   replay remain empty even if another meal is logged later.
8. Insert a new header with a new UTC timestamp and verbatim original meal type,
   description, calories, and macros.
9. Copy children with an explicit column list. Exclude old `id`, `diet_log_id`, and
   `created_at`; substitute the new parent/current creation time; preserve:
   `item_order`, `source_type`, `source_id`, `source_provider`, `source_revision`,
   `display_name`, entered/resolved amounts/units, calories, and macros.
10. Owner-scope the child source query by both `user_id` and old `diet_log_id`, ordered
    by `item_order, id`.
11. Record the `"diet_repeat"` receipt as `entity_type='diet'`.
12. Return the new exact header/items.

Do not call `log_diet_with_items()` for Repeat because it recomputes the header display
and totals. A failure at any point leaves no new header, child, or receipt.

Mutation receipts survive Undo and serve as the tombstone for replay.

### 7.3 Targeted Undo

Add:

```python
async def delete_meal_if_recent(
    user_id: int,
    meal_id: int,
    *,
    now_utc: datetime | None = None,
) -> UndoResult
```

Production defaults `now_utc` to an aware UTC `datetime`; tests may inject one. Inside
`_write_operation(begin_immediate=True)`:

1. select only `WHERE id=? AND user_id=?`;
2. missing/fabricated/cross-owner all return `ALREADY_REMOVED`;
3. parse naive timestamps as UTC and normalize aware timestamps to UTC;
4. a null/malformed timestamp returns `EXPIRED` because recency is unprovable;
5. expire only when `now_utc - logged_at > 24 hours`; exactly 24 hours remains eligible,
   matching the current `/undo` boundary;
6. delete by exact owner/id and inspect `rowcount`;
7. concurrent disappearance returns `ALREADY_REMOVED`;
8. child rows cascade; and
9. receipts are not deleted.

Return the deleted header for a bounded receipt message.

### 7.4 Private default quantities

Use the existing v8 `default_amount/default_unit` columns. Add:

```python
async def set_default_quantity(
    user_id: int,
    source_type: Literal["food", "recipe"],
    source_id: int,
    quantity: DefaultQuantity,
) -> DefaultQuantity

async def clear_default_quantity(
    user_id: int,
    source_type: Literal["food", "recipe"],
    source_id: int,
) -> bool
```

`set_default_quantity` is the validation boundary. Inside
`_write_operation(begin_immediate=True)` it:

1. accepts only `food` or `recipe`;
2. loads the active acting-user source and, respectively, its current portions or
   current recipe ingredients/foods;
3. passes `[amount, unit]` through the existing source-specific nutrition resolver;
   this is what validates finite/positive/bounded amounts, metric aliases, named food
   portions, recipe yield units, and empty recipes;
4. stores the resolver's normalized `entered_amount/entered_unit` together;
5. preserves pin/hide; and
6. returns the exact stored pair.

Missing and another user's source fail identically. No public handler writes raw
default columns or performs a separate validate-then-write sequence.

`clear_default_quantity` is intentionally different: it addresses only the acting
user's `(user_id, source_type, source_id)` preference key and does not require the
source to remain active or even exist. It nulls both columns, preserves pin/hide, and
deletes the row only if both flags are false. It can therefore remove an invalid
default after source archival/deletion without inspecting or changing another user's
row. A missing preference is a harmless `False` result.

Partial legacy pairs are invalid and never used automatically. Every automatic use
re-resolves the stored pair against current authoritative rows inside the meal's write
transaction.

Add:

```python
async def create_quick_meal(
    user_id: int,
    meal_type: str,
    source_type: Literal["food", "recipe", "catalog"],
    source_id: int,
    *,
    quantity: DefaultQuantity | None,
    set_as_default: bool,
    source: MutationSource,
) -> QuickMealResult
```

It executes in `_write_operation(begin_immediate=True)`, uses operation key
`"diet_quick"`, and checks replay before reading a source or changing a preference. A
successful receipt is `entity_type='diet'` with the created meal ID.

Validation/resolution matrix inside that same transaction:

| Source | Quantity | Default behavior |
|---|---|---|
| private `food` | Active acting-user food plus current portions; use supplied quantity or a complete stored default | `set_as_default` requires a supplied quantity and stores exactly the resolver's entered pair |
| private `recipe` | Active acting-user recipe plus current ingredients/foods; use supplied quantity or a complete stored default | same |
| shared `catalog` | Active catalog identity plus current portions; supplied quantity is required | `set_as_default` must be false |

A missing stored/default-required quantity returns `QUANTITY_REQUIRED`; an invalid
supplied quantity returns `QUANTITY_INVALID`; and a partial or no-longer-resolvable
stored default returns `DEFAULT_INVALID`. Each writes nothing and never picks a
fallback. The method accepts no arbitrary pre-resolved item, so a default cannot be
paired with a different source or quantity.
It resolves one `DietLogItemInput`, calls the shared locked meal insert helper, applies
the optional private default, and records the receipt. `Log + set default` either
commits the preference, header, child, and receipt together or commits none.

`freetext` cannot enter this API. `Type it` follows the Phase 0 structured freetext
completion path with operation key `"diet_log"` and has no default action.

If replay points to a meal removed by Undo, return `REPLAYED_REMOVED` and do not
recreate it or reapply a default. Unexpected receipt types fail closed.

### 7.5 Preference reset and pin/hide rules

This subsection is Release A rollback compatibility work. Implement and test it in A1,
before preserving the Release A artifact; do not wait for the Phase 1b default UI.

Change `/suggestions reset` storage behavior:

- clear `is_pinned` and `hidden`;
- preserve `default_amount/default_unit`;
- delete only now-empty rows with no default; and
- return the count of pin/hide preferences actually cleared.

Replace toggle writes with explicit desired-state writes:

```text
dpin_<owner>_<ui_revision>_<0|1>
dhide_<owner>_<ui_revision>_<0|1>
```

These are A1 callback families. Their numeric owner/revision tokens use the strict
base-36 helper defined in section 9.2. Introduce `diet_ui_revision`, initialize it for
the baseline `/diet` tap flow, and add it to Diet cleanup in A1; do not defer that key
plumbing to B work.

The handler validates owner, current message, and revision, calls one atomic setter,
then increments the server revision before attempting the replacement render. A
Telegram render failure does not roll back the stored preference. Setting pin to `1`
clears hidden; setting hide to `1` clears pinned; either true write requires an active
acting-user private source. Setting either to `0` changes only that acting user's
preference key and remains allowed after archival/deletion so a stale preference can be
cleared. Repeated delivery therefore converges on the same value instead of toggling
back. Hide wins and invalid combined states are not newly created. Add same-update and
rapid double-delivery regressions for both actions.

### 7.6 Current-value commit

The preview and confirmation repository contracts are:

```python
async def get_current_value_bundle(
    user_id: int,
    source_meal_id: int,
) -> CurrentValueSourceBundle | None

async def commit_current_value_meal(
    user_id: int,
    source_meal_id: int,
    decisions: Mapping[int, CurrentValueDecision],
    expected_digest: str,
    *,
    source: MutationSource,
) -> CurrentValueCommitResult
```

`get_current_value_bundle` loads one owner-scoped header, its children ordered by
`item_order, id`, and all current food/portion, recipe/ingredient/food, and catalog/
portion rows needed to resolve those children. It performs those reads under
`_conn_lock` in one short read transaction, so the service receives one consistent
typed bundle. Children retain their database child IDs; `item_order` is display order,
not identity.

`commit_current_value_meal` executes the following entirely inside
`_write_operation(begin_immediate=True)`:

1. check `(source.update_id, "diet_current")` before loading/re-resolving any source;
2. for an owner-scoped `entity_type='diet'` receipt, return its existing meal as
   `REPLAYED`, or `REPLAYED_REMOVED` if targeted Undo removed it;
3. fail closed on every other receipt type;
4. reload the owner-scoped source header/children and the complete current source bundle
   using locked helpers; if the source meal is gone, return `SOURCE_MEAL_REMOVED`;
5. validate that every decision key is an actual child ID from that source meal, apply
   the decisions, and call the same pure current-value resolver/digest function used by
   preview;
6. if any decision is `UNRESOLVED`, no proposed/kept item remains, or the digest differs
   from `expected_digest`, return `REVIEW_REQUIRED` with the new preview and write
   nothing; retained issue metadata on a `KEEP_ORIGINAL`/`REMOVE` decision is not an
   unresolved repair;
7. insert the displayed items with the original meal type through the shared locked
   helper;
8. record `operation_key='diet_current'`, `entity_type='diet'`, and the new meal ID; and
9. commit, returning `CREATED`.

Do not call `log_diet_with_items()` for this action because it hard-codes operation key
`"diet_log"`. The receipt check, authoritative re-resolution, digest comparison,
header/children, and receipt are one serialized transaction. Record no receipt for
`REVIEW_REQUIRED`, so the newly rendered preview can be confirmed with a later update.

## 8. Phase 1a Home and routing

### 8.1 Normalized control text

In `bot/handlers/common.py`, add a pure normalizer:

```python
def normalize_control_text(text: str) -> str:
    return text.strip().casefold()
```

Do not apply Unicode compatibility normalization or collapse internal whitespace in
Phase 1; the authoritative contract is trimmed, case-folded, whole-string matching.

Whole normalized strings only are controls:

```python
HOME_ACTIONS = {
    "meal": "meal",
    "repeat": "repeat",
    "describe": "describe",
}
HOME_WORDS = {"home"}
GREETINGS = {"hi", "hello", "hey"}
```

At baseline `a0a7204`, no persistent Home labels have previously shipped, so do not add
emoji aliases. If a label is ever shipped and later changed, move its normalized form to
an explicit `LEGACY_HOME_ACTIONS` set before deploying the change.

Create custom PTB message filters for:

- exact Meal;
- any Home action;
- greetings/Home; and
- all active-flow control text.

`hi there`, `meal prep`, and other longer strings are ordinary text.

### 8.2 Real Diet entry points

Do not register the existing `diet_menu_entry` as a `MessageHandler`; it assumes
`update.callback_query`.

Add `diet_home_entry(update, context)` to `diet_conv_handler.entry_points` through:

```python
MessageHandler(AUTH_FILTER & MEAL_LABEL_FILTER, diet_home_entry)
```

It:

1. calls `conversation_available(update, context, "diet")` before any flag check or DB
   call, so an active Study/Gym/Habit/Diet flow receives the active-flow hint and keeps
   ownership of the update;
2. checks `phase1_enabled_for(user_id)` before `ensure_user()` or any other DB call;
3. when disabled, sends compatibility guidance plus `ReplyKeyboardRemove` and returns
   `ConversationHandler.END`;
4. ensures the user/settings exist;
5. activates Diet;
6. sets `diet_entry_mode = DietEntryMode.QUICK`;
7. infers from `now_local().time()`;
8. stores `diet_meal_type`, `diet_choice_page=0`, and `diet_ui_revision=0`; and
9. renders `FOOD_CHOICE` with the inferred meal type visible.

In A2 add `diet_entry_mode` and `diet_choice_page` to every Diet cancel, timeout, and
failure cleanup path; `diet_ui_revision` is already covered by A1.

Keep `/diet` and `menu_diet` as `BUILDER` entry points. Their normal meal-type step
remains. The existing in-builder `Log another` loop also remains `BUILDER`.

Add receipt entry points:

```text
mr_more_<owner>
mr_current_<owner>_<meal_id>
```

`log_another_from_receipt` is an authorized Diet entry point that validates the owner,
checks conversation availability before the flag/any DB call, then checks feature
eligibility, chooses `QUICK`, infers the current meal, and enters `FOOD_CHOICE`. If the
availability guard rejects it, this entry handler itself answers and retires the receipt
markup; when the flag is also off it additionally sends keyboard removal. It does not
rely on fallthrough to a global handler.

`current_values_entry` is an authorized Diet entry point that validates owner and meal
ID, checks conversation availability before the flag/any DB call, then checks feature
eligibility, activates Diet, builds the current-value review, and enters
`CURRENT_VALUES_REVIEW`. It uses the same no-DB active-flow/flag-off retirement helper
before returning.

Global stale receipt handlers remain after the ConversationHandlers for callbacks not
claimed by an entry/state handler, including timed-out Diet state and Release A. Both
the entry rejection path and global stale path answer and retire safely; neither assumes
the other will run.

Release A already installs inert global compatibility handlers for every final Release B
family, before any B feature is implemented: `dpage_*`, `dchangemeal_*`, `dmanage_*`,
all `dq_*`, all `dd_*`, revised `dadd_*`/`dsave_*`, `dqty_*`, `dedit_*`, `dremove_*`,
all `cv_*`, and all `mr_*`. It also retains retirement for pre-A decimal payloads,
including `dpin_<owner>`, `dhide_<owner>`, `dadd_<owner>`, `dsave_<owner>`, and every
other baseline `_DIET_TAP_RE` shape. In Release A these handlers only answer, remove
markup best-effort, send compatible removal/guidance where relevant, and return no
conversation state; they perform no DB read/write. Release B registers real handlers
before these unchanged fallbacks.

### 8.3 Home surface

`bot/handlers/home.py` owns one authorized late text router, one authorized Home voice
router, and receipt callbacks. All are private-chat filtered.

The text router runs after every ConversationHandler and command handler:

1. If `active_conversation_flow(context)` is set, respond with the active-flow hint and
   perform no Home mutation. This is defense in depth; state handlers should normally
   have consumed the input.
2. Compute `phase1_enabled_for(user_id)`. If false, exact Home actions, `Home`, and
   greetings receive compatibility guidance to use `/menu` plus
   `ReplyKeyboardRemove`; arbitrary other text receives no Phase 1 surface. No snapshot
   query or mutation occurs.
3. When enabled, a greeting or `Home` calls `show_home`.
4. `Repeat` calls exact Repeat only after rechecking the flag.
5. `Describe` says it is not enabled, performs no parsing/network/DB action, and
   synchronizes/removes the keyboard.
6. `Meal` reaching this router is an internal routing failure; log a sanitized warning,
   send `/diet` guidance, and do not try to set a conversation state.
7. Other text shows Home plus `Use Meal or /diet`; it is never stored as Diet data.

`show_home` computes `today = today_local()` exactly once and displays:

- Diet: meal count and calorie total, visibly marking incomplete calories;
- Study: total minutes;
- Gym: exercise count;
- Habits: checked/active count.

Use existing owner-scoped DB helpers plus active/checked habit reads. Escape and bound
the Telegram first name and every dynamic value.

Send two messages:

1. snapshot with `main_menu_keyboard()`; and
2. short quick-action text with either `home_reply_keyboard()` or
   `ReplyKeyboardRemove`, according to flags.

Never put inline and reply keyboards on one message.

Add:

- `/keyboard hide`: always authorized; sends `ReplyKeyboardRemove` and says
  `Hidden for now; an eligible Home response may show it again`;
- `/keyboard show`: sends the keyboard only when that user is Phase 1/keyboard eligible,
  otherwise sends removal plus current status.

There is no durable per-user hide preference in v8. `hide` means until the next eligible
Home/compatibility synchronization, not forever or until an explicit `show`. These
commands do not alter ledger data or conversations.

### 8.4 Persistent keyboard

Add to `bot/keyboards.py`:

```python
ReplyKeyboardMarkup(
    [["Meal", "Repeat"]],
    resize_keyboard=True,
    is_persistent=True,
    one_time_keyboard=False,
)
```

`Describe` is not rendered. Also add a `ReplyKeyboardRemove` helper.

Disabled/legacy labels are always consumed. When unavailable they make no mutation and
send the currently eligible keyboard or removal. This compatibility path remains in
Release A and every later rollback-compatible binary.

### 8.5 State routing matrix

Register state controls before each broad `filters.TEXT` handler. A handler returning
`None` preserves the PTB state.

For Study:

- `SUBJECT`, `DURATION`, `NOTES`: all Home actions and Home/greetings get the
  active-flow hint; ordinary text retains existing behavior.

For Gym:

- `EXERCISE`, `SETS`, `REPS`, `WEIGHT`: same ordering and behavior;
- `MORE`: existing callback handlers, control interceptor, then a catchall saying
  `Use the buttons or /cancel`.

For Habit setup:

- `ADDING_HABIT`: existing setup callbacks, control interceptor, then ordinary habit
  text.

For Diet, put a Diet-specific `Meal` re-render handler first, then the non-Meal control
interceptor:

| State | Ordinary text after control interception |
|---|---|
| `MEAL_TYPE` | `Use the buttons or /cancel` |
| `FOOD_CHOICE` | `Use the buttons or /cancel` |
| `SEARCH` | Existing catalog query |
| `PORTION_CHOICE` | `Use the buttons or /cancel` |
| `CUSTOM_AMOUNT` | Existing amount parser |
| `QUICK_CONFIRM` | `Use the buttons or /cancel` |
| `CONFIRM_ITEM` | `Use the buttons or /cancel` |
| `DEFAULT_MENU` | `Use the buttons or /cancel` |
| `DEFAULT_AMOUNT` | Existing amount parser in default-management mode |
| `DEFAULT_CONFIRM` | `Use the buttons or /cancel` |
| `CURRENT_VALUES_REVIEW` | `Use the buttons or /cancel` |
| `LOG_ANOTHER` | `Use the buttons or /cancel` |
| `FOOD_ITEMS` | Existing manual food input |
| `CALORIES` | Existing calorie input |
| `MACROS` | Existing macro input |

`Meal` inside any Diet state re-renders that exact current step/draft without changing
the draft, selection, revision, DB, or feature flags. Implement explicit state renderers
rather than an ambiguous common handler:

- retire the previously tracked inline markup best-effort;
- rebuild from authoritative `user_data` plus fresh read-only source lists;
- send a new tracked UI message;
- update `diet_ui_message_id` or `diet_meal_message_id`; and
- return the same state.

If required state data is missing/invalid, fail closed, clear Diet data, end the
conversation, and say the draft expired.

`Repeat`, `Describe`, and Home/greetings in any active flow get:
`Finish this flow or /cancel first`. They do not change the draft or DB.

There are no section-switch reply-text literals in Phase 1. Section changes are existing
commands or `menu_*` callbacks and are guarded separately through
`conversation_available`/the active-flow callback hint.

Every callback-only state also gets a generic non-command text catchall so arbitrary text
cannot fall through to Home.

### 8.6 Voice and section callbacks

Add `filters.VOICE` handlers to every active state before any global Home voice handler.
They respond that voice is not enabled/currently accepted, return the same state, and
never call `get_file` or download content.

At Home, a voice update says voice logging is not enabled and performs no download.
The global voice handler checks the Phase 1 flag before any DB call: when disabled it
sends only `/menu` compatibility guidance plus keyboard removal; when enabled it sends
the voice-not-enabled Home response. Neither branch exposes a snapshot or downloads.

Keep `/start`, `/menu`, and `/help` distinct; they do not write ledger/domain rows or
clear a conversation. Preserve their existing authorized-user/settings bootstrap
behavior, so "read-only" here is about ledger/domain and conversation state, not those
pre-existing idempotent account rows. Update non-Conversation section callbacks such as
`menu_habits` and `menu_analytics` to honor the active conversation marker and show the
active-flow hint. The existing conversation entry points continue using
`conversation_available`.

During any active flow, `/keyboard hide` sends `ReplyKeyboardRemove` and preserves the
state/draft. `/keyboard show` sends the active-flow hint plus removal and also preserves
state/draft; it may show the keyboard only after the flow has ended and the user remains
eligible.

`/cancel` remains the only implicit flow switch and clears every new Diet key.

### 8.7 Handler order and concurrency

Make PTB's ConversationHandler requirement explicit:

```python
ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(False)
```

Group 0 order:

1. Study, Gym, Diet, Habit ConversationHandlers.
2. Existing and new command handlers.
3. Receipt Undo/current stale callbacks.
4. Existing stale/menu/habit/analytics callbacks.
5. Authorized Home text router.
6. Authorized Home voice handler.

Meal is not a global Home handler; it is a real Diet entry point.

## 9. Phase 1b quick mode, defaults, and builder controls

### 9.1 Context keys and states

Append new states without renumbering existing symbolic meanings:

```text
QUICK_CONFIRM
DEFAULT_MENU
DEFAULT_AMOUNT
DEFAULT_CONFIRM
CURRENT_VALUES_REVIEW
```

`diet_ui_revision` was already introduced and added to cleanup in A1.
`diet_entry_mode` and `diet_choice_page` are introduced/cleaned in A2 with the Home
entry. B work keeps those three keys and adds/clears:

```text
diet_edit_index
diet_edit_action
diet_quick_pending_item
diet_default_source_type
diet_default_source_id
diet_pending_default
diet_current_source_meal_id
diet_current_source_child_ids
diet_current_decisions_by_child_id
diet_current_preview
diet_current_digest
diet_current_revision
diet_current_created_at
```

Add the remaining keys to `_CONVERSATION_DATA_KEYS["diet"]`,
`_clear_diet_entry_data`, timeout, and cancel cleanup. In-memory drafts/previews are
intentionally lost on restart; their old callbacks become stale.

### 9.2 Callback contract

Every new or changed ephemeral Diet callback uses these exact payloads:

| Payload | Valid state | Handler result |
|---|---|---|
| `dpage_<owner>_<page>` | `FOOD_CHOICE` | read/rank/clamp and return `FOOD_CHOICE` |
| `dchangemeal_<owner>` | `FOOD_CHOICE` | retire picker and return `MEAL_TYPE` |
| `dmanage_<owner>_<rev>_<f|r>_<source_id>` | `FOOD_CHOICE` | store the validated private source and return `DEFAULT_MENU` |
| `dpin_<owner>_<rev>_<0|1>` | `PORTION_CHOICE` | atomically set desired pin state, increment revision, re-render `PORTION_CHOICE` |
| `dhide_<owner>_<rev>_<0|1>` | `PORTION_CHOICE` | atomically set desired hide state, increment revision, re-render `PORTION_CHOICE` |
| `dq_log_<owner>_<rev>` | `QUICK_CONFIRM` | call quick commit without setting a default; on success end and receipt |
| `dq_default_<owner>_<rev>` | `QUICK_CONFIRM` | call atomic quick commit with `set_as_default=True`; on success end and receipt |
| `dq_amount_<owner>_<rev>` | `QUICK_CONFIRM` | keep no old mutation, reopen its quantity path |
| `dq_cancel_<owner>_<rev>` | `QUICK_CONFIRM` | clear Diet state and end |
| `dd_use_<owner>_<rev>` | `DEFAULT_MENU` | leave management mode and open normal quantity selection without changing the default |
| `dd_edit_<owner>_<rev>` | `DEFAULT_MENU` | prompt for a set/change/repair value and return `DEFAULT_AMOUNT` |
| `dd_clear_<owner>_<rev>` | `DEFAULT_MENU` | clear the acting user's preference key and re-render `DEFAULT_MENU` |
| `dd_back_<owner>_<rev>` | `DEFAULT_MENU` | return to the same `FOOD_CHOICE` page |
| `dd_save_<owner>_<rev>` | `DEFAULT_CONFIRM` | re-resolve/store the pending pair and return `DEFAULT_MENU` |
| `dd_reenter_<owner>_<rev>` | `DEFAULT_CONFIRM` | discard only the pending pair and return `DEFAULT_AMOUNT` |
| `dd_cancel_<owner>_<rev>` | `DEFAULT_CONFIRM` | discard the pending pair and return `DEFAULT_MENU` |
| `dadd_<owner>_<rev>` | `CONFIRM_ITEM` | preserve draft and return `FOOD_CHOICE` |
| `dsave_<owner>_<rev>` | `CONFIRM_ITEM` | persist the exact draft once; on success end and receipt |
| `dqty_<owner>_<rev>_<index>` | `CONFIRM_ITEM` | enter quantity replacement for that structured draft item |
| `dedit_<owner>_<rev>_<index>` | `CONFIRM_ITEM` | enter source replacement for that draft item |
| `dremove_<owner>_<rev>_<index>` | `CONFIRM_ITEM` | remove that exact draft item and re-render |

Every numeric token in this table is an unsigned lowercase base-36 integer, emitted and
parsed by one strict helper (`0-9a-z`, no sign, no whitespace, canonical encoding).
Decode, range-check, and compare the owner before using any other token. Existing
pre-Phase-1 decimal callback families remain accepted only in their existing states or
retired by their legacy stale handlers.

`rev` is `diet_ui_revision`. Increment it whenever a handler intentionally changes the
logical actionable state: page, selected/pending/default data, preference state, or
draft content. A read-only same-state re-render caused by the `Meal` control preserves
the revision even if fresh source reads alter visible choices, and sends a new tracked
message; the old message ID plus fresh source validation is sufficient to invalidate
its callbacks. Every handler is
`@authorized_callback`, uses `fullmatch`, validates embedded owner, current tracked UI
message, the revision whenever that family carries one, valid state data, and Phase 1
eligibility before any DB call when it is a Phase 1-only family. Baseline Builder and
A1 pin/hide handlers remain available with Phase 1 off and must not acquire a Phase 1
gate. `dpage` and `dchangemeal` rely on owner plus the current picker message because
their compact payloads carry no revision. A Phase 1 DB mutation path rechecks the flag
immediately before calling the repository. Payloads carry only identity/action, never
nutrition or raw default values.

Do not feed base-36 callbacks into the legacy `_DIET_TAP_RE`/`int(token)` parser. Add a
shared current-message validator that accepts an already decoded owner: the legacy
wrapper parses its decimal payload and calls it, while Phase 1 handlers parse strict
base 36 and call it. This preserves the meaning of every pre-A numeric callback.

Register the exact anchored family subset from the table on each relevant
state-level `CallbackQueryHandler` inside the single `diet_conv_handler`. Combine all
families into this one allowlist for the global stale Diet handler:

```python
_DIET_PHASE1_CALLBACK_RE = re.compile(
    r"^(?:"
    r"dpage_[0-9a-z]+_[0-9a-z]+|dchangemeal_[0-9a-z]+|"
    r"dmanage_[0-9a-z]+_[0-9a-z]+_[fr]_[0-9a-z]+|"
    r"d(?:pin|hide)_[0-9a-z]+_[0-9a-z]+_[01]|"
    r"dq_(?:log|default|amount|cancel)_[0-9a-z]+_[0-9a-z]+|"
    r"dd_(?:use|edit|clear|back|save|reenter|cancel)_[0-9a-z]+_[0-9a-z]+|"
    r"d(?:add|save)_[0-9a-z]+_[0-9a-z]+|"
    r"d(?:qty|edit|remove)_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+|"
    r"cv_(?:keep|remove)_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+|"
    r"cv_(?:save|cancel)_[0-9a-z]+_[0-9a-z]+"
    r")$"
)
```

The state handlers are registered before generic text/callback catchalls. The global
stale handler is registered after all ConversationHandlers, answers the query, safely
retires its markup, and never mutates or enters a state. Unit-test the maximum possible
signed-64-bit IDs/revision encoded in base 36 for every family as `< 64` UTF-8 bytes.

Preserve the baseline Builder when exposure is off. Change
`diet_save_keyboard(user_id, ..., phase1_enabled, revision)` and register both paths:

- flag off: render the existing decimal `dadd_<owner>`, `dsave_<owner>`, and
  `dcancel_<owner>` payloads; their existing owner/current-message handlers remain
  ungated and Save uses operation key `"diet_log"`;
- flag on: render the revisioned base-36 `dadd_<owner>_<rev>` and
  `dsave_<owner>_<rev>` plus Phase 1 draft controls; these handlers recheck the flag;
- never emit or accept Phase 1 default/paging/edit/current controls in the flag-off
  Builder; stored defaults are ignored and the existing quantity flow is used.

Thus Release A and a Release B binary with empty `PHASE1_ENABLED_USER_IDS` retain a
fully saveable structured `/diet` flow. Pre-A callbacks left on screen after a restart
are retired by compatibility handlers unless they belong to the current tracked
baseline Builder message.

### 9.3 Quick versus builder matrix

For a selected source:

| Mode/source | Valid default | Missing/invalid default |
|---|---|---|
| `QUICK`, private food/recipe | Re-resolve current source and immediately save one item | Ask quantity, then `QUICK_CONFIRM` |
| `BUILDER`, private food/recipe | Re-resolve and append/replace the visible draft; no DB write | Existing quantity flow, then append/replace |
| Either mode, catalog | No default support on v8 | Existing quantity flow |

An invalid/partial default never logs, never silently chooses a fallback, and is shown
as needing repair/removal.

In Quick mode, a valid-default source tap is the mutation update. Remove the source
keyboard only after commit; on a transient failure, re-render it so retry remains
possible. After success, end the Diet conversation and send a receipt.

Handle `QuickMealResult` exhaustively:

- `CREATED`/`REPLAYED`: end and render the returned exact receipt;
- `REPLAYED_REMOVED`: end and report that the original result was undone;
- `QUANTITY_REQUIRED`: write nothing and open the normal quantity flow;
- `QUANTITY_INVALID`: write nothing and reopen the quantity flow with a bounded error;
- `DEFAULT_INVALID`: write nothing and open `DEFAULT_MENU` with Repair/Remove; and
- `SOURCE_UNAVAILABLE`: write nothing, retire the stale source control, and re-render
  `FOOD_CHOICE`.

In Builder mode, default resolution only changes `diet_items`; final Save remains the
single ledger mutation.

### 9.4 Quantity confirmation and default management

When Quick mode resolves a user-selected amount, render `QUICK_CONFIRM`:

- `Log`;
- `Log + set default` for private food/recipe only;
- `Change amount`; and
- `Cancel`.

The pending item is server-created. Callback payloads contain owner/revision/action, not
nutrition. `Log + set default` calls the atomic quick/default repository method.

Add a gear/manage button beside each private food/recipe suggestion. It opens
`DEFAULT_MENU` without adding/logging an item:

- valid default: `Use different amount`, `Change default`, `Remove default`, `Back`;
- no default: `Use different amount`, `Set default`, `Back`;
- invalid/partial default: `Use different amount`, `Repair default`, `Remove default`,
  `Back`.

`DEFAULT_AMOUNT` accepts amount/unit, resolves it against the current owner-scoped
source, then shows `DEFAULT_CONFIRM`. `Save default` writes the resolved entered
amount/unit explicitly. It is never a toggle. `Back` returns to the same suggestion page
without draft mutation.

Catalog rows never render default/pin/hide controls on v8.

### 9.5 Meal inference and Change meal type

Home/receipt Quick entry stores and displays the inferred type. Add
`dchangemeal_<owner>` to the food-choice keyboard.

`change_meal_type`:

1. is `@authorized_callback`;
2. decodes the owner with the strict Phase 1 base-36 parser and validates it plus the
   current `diet_ui_message_id` through the shared current-message helper;
3. retires the old food keyboard;
4. sends `meal_type_keyboard`;
5. records the new prompt as `diet_meal_message_id`; and
6. returns `MEAL_TYPE`.

The selected entry mode and existing draft remain unchanged. Add `changemeal` to:

- the strict Phase 1 callback parser from section 9.2;
- the `FOOD_CHOICE` state handler list; and
- the global stale Diet callback pattern.

Do not add it to legacy `_DIET_TAP_RE`; that parser treats tokens as decimal and must
retain the meaning of already-issued callbacks.

### 9.6 Suggestion pagination

Use `SUGGESTION_PAGE_SIZE = 8`. Extend `food_choice_keyboard` with page metadata and:

```text
dpage_<owner>_<page>
```

Each render re-reads and deterministically ranks candidates, clamps the requested page,
updates `diet_choice_page`, and increments `diet_ui_revision`. Page navigation always
retires the old markup and sends a new tracked picker message; it never edits a page in
place. Existing `dfood_*`/`drecipe_*`/`dcatalog_*` source payloads carry no revision, so
the new `diet_ui_message_id` is what rejects a queued tap from the prior page.
Navigation is owner/current-message checked. Stable ties remain normalized name, source
type, and source ID.

Rows:

- private food/recipe: main selection plus manage/default gear;
- catalog history: main selection only;
- navigation when needed;
- `Change meal type`;
- Search, Type it, and Cancel/Back-to-draft controls.

Do not put the full catalog in suggestions; Search remains the explicit full catalog
entry.

### 9.7 Draft edit, remove, and change quantity

Maintain `diet_ui_revision`, incremented after every append, replace, remove, or quantity
change. Preview callbacks include owner, revision, and item index:

```text
dqty_<owner>_<revision>_<index>
dedit_<owner>_<revision>_<index>
dremove_<owner>_<revision>_<index>
```

For each structured draft item render `Change quantity`, `Replace`, and `Remove`.
Freetext renders `Replace` and `Remove`.

- `Change quantity` keeps the old item until a new amount successfully resolves; then
  atomically replaces that list position in memory.
- `Replace` returns to the source picker with `diet_edit_index`; the next successfully
  resolved item replaces that index rather than appending.
- `Remove` deletes exactly that item. If the draft becomes empty, return to
  `FOOD_CHOICE`; otherwise re-render preview.
- Back during an edit clears edit mode and restores the unchanged preview.
- Missing/archived structured sources offer Keep, Replace, or Remove; they never
  silently discard the snapshot.

Validate UI message, revision, bounds, index, owner, and current source on every callback.
Incrementing the revision invalidates every older draft callback. With at most 20 items,
the keyboard remains below Telegram's 100-button limit.

When Phase 1 is enabled, `diet_save_keyboard` includes the current revision as defined
in section 9.2; its flag-off branch keeps the legacy payload. Write first, then retire
markup. A write failure preserves/re-renders the same draft and revision for retry.

## 10. Receipts, Repeat, Undo, and current-value replay

### 10.1 Receipt contract

Use compact callback data, always below Telegram's 64-byte limit:

```text
mr_undo_<owner>_<meal_id>
mr_more_<owner>
mr_current_<owner>_<meal_id>
```

Receipt numeric tokens use the same strict unsigned lowercase base-36 helper. Register
full-match patterns with `[0-9a-z]+`; do not use permissive prefix matching.

Every Quick, Repeat, and current-value result renders:

- exact meal ID;
- meal type, bounded item summary, and totals;
- `Undo`;
- `Log another`; and
- `Use current values` when the meal has at least one `food`, `recipe`, or `catalog`
  child, even if its live source is now unavailable and will require repair.

All receipt callbacks:

- use `@authorized_callback`;
- full-match and validate the embedded owner against `effective_user.id`;
- recheck `phase1_enabled_for(user_id)` before every DB call; after flag-off they retire
  their actions, synchronize keyboard removal, and make no mutation;
- use the acting user in every DB call;
- answer the callback before lengthy local work;
- escape/bound dynamic text; and
- fail closed on malformed/stale payloads.

Receipts are durable completed-action controls, not ephemeral conversation UI. They do
not require the newest outbound message ID. Their validity is owner plus feature flag
and, where the payload carries one, the exact embedded meal ID and its
operation-specific age/existence. An intervening meal or newer receipt does not
invalidate an older receipt.

Targeted Undo is allowed during an active guided flow because it targets a fixed
completed meal and does not touch conversation state. `Log another` and `Use current
values` are rejected with the active-flow hint when another flow is active.

### 10.2 Repeat handler

At Home, Repeat:

1. checks there is no active flow;
2. checks `phase1_enabled_for(user_id)` before any DB call;
3. ensures the user exists;
4. passes `mutation_source(update)` to `repeat_last_meal`; and
5. handles:
   - `CREATED`: normal receipt;
   - `REPLAYED`: render the same meal receipt without a new row;
   - `EMPTY`: `Nothing to repeat yet`;
   - `REPLAYED_REMOVED`: `That repeated meal was already undone; nothing changed`.

No confirmation and no live source resolution occur. Two distinct Telegram updates
create two repeated meals; a delivery replay does not.

### 10.3 Targeted receipt Undo

`undo_from_receipt` calls `delete_meal_if_recent` and:

- `DELETED`: edits receipt to `Undone`, removes all action buttons;
- `ALREADY_REMOVED`: edits to `Already removed`, removes buttons;
- `EXPIRED`: leaves the meal, replaces actions with an expiry message/no Undo.

An intervening meal does not affect the target. A repeated callback never deletes a
different row. Telegram edit failure is logged with no rollback of the committed DB
outcome.

### 10.4 `Use current values` source rules

This action is visibly distinct from Repeat. It copies the original meal type but
attempts to re-resolve current structured sources:

| `source_type` | Behavior |
|---|---|
| `food` | Owner-scoped active private food plus current portions; use stored entered amount/unit |
| `recipe` | Owner-scoped active recipe plus current ingredients; use stored entered amount/unit |
| `catalog` | Active current catalog identity plus current portions; stamp current provider/revision |
| `freetext` | Carry the original snapshot unchanged |

Phase 1 has no `adhoc` rows. The later post-v12 rerun will add that case.

Preserve item order and duplicates. Null nutrients remain unknown; never turn null into
zero. A header with zero children or an all-freetext meal has no current-value action.

Issue codes:

```text
SOURCE_MISSING
QUANTITY_MISSING
QUANTITY_INVALID
RECIPE_EMPTY
```

If any structured item has an issue, no Save button is offered until the user explicitly
chooses for each issue:

- `Keep original snapshot`; or
- `Remove item`.

If removal leaves no items, saving is disabled. These are repair choices; no automatic
fallback or partial write occurs.

An issue initially has decision `UNRESOLVED`. `Keep original snapshot` changes the
decision to `KEEP_ORIGINAL` and uses the permissive historical snapshot-copy path;
`Remove item` changes it to `REMOVE`. Retain the original issue code as display/digest
metadata after either choice, but it is no longer an unresolved repair. Set
`can_save=True` only when at least one proposed/kept item remains and no item decision is
`UNRESOLVED`. The UI and transactional commit use this same predicate.

### 10.5 Current-value review state

`current_values_entry` loads the owner-scoped source bundle, activates Diet, and stores
only server-created state:

```text
diet_current_source_meal_id
diet_current_source_child_ids
diet_current_decisions_by_child_id
diet_current_preview
diet_current_digest
diet_current_revision
diet_current_created_at
```

Render old -> proposed calories/macros per item and total. Use callback data:

```text
cv_keep_<owner>_<revision>_<child_id>
cv_remove_<owner>_<revision>_<child_id>
cv_save_<owner>_<revision>
cv_cancel_<owner>_<revision>
```

The v8 schema does not require `item_order` to be unique, so neither `item_order` nor a
mutable list position is identity. Each handler validates that the child ID belongs to
the stored ordered source-meal snapshot before applying the decision. Every repair
increments revision and re-renders. Old revisions/current-message mismatches are stale.
The existing 300-second conversation timeout discards the preview.

The canonical digest is SHA-256 of UTF-8 JSON generated with
`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, and
`allow_nan=False`. Encode every persisted real-number field as a canonical decimal
string using `format(Decimal(str(value)).normalize(), "f")`; reject non-finite values
and map every signed zero to `"0"`. IDs remain JSON integers and missing values are
JSON `null`. The payload is:

```text
{
  "meal_type": original canonical meal type,
  "items": [
    {
      "source_child_id": integer,
      "decision": "unresolved" | "resolved" | "keep_original" | "remove",
      "persisted": null for remove, otherwise {
        item_order, source_type, source_id, source_provider, source_revision,
        display_name, entered_amount, entered_unit,
        resolved_base_amount, resolved_base_unit,
        calories, protein_g, carbs_g, fat_g
      },
      "issue_code": null or the stable issue code
    }
  ]
}
```

Items remain ordered by the source meal's `(item_order, child_id)`. Volatile timestamps
and UI metadata are excluded. Both initial preview and transactional confirmation call
one shared digest function; neither handler constructs the payload independently.

On `cv_save`:

1. recheck the Phase 1 flag;
2. pass the source meal ID, child-ID decisions, shown digest, and
   `mutation_source(update)` directly to `commit_current_value_meal`;
3. on `REPLAYED`, clear/end and render the original result receipt; on
   `REPLAYED_REMOVED`, clear/end and report that the original result was undone; do
   either before any new source-based UI;
4. on `REVIEW_REQUIRED`, replace stored server-created preview/digest with the returned
   values, increment revision, and require another confirmation;
5. on `SOURCE_MEAL_REMOVED`, clear state and end without a write;
6. on `CREATED`, consume/clear the preview immediately after commit, before editing
   Telegram markup, end the conversation, and send a new targeted receipt.

The transaction uses operation key `"diet_current"`. A replay returns its original
meal/tombstone even if current sources changed after the first commit.

## 11. Catalog history, ranking, and preferences

### 11.1 Catalog candidates

Extend `get_diet_item_stats` to include:

```sql
source_type IN ('food', 'recipe', 'catalog')
```

Add `get_user_catalog_history(user_id)`:

```sql
SELECT DISTINCT cf.*, cf.display_name AS name
FROM catalog_foods AS cf
WHERE cf.is_active = 1
  AND EXISTS (
      SELECT 1
      FROM diet_log_items AS dli
      JOIN diet_logs AS dl
        ON dl.id = dli.diet_log_id
       AND dl.user_id = dli.user_id
      WHERE dli.user_id = ?
        AND dli.source_type = 'catalog'
        AND dli.source_id = cf.id
  )
ORDER BY cf.name_key, cf.id
```

Never enumerate the full shared catalog in Home suggestions.

### 11.2 Ranking behavior

Refactor `_ranked_choices` so it does not return early when private lists are empty.

When suggestions are enabled:

- rank active private foods, private recipes, and active catalog-history candidates with
  meal-type frequency, recency, overall frequency, then stable name/type/ID ties;
- apply private pin/hide; and
- hide wins over pin.

When suggestions are disabled:

- show active private foods/recipes alphabetically;
- do not show learned catalog-history candidates; and
- do not use frequency/recency.

Catalog rows have no v8 preference/default controls.

### 11.3 Reset behavior

`/suggestions reset` continues to mean only pin/hide reset. Update its user-facing text
to state that saved default quantities are preserved. Do not implement or advertise
`forget`/`reset-all` in this phase.

## 12. Exact file-level work map

### New

- `bot/meal_models.py`
- `bot/nutrition_resolution.py`
- `bot/callback_data.py`
- `bot/services/__init__.py`
- `bot/services/meal_logging.py`
- `bot/handlers/home.py`
- `scripts/benchmark_phase1.py`
- `tests/test_home.py`
- `tests/test_callback_data.py`
- `tests/test_nutrition_resolution.py`
- `tests/test_phase1_routing.py`
- `tests/test_database_repeat.py`
- `tests/test_diet_defaults.py`
- `tests/test_current_values.py`
- `tests/test_suggestion_pagination.py`

### Modify

- `bot/config.py`
  - flags, subset validation, effective feature helpers.
- `bot/database.py`
  - explicit read/write transaction options, typed bundle/receipt helpers, shared meal
    insert helper, Repeat, transaction-scoped pure nutrition resolution for quick/
    defaults/current confirmation, exact Undo, catalog history/stats, reset semantics.
- `bot/handlers/common.py`
  - normalized filters, control interceptors, new Diet cleanup keys.
- `bot/handlers/catalog.py`
  - import the Telegram-free resolution types/functions; retain only handler/UI
    orchestration.
- `bot/handlers/diet.py`
  - Phase 0 structured writes, entry modes/states, state renderers, inference,
    Change meal, defaults, Quick confirmation, pagination, draft controls,
    current-value conversation.
- `bot/handlers/study.py`, `gym.py`, `habits.py`
  - ordered control/voice/callback-state catchalls.
- `bot/handlers/start.py`
  - preserve distinct commands; help text; active-flow guard for section callbacks.
- `bot/handlers/settings.py`
  - reset text/default preservation and Phase 1 status.
- `bot/keyboards.py`
  - reply/removal keyboards, pagination/manage/default/quick/edit/current/receipt
    keyboards.
- `bot/main.py`
  - explicit sequential updates, imports, handler order, commands, stale callbacks.
- `.env.example`
- `README.md`
- `docs/user_guide.html`
- `docs/operations_runbook.md`
- `docs/backup_runbook.md`

Do not edit `bot/migrations.py` or bump `SCHEMA_VERSION`.

## 13. Verification plan

### 13.1 Real dispatcher routing matrix

Use the real `build_application()` and sequential `Application.process_update()`. Do not
limit routing tests to direct handler calls.

For every listed state, test:

- `Meal`, `Repeat`, `Describe`, `Home`, `hi`, `hello`, `hey`;
- ordinary non-command text;
- `/start`, `/menu`, `/help`, `/cancel`;
- voice;
- relevant section callbacks; and
- stale/current/foreign-owner callbacks.

States:

- Diet: `MEAL_TYPE`, `FOOD_CHOICE`, `SEARCH`, `PORTION_CHOICE`, `CUSTOM_AMOUNT`,
  `QUICK_CONFIRM`, `CONFIRM_ITEM`, `DEFAULT_MENU`, `DEFAULT_AMOUNT`,
  `DEFAULT_CONFIRM`, `CURRENT_VALUES_REVIEW`, `LOG_ANOTHER`, `FOOD_ITEMS`, `CALORIES`,
  `MACROS`;
- Study: `SUBJECT`, `DURATION`, `NOTES`;
- Gym: `EXERCISE`, `SETS`, `REPS`, `WEIGHT`, `MORE`;
- Habit: `ADDING_HABIT`.

Assert state and every draft key remain unchanged unless that input is the state's
documented ordinary input. Verify labels never become a subject, exercise, habit,
search, quantity, calories, macros, or food description. Assert voice causes no
`get_file`, download, DB mutation, or external call.

End-to-end dispatcher sequences:

- Home `Meal` enters `FOOD_CHOICE`; a real `dfood_*` callback is consumed by Diet.
- Receipt `Log another` enters inferred Quick `FOOD_CHOICE`.
- Receipt entry is denied during Study/Gym/Habit setup.
- For each Study/Gym/Habit active state with Phase 1 off, `mr_more` and `mr_current`
  are answered/retired by the claiming entry path, send keyboard removal, preserve the
  owning state/draft, and perform zero DB calls.
- Active Habit setup plus `Meal` is consumed by the Diet entry's pre-DB active-flow
  guard, returns the Habit hint, and changes neither Habit state nor any DB row.
- Change meal type goes `FOOD_CHOICE -> MEAL_TYPE -> FOOD_CHOICE`.
- Old source/change buttons become stale after transition.
- `Meal` re-renders each Diet state without draft change.
- Arbitrary text/greetings in callback-only states never show Home.
- Targeted Undo during a draft leaves that draft/state intact.
- Disabled Phase 1 actions and every unauthorized/group action cause no mutation;
  disabled Phase 1 actions do not even create user/settings bootstrap rows. Baseline
  `/diet` and A1 pin/hide remain available to authorized private users as specified.
- With Phase 1 disabled, greetings/Home/actions show no Home snapshot; compatibility
  paths remove the keyboard and arbitrary text exposes no Phase 1 surface.
- With Phase 1 disabled on the Release B binary, a full structured
  `/diet -> source -> quantity -> Add/Save` sequence uses legacy callbacks and saves
  exactly once; no Phase 1 control/default appears.
- Drive every `dq_*`, `dd_*`, revised builder, current-value, and receipt callback
  family through real dispatch in its valid state plus wrong-state, stale-revision,
  old-message, foreign-owner, and flag-off cases.
- With Phase 1 disabled, `mr_undo` leaves its target intact and retires/synchronizes the
  stale receipt safely.
- `/keyboard hide|show` in every active state preserves the state and all draft keys;
  hide removes the bar and show returns the active-flow hint/removal.
- `app.update_processor.max_concurrent_updates == 1`.

### 13.2 Clock and Home

Test in `LOCAL_TZ`:

- 03:59/04:00;
- 10:59/11:00;
- 15:59/16:00;
- 21:59/22:00;
- local-date rollover for every Home metric;
- incomplete Diet calories marker;
- escaped/bounded first name;
- exact two-message choreography;
- keyboard `off`, `pilot`, `on`, and `remove`; and
- exact markup properties/labels with no `Describe`.

### 13.3 Transaction primitives and Repeat

- `BEGIN IMMEDIATE` precedes the first receipt/source read.
- An unexpected/nested active transaction raises `RuntimeError` in normal and
  `python -O` execution; it never depends on `assert`.
- `BaseException` rolls back explicit read and write transactions and releases the lock.
- With two SQLite connections, a writer committing between bundle queries cannot
  produce a mixed `get_current_value_bundle` snapshot.
- Structured meal copies every snapshot/provenance field, duplicates, order, original
  meal type, and null-versus-zero values.
- Legacy no-child meal invents no child.
- Deleted/archived live sources do not affect exact Repeat.
- Same timestamp chooses highest ID.
- Same update returns the same meal; distinct updates create distinct meals.
- Empty Repeat receipt remains empty after a later meal.
- Replay after targeted Undo returns `REPLAYED_REMOVED`.
- Failure after header, during child copy, or before receipt rolls back everything.
- Cross-owner direct calls fail closed.
- Mutation receipt survives Undo.

### 13.4 Targeted Undo

- Age 23:59:59 and exactly 24:00:00 delete; greater than 24 hours expires.
- Second/microsecond/aware/naive timestamp parsing.
- Null/malformed timestamp fails closed as expired.
- Fabricated and cross-owner IDs have the same result.
- An intervening newer meal remains.
- Children cascade; receipt remains.
- Concurrent/replayed callback yields one delete and harmless already-removed outcomes.

### 13.5 Defaults and modes

- Reject zero, negative, NaN, infinity, over-bound amount, invalid unit, partial pair,
  inactive/missing/cross-owner source.
- Accept metric aliases, named private portions, and recipe serving.
- Source/portion removal invalidates automatic use without silently clearing/logging.
- Catalog Quick requires an explicit quantity, never accepts a default, and stamps the
  current active catalog provenance; freetext never enters the quick repository API.
- Set/change/remove preserves pin/hide.
- Explicit pin/hide desired-state replay and rapid double delivery converge; pin/hide
  mutual rule holds.
- `/suggestions reset` preserves defaults.
- Quick valid default creates exactly one child in two taps.
- Same quick callback replay writes once; replay after Undo does not recreate.
- Builder valid default changes only the draft until Save.
- Missing default asks quantity.
- `Log + set default` forced failure rolls back meal, child, preference, receipt.
- Catalog has no default controls.

### 13.6 Current values

- Changed private food.
- Changed recipe ingredient totals.
- Changed catalog provider revision/nutrition.
- Mixed structured/freetext, duplicates, duplicate `item_order` values, child-ID
  decision identity, stable order, and null nutrient propagation.
- Missing/archived source, removed portion, empty recipe, invalid/missing quantity.
- Keep-original and Remove repairs.
- Removing all items disables Save.
- Legacy no-child/all-freetext has no action.
- Source changes between preview/confirm force a new preview.
- Confirmation holds `BEGIN IMMEDIATE` across authoritative bundle reload, digest check,
  insert, and receipt; a competing source write cannot interleave.
- Cross-owner callback/meal fails closed.
- Cancel/timeout writes nothing.
- Same confirmation update writes once; replay after Undo does not recreate.
- A committed confirmation replay returns its receipt/tombstone before source
  re-resolution, even when the source later changes or disappears.
- Canonical JSON/digest fixtures cover Unicode, nulls, `1` versus `1.0`, signed zero,
  decimal precision, item order, and every persisted field.

### 13.7 Ranking, pagination, and draft controls

- Catalog-only user history with no private sources.
- Active-only catalog identities.
- Meal frequency, total frequency, recency, stable ties.
- Suggestions-off excludes learned catalog candidates.
- Cross-user isolation and no full-catalog enumeration.
- First/middle/last/out-of-range pages.
- Page rerank/clamp after hide/deactivation.
- A queued source tap from page N is rejected after navigation replaces it with page
  N+1, even though the legacy source payload carries no revision.
- Pin/hide/default manage controls owner/current-message checks.
- Draft replace/remove/change quantity for first/middle/last item.
- Freetext control set.
- Revision-stale callbacks make no change.
- Removing final item returns to choice.
- Maximum 20-item keyboard remains under Telegram limits.

### 13.8 Baseline and dependency checks

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q bot scripts
.\.venv\Scripts\python.exe -m pip check
```

The repository does not declare `pip-audit`, so do not assume it exists in `.venv`.
Create a clean temporary audit environment and install it explicitly:

```powershell
$LedgerAuditVenv = Join-Path ([System.IO.Path]::GetTempPath()) ("ledger-audit-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m venv $LedgerAuditVenv
$LedgerAuditPython = Join-Path $LedgerAuditVenv "Scripts\python.exe"
& $LedgerAuditPython -m pip install -r requirements-dev.txt
& $LedgerAuditPython -m pip install pip-audit
& $LedgerAuditPython -m pytest -q
& $LedgerAuditPython -m pip check
& $LedgerAuditPython -m pip_audit -r requirements.txt
```

Record the installed `pip-audit` version. Do not add it to runtime dependencies merely
for this check. Review every finding on its current merits; do not copy a stale advisory
or silently waive/upgrade it. Remove only the verified `$LedgerAuditVenv` temporary
directory after the evidence is recorded. Do not use a hard-coded historical test
count.

Assert before/after:

- `PRAGMA user_version = 8`;
- current schema verification passes;
- `PRAGMA foreign_key_check` is empty; and
- no new runtime dependency was added unless separately justified and pinned.

### 13.9 Performance gate

`scripts/benchmark_phase1.py` builds the production application with
`build_application()`, replaces only Telegram transport/send methods, and drives every
workload through `Application.process_update()`. It uses the real handlers, service,
repository, and a warmed WAL database; direct service-only timing does not satisfy this
gate.

Use either a restored isolated temporary production backup or a deterministic synthetic
fixture whose per-table row counts are at least the recorded sanitized live counts and
whose per-user history/source/draft distributions include the live maxima. Record host,
OS, Python, PTB, SQLite, DB/WAL size, per-table fixture counts, warmups, and samples.
Never benchmark against the live writable DB.

Measure at least:

- greeting-to-Home snapshot render;
- `Meal` entry through first ranked suggestion-page render;
- max-20-item exact Repeat;
- valid-default Quick commit;
- current-value resolution/confirmation;
- targeted Undo.

Use at least 20 warmups and 200 measured samples per workload. Report p50, p95, and max.
Use unique update IDs and restore the same SQLite baseline with the backup API outside
the timed interval between mutating samples; also restore the workload's documented
conversation/user-data baseline outside the timer. Receipts, history growth, or leaked
state therefore cannot bias later measurements. Validate the expected state and DB
outcome after each sample. Every full dispatcher workload must have p95 below 500 ms on
the target deployment host. No benchmark path performs network work.

## 14. Release gates, rollout, and rollback

### 14.1 Release A gate

- Unit 0/full suite green.
- A seeded v8 preference row with a default survives `/suggestions reset`; only
  pin/hide clear, and explicit pin/hide replay converges.
- Every dispatcher matrix cell green with production flags off.
- A Release-A-only dispatcher fixture sends one validly shaped callback from every
  final B and pre-A legacy family; each is answered/retired and none reads/writes the
  DB, enters a conversation, or exposes a B control.
- Manually typed disabled labels cause no mutation.
- Persistent keyboard is not sent.
- `ReplyKeyboardRemove` demonstrated in a real Telegram client.
- Release A artifact/config recorded as the only Phase 1 binary rollback target.
- Operations runbook contains exact flag/restart/removal steps.

### 14.2 Release B pre-enable

Even without a migration:

1. stop polling and drain handlers;
2. make a verified SQLite backup outside the repository;
3. record sanitized row counts and `user_version=8`;
4. restart Release B with all exposure off;
5. smoke-test `/start`, `/menu`, `/help`, `/diet`, `/recent`, reminders, schema
   verification, and disabled labels;
6. enable one user and pilot keyboard;
7. test greeting, default Quick, builder, Repeat, current values, exact Undo, stale
   controls, and keyboard removal;
8. confirm error/latency gates; then
9. enable the second user and finally `HOME_KEYBOARD_MODE=on`.

### 14.3 Rollback acceptance

- First action is the exact safe three-variable flag block from section 4.2, including an
  empty pilot list.
- Every previously enabled user sends the required synchronization update and confirms
  removal/replacement; rollback is not accepted while either known user is outstanding.
- Accepted meals/defaults are preserved.
- Binary rollback, if needed, is only to Release A.
- After rollback verify `/start`, `/diet`, `/recent`, reminders, schema v8, foreign keys,
  and sanitized row counts.
- Preserve suspect DB/WAL/SHM files for diagnosis; do not delete them.

## 15. Documentation

Update:

- README: Home, exact labels, Quick versus Builder, inference, defaults, exact Repeat
  versus current values, receipts/Undo, paging/draft controls, flags, momentary
  `/keyboard hide`, and local-only privacy.
- `docs/user_guide.html`: the same end-user behavior in its existing HTML style,
  including deferred Describe/voice/lookup and the fact that keyboard hide lasts only
  until the next eligible Home response.
- `/help`: visible Phase 1 actions and `/keyboard hide|show`; do not advertise Describe,
  lookup, voice, `forget`, or `reset-all`; state the momentary hide behavior.
- `/settings`: Phase 1/default/suggestion status useful to the acting user.
- `.env.example`: exact safe-off flags and restart requirement.
- operations runbook: Release A/B sequence, canary expansion, observation, flag-off,
  restart, stale-label handling, real-client keyboard removal.
- backup runbook: Phase 1 has no migration, Release A is rollback target, accepted rows
  are not discarded.
- `README.md` and `docs/user_guide.html` privacy wording: Phase 1 is local-only; it
  enables no lookup, LLM, or audio transfer.

## 16. Definition of Done

Phase 1a + 1b is complete only when:

- Unit 0 passes before feature work;
- all implementation units and their gates pass in order;
- schema stays v8;
- every new mutation is owner-scoped, atomic, and replay-safe;
- every state consumes controls/text/voice according to the matrix;
- Home Quick/default is two taps and Builder remains draft-only;
- Repeat copies exact historical snapshots and original meal type;
- current values always previews and confirms;
- targeted Undo cannot select an intervening meal;
- suggestions/default/reset/pagination behavior matches this document;
- Release A removal/rollback is demonstrated before Release B keyboard enablement;
- one-user then both-user pilot succeeds;
- target-host p95 is below 500 ms;
- full and clean-environment checks pass;
- docs/runbooks match deployed behavior; and
- no placeholder or unresolved design marker remains.
