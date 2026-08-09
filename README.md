# 📒 Ledger — Personal Logging Telegram Bot

A multi-user Telegram bot for tracking **Study**, **Gym**, **Diet**, **Weight**, and **Habits** with daily/weekly analytics, streak tracking, chart generation, and daily reminders.

## Features

| Category | What it tracks |
|---|---|
| 📖 **Study** | Subject, duration (minutes), notes |
| 🏋️ **Gym** | Exercise picked by muscle group, then one set at a time — each set keeps its own reps and weight |
| 🍽️ **Diet** | Meal type, food items, calories, and protein/carbs/fat macros |
| ⚖️ **Weight** | One body weight per day; missed days carry forward (up to 10) so the 7-day average stays readable |
| ✅ **Habits** | Predefined habits, daily check-off, streaks |

**Extras:**
- 📊 Charts — Study hours, gym volume, calorie intake, weight trend, habit heatmaps
- 🔥 Streaks — Consecutive-day tracking for habits
- ⏰ Reminders — Daily evening nudge for unchecked habits, **opt-in per user**
- 🌙 Routine — Optional log-aware anchor nudges + motivational quotes ([details](#routine--motivation))
- ↩️ Undo — Delete last log entry (within 24h)
- 🗒️ Recent — `/recent` lists your latest entries to reconcile a save
- ⚡ Shortcuts — Quick-log via inline args (e.g., `/study maths 45`)

**Multi-user:** each authorized user gets a **private, independent ledger** — data,
analytics, streaks, callbacks, and reminders are strictly per-user. One bot token
and database provide application-level isolation; they do not hide data from the
machine/database operator.

## Setup

### 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get Your User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. Copy the numeric user ID

### 3. Install

```bash
# Clone and enter the project
cd personal_ledger

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

### 4. Configure

```bash
# Copy the example config
copy .env.example .env

# Edit .env with your values:
# BOT_TOKEN=<your-bot-token>
# ALLOWED_USER_IDS=<your-user-id>
# TZ=Asia/Kolkata
# REMINDER_HOUR=20
```

### 5. Run

```bash
python -m bot
```

## Commands

### Logging
| Command | Description |
|---|---|
| `/study` | Guided study log |
| `/study <subject> <min> [notes]` | Quick study log (see note) |
| `/gym` | Tap-through workout log: muscle group → exercise → set by set |
| `/gym <exercise> <sets> <reps> [kg]` | One-line shortcut, when every set is identical |
| `/diet` | Guided meal log |
| `/diet <meal> <food> [calories] [p=<g> c=<g> f=<g>]` | Quick meal log |

The study duration must be unambiguous: a single bare number
(`/study maths 45 revised trig`), or — when notes also contain a number — a
token marked with `m`, e.g. `/study physics 60m reviewed chapter 2`. Two bare
numbers are rejected with a hint rather than guessed.

**Calories are mandatory; macros are not.** Every meal records calories, because
Home, the daily total, and the diet chart are all built on them — a meal without
calories is not an incomplete record, it is an absent one.

Macros were mandatory too, briefly. What that produced in practice was people
typing macros they had estimated in their heads, which puts the same guess in
the ledger while removing the app's ability to know it was a guess. So in the
guided flow `/skip` saves the meal with its calories and leaves the macros
blank. A blank is honest and visible: an unknown macro makes the *meal's* total
for that macro unknown rather than smaller, and the summary reports
`Macros (known values)` with a note naming how many it excluded.

**Definitions are still complete.** A saved food, a recipe, or a catalog entry
needs all four, because those numbers are inherited by every meal that uses
them — one hole there spreads everywhere. Saving your usual foods once is still
the way to stop typing numbers at all.

The numbers come from one of two places, and the app never estimates them:

- **From your data.** Log a saved food, a recipe, or a shared-catalog item and
  the nutrition is calculated from that stored definition. This is the fast path
  — `/food add oats per=100g kcal=389 p=16.9 c=66 f=6.9` once, then
  `/describe 100g oats` forever after.
- **Typed in.** A one-off meal takes all four: `/diet lunch dal+rice 650 p=25
  c=80 f=15`, or the guided flow's calorie and macro prompts (`25 80 15`).

A definition saved before this rule that is still missing a macro is reported as
"not logged" with the reason, rather than being logged with a hole in it.

### Keeping what you typed

Typing a meal is the escape hatch for everything the ledger does not already
know, and it costs a name plus four numbers *every* time — which is why an empty
food list is the single largest source of friction here. `bot/handlers/keep_food.py`
closes the loop at the one moment it is cheap: the meal is saved, so the
nutrition is known to be complete, and the name is still on screen.

```
✅ Saved · Lunch · Rajma chawal · 520 cal

➕ Log another meal?
  [💾 Save “Rajma chawal”]
  [🍽️ Log another] [✅ Done]
```

One tap writes a private food defined as **one helping** — `base_unit="piece"`,
`basis_amount=1` — plus a `portion` portion and that same `1 portion` as the
stored usual, which is what promotes it to a one-tap ⚡ row in the picker. A gram
weight is deliberately *not* invented: nobody supplied one, and guessing it would
make every future log from that food quietly wrong.

The button is registered outside every `ConversationHandler`, alongside targeted
meal Undo and suggestion Withdraw — the keyboard outlives the flow that drew it,
and a control that goes inert on timeout looks broken. Nothing large rides in the
callback: it names a meal id and an item position, and both the name and the four
nutrients are read back from `diet_log_items` at tap time. An existing food of the
same name suppresses the offer and is re-checked at the tap, so a second press
reports rather than overwrites.

### Meal shortcuts

The picker already learns: `bot/suggestions.py` weights same-meal-type frequency
three times general use, so what you eat at snack time drifts to the top of the
snack list on its own. `/shortcuts` is the manual half, for the two cases
learning cannot cover — a food you *know* belongs to a meal but haven't logged
yet, and a shared-catalog item, which has no per-user preference row at all.

```
/shortcuts  →  [🌅 Breakfast] [🥗 Lunch]
               [🍽️ Dinner]    [🍎 Snack]

🍎 Snack shortcuts
  [⭐ Skyr]        ← tap to remove
  [☆ Oats]        ← tap to add
  [🔍 Search the catalog]
```

A starred item is the **first row of that meal's picker and no other**, marked
⭐ so it is clear it is there because you put it there rather than because it
scored well. It outranks even a pin, which is the broader "always show me this".

It stores only a pointer — no nutrition, no amount — so editing the food still
changes what gets logged, and archiving it simply drops the row from the list
until the food comes back. Limit of 12 per meal: past a handful of buttons,
scanning them costs more than searching would.

| Command | Description |
|---|---|
| `/shortcuts` | Pick a meal, then star the items that belong to it |

### Logging a workout

Tap **🏋️ Workout** and pick a muscle group — Chest, Back, Legs, Shoulders, Arms,
Core, Cardio/HIIT — then the exercise. Exercises you logged recently appear above
the groups, because most sessions repeat previous work.

Then log **one set at a time**:

```
🏋️ Chest press

Set 1 — send reps and weight, e.g. 10 50
> 12 40
  1. 12 × 40kg
[🔁 Same again]  [✏️ Different]
[✅ Done with this exercise]
```

`🔁 Same again` repeats the last set with one tap, so a straight 3×10 costs three
taps and no typing. `✏️ Different` asks for the next set's numbers — because real
sets vary, and the ledger records each one rather than flattening them.

Send just a number (`15`) for a bodyweight set.

**A whole exercise in one message**, if you would rather type than tap:

| You send | It records |
|---|---|
| `10 50` | one set, 10 reps at 50 kg |
| `15` | one set, 15 reps bodyweight |
| `3x10 40` | three sets of 10 reps at 40 kg |
| `12 40, 10 45, 8 50` | three different sets |

`NxR` is **sets × reps**, the way it is written in a gym — `3x10` is three sets of
ten, never three reps at 10 kg. Sets can be separated by commas, semicolons, or
newlines, and `@` is accepted anywhere (`3x10 @ 40`). Each set still becomes its
own row, so typing and tapping store exactly the same thing. Nothing is written
until you finish the exercise, and the draft is shown first — so a batch you
mistyped is cancelled, not corrected afterwards.

**Logging only the muscle group.** Not everyone wants per-set detail. On the
exercise list, **✅ Just log chest** records the session as the muscle group and
nothing else — two taps from the Workout button. It stores no reps, no weight,
and deliberately no set rows: a fabricated `1 × 1` would be a number nobody
performed. Those sessions show as "1 set(s)" in `/recent` and count toward your
workout streak, but contribute nothing to the volume chart, which is correct —
no volume was recorded.

An exercise that isn't listed is added with **➕ Add your own**; it is saved under
that muscle group for you (not the other user) and you go straight into logging
it. The shared starter list is never modified.

**How it is stored.** Each set is its own row. The exercise header keeps the
`sets` count, plus the reps and weight *only when every set matched* — so a
uniform exercise still summarises as "3×10 @ 50kg" and a varying one honestly
records no single figure. Total volume is stored either way, so the volume chart
is unaffected.

### Saved foods and recipes

| Command | Description |
|---|---|
| `/food add <key> per=<qtyunit> kcal=<n> p=<g> c=<g> f=<g>` | Save or update a food (**all four nutrients required**) |
| `/food portion <key> <portion>=<qtyunit>` | Add or update a named portion |
| `/food unportion <key> <portion>` | Remove a named portion |
| `/food list` / `/food show <key>` / `/food remove <key>` | Browse or archive saved foods |
| `/recipe add <key> yield=<qtyunit>` | Save or update a recipe and its yield |
| `/recipe ingredient <recipe> food:<food> <qtyunit>` | Add or update an ingredient (attached or spaced unit) |
| `/recipe removeitem <recipe> food:<food>` | Remove an ingredient |
| `/recipe duplicate <recipe> <new-key>` | Clone a recipe and its ingredients to make a variant |
| `/recipe list` / `/recipe show <key>` / `/recipe remove <key>` | Browse or archive recipes |

`duplicate` is how you keep variants: clone `chicken-curry` to
`chicken-curry-light`, then edit the copy. The two are fully independent
afterwards, and a duplicate can itself be duplicated. The copy is created with
all its ingredients in one transaction, so a failure leaves no half-built recipe.

Keys use one token of ASCII letters/numbers; kebab-case names such as
`greek-yogurt` are recommended.
Quantities accept metric aliases such as `220gm` as well as food-specific
portions such as `1 medium`. Log a saved definition explicitly so ordinary
`/diet` commands remain unchanged:

Saving an existing food key replaces its whole nutrition profile, so every
update restates all four values — an update can no longer blank out a macro by
omitting it.

Recipe ingredients keep the resolved base quantity, so changing a named
portion later does not rewrite the recipe. Food nutrition edits affect future
recipe logs, while completed diet logs remain unchanged snapshots.

```text
/diet snack food:apple 1 medium
/diet snack food:apple 220 gm
/diet dinner recipe:chicken-curry 1 serving
```

Dimensions are never converted unless you configure an explicit food-specific
mapping such as `piece=50g` or `ml=1.4g`. A recipe can be logged in grams or
millilitres only when its recorded yield uses that unit dimension.

**Food or recipe?** A food is one ingredient and stores its own nutrition, read
off a label as "per 100 g" or "per piece"; the amount varies when you log it. A
recipe is a combination and stores *no* nutrition at all — it is calculated from
its ingredients every time. So every ingredient must already exist as one of that
user's saved foods, and a shared-catalog entry cannot be an ingredient.

### Adding foods from this machine

Building the first dozen staples is a form-shaped job, not a chat-shaped one:

```powershell
python -m scripts.food_admin
```

It prints a `http://127.0.0.1:8765/?token=...` URL and opens it. The page adds
foods and recipes to either ledger — or both at once, as separate rows — with
named portions and the usual amount that makes a source a ⚡ one-tap row.

Every write goes through `DatabaseManager`, so mandatory nutrition, name
normalization, portion rules, and owner checks are the same code the bot runs.
It **never migrates**: if the database is not at the version this checkout knows,
it refuses to start and tells you to start the bot once, whose preflight takes a
verified backup first. It binds to the loopback interface and requires the
per-run token, because a page in a browser can post to `localhost` without a
token being involved.

Safe to run while the bot is polling — both open the same WAL database with a
busy timeout — though a batch of foods is calmer with the bot stopped.

### Fast logging (Phase 1)

Gated per user by `PHASE1_ENABLED_USER_IDS` — see
[docs/operations_runbook.md](docs/operations_runbook.md) for the rollout flags.
With it off, everything above behaves exactly as documented.

Enabled users also get a persistent `[🍽️ Meal] [🔁 Repeat last meal]` bar
(`/keyboard hide|show`), and Home names the meal Repeat would re-log.

| Action | What it does |
|---|---|
| 🍽️ **Meal** | Opens a one-item Quick log: tap a food, tap an amount, done |
| 🔁 **Repeat last meal** | Re-logs your most recent meal as an *exact copy* — same items, same numbers, nothing re-priced |
| ⚙️ beside a saved item | Set, change, repair, or remove that item's "usual" amount |

Once a food has a **usual** amount, tapping it in Quick mode logs the whole meal
in one tap. Without one you pick an amount and get
`Log it` / `Log + set as my usual`.

**A tap that writes looks like it writes.** In Quick mode the picker labels say
what one tap will do, and only the dynamic name is ever truncated:

| Row | One tap |
|---|---|
| `⚡ Banana · 1 medium` | Logs it now, at that exact amount |
| `⚡ Chicken curry (recipe) · 1 serving` | Same, for a recipe |
| `🥗 Banana` | Opens amount selection (no usual saved) |
| `🛠 Banana — fix usual` | Opens the repair menu; never logs by surprise |
| `🔎 Banana, raw` | Shared catalog item: always asks how much |

The same source in the guided `/diet` builder renders plainly and opens amount
selection, because there the tap is a step, not a write. Labels are decorated
from a single batched preference read per render, so the picker issues no
per-row query and the keyboard layer performs no I/O.

For one compatibility release the bar's old `Repeat` text still routes to the
same action, so a keyboard already sitting on a client cannot misroute.

Every fast log leaves a **receipt** you can act on later:

| Button | What it does |
|---|---|
| ↩️ **Undo** | Removes *that exact meal* (within 24h), never a newer one |
| 🍽️ **Log another** | Starts the next Quick meal |
| 🔄 **Log again at today's values** | Re-prices the same items from your current foods and shows the difference before saving |

`Repeat` and `Log again at today's values` are deliberately different: the first
preserves history exactly, the second reflects edits you have made since. If an
item can no longer be re-priced (its food was deleted, a portion renamed), you
are asked to keep or drop that item — nothing is guessed or silently omitted.

Redelivered updates (which happen when the bot restarts) never double-log:
each action's outcome is recorded once and replayed.

### Habits
| Command | Description |
|---|---|
| `/habits` | Today's habit checklist — each habit is one full-width button; tap its **name** to check or un-check |
| `/habits setup` | Add/remove habits |

### Weight
| Command | Description |
|---|---|
| `/weight` | Prompt for today's weight — tap a number near your last one, or type an exact one |
| `/weight 72.4` | Log it in a single message, with no flow to leave |

One entry per calendar day: weighing again **corrects** the day rather than
adding a second answer. Missing a day is expected — the chart and the 7-day
average carry the last reading forward for up to **10 days**, then break the
line rather than draw a flat one across a longer silence. A carried day is
always drawn as a hollow dot, so it can never be mistaken for a day you weighed.

### Analytics
| Command | Description |
|---|---|
| `/summary` | Today's summary |
| `/summary week` | Last 7 days summary |
| `/chart study` | Study hours chart |
| `/chart gym` | Gym volume chart |
| `/chart diet` | Calorie intake chart |
| `/chart weight` | 30-day weight trend with its 7-day rolling average |
| `/chart habits` | 14-day habit heatmap |
| `/streak` | Current habit streaks |

### Home and utility
| Command | Description |
|---|---|
| `/home` | Today's totals and the action buttons |
| `/start` | Register (reminders off), then open Home with a one-time welcome |
| `/menu` | Same surface as `/home` |
| `/recent` | List your latest study/gym/diet entries (reconcile a save) |
| `/shortcuts` | Star the items that belong to each meal |
| `/undo` | Undo last log — preview, then confirm (within 24h) |
| `/reminders on\|off` | Turn your scheduled reminders on or off |
| `/suggestions on\|off\|reset` | Personalized ordering of your saved items |
| `/suggest <idea>` | Send an idea about the bot itself; bare `/suggest` asks for it |
| `/keyboard hide\|show` | Hide or restore the quick-action bar |
| `/settings` | View your settings (reminders, routine profile) |
| `/cancel` | Cancel current conversation |
| `/help` | Command reference |

**One idle Home.** `/home`, `/start`, `/menu`, and a greeting (`hi`, `hello`,
`hey`, `home`) all render the same surface, so there is nothing to remember:

```text
[🍽️ Log meal]   [⚖️ Weight]
[✅ Habits]      [💊 Supplements]
[🏋️ Workout]     [📖 Study]
[🗒️ Recent]      [📊 Analytics]
```

During a guided log, every one of those entries preserves the draft and returns
the finish-or-cancel hint instead of replacing the flow. Unrecognized idle text
gets one short reply carrying the same buttons, rather than silence. Telegram's
command picker lists the everyday set: `/home`, `/weight`, `/recent`, `/undo`,
`/help`.

Reminders are **opt-in**: a newly authorized user receives no scheduled messages
until they run `/reminders on`.

### Suggestions about the bot

`/suggest <idea>` files what a user thinks the app should do differently;
`/suggest` alone shows what they have already sent and waits for the next
message. **Everything after the command is the suggestion** — no word is
reserved as a subcommand, because a capture command that silently swallows one
phrasing is worse than one with no shortcuts, and the text is stored exactly as
typed (up to 1000 characters).

A suggestion is deliberately **not ledger data**: it joins no total, chart, or
streak, and `/undo` does not reach it. The receipt carries 🗑 **Withdraw**, with
no time limit, since withdrawing an opinion rewrites no history. Read them with
`python -m scripts.list_suggestions` (add `--user <id>` or `--limit N`); it only
ever issues SELECTs, so it is safe to run beside the live bot.

Note the singular/plural split: `/suggest` sends an idea about the bot,
`/suggestions` orders your food list.

## Routine & motivation

Drop a `routine.yaml` in the project root (copy `routine.example.yaml`) to turn
the bot from a passive logger into a daily feedback loop. Each **anchor** fires
once per day at a fixed local time and can inspect that day's logs — celebrating
what's done, nudging what isn't — and optionally append a rotating motivational
quote.

```yaml
targets:
  study_min: 60
  gym_days: [Mon, Wed, Fri, Sat]
anchors:
  - { id: morning, time: "08:00", emoji: "☀️", title: "Morning kickoff", checks: [], quote: true }
  - { id: evening, time: "21:00", emoji: "🌙", title: "Evening review", checks: [habits, study, gym, diet], quote: true }
quotes:
  - "Discipline equals freedom."
```

- `checks` accepts any of `study`, `gym`, `diet`, `habits`; an empty list is a
  pure motivational push.
- `targets.study_min` reports study as on-track vs. behind; `targets.gym_days`
  keeps the gym line kind ("rest day") on non-gym days.
- When `routine.yaml` is present it **replaces** the single `REMINDER_HOUR`
  reminder — include `habits` in your evening anchor to keep the habit nudge.
- Missing file → legacy single reminder. Malformed file → the bot refuses to
  start with a clear error. Edits apply on restart.

## Architecture

```
bot/
├── main.py          # Entry point, build_application(), handler registration
├── config.py        # .env loader, allowlist/TZ validation, TZ helpers
├── database.py      # Async SQLite (aiosqlite)
├── migrations.py    # Versioned, atomic PRAGMA user_version migrations
├── nutrition.py     # Exact unit parsing and nutrition scaling
├── meal_models.py   # Typed nutrition/receipt/result contracts
├── suggestions.py   # Per-user ranked food suggestions
├── keyboards.py     # InlineKeyboard builders
├── charts.py        # matplotlib chart generation
├── routine.py       # routine.yaml loader/validator + quote rotation
├── services/        # Shared meal logging and current-value resolution
└── handlers/
    ├── common.py    # Auth, errors, validators, /cancel, /undo, mutation source
    ├── start.py     # /start, /help, /menu
    ├── home.py      # Today snapshot, quick actions, keyboard controls
    ├── study.py     # Study ConversationHandler
    ├── gym.py       # Gym ConversationHandler
    ├── diet.py      # Diet ConversationHandler
    ├── catalog.py   # Saved food and recipe commands
    ├── receipts.py  # Targeted meal Undo and current-value replay
    ├── habits.py    # Habit setup + check-off
    ├── analytics.py # Summaries, charts, streaks
    ├── recent.py    # /recent reconciliation
    ├── settings.py  # /settings, /reminders opt-in
    ├── suggest.py   # /suggest — ideas about the bot, not ledger data
    └── reminders.py # Daily reminder + routine anchor jobs

ledger_schema.py     # Dependency-free schema contract (versions, required tables)
ledger_backup.py     # Dependency-free inspect/verify/online-backup functions

scripts/
├── backup_db.py        # CLI over ledger_backup (create / --verify-only)
├── list_suggestions.py # Read what /suggest has filed (maintainer view)
└── send_bot_message.py # Manual allowlisted message helper

docs/
├── backup_runbook.md      # Backup/restore + pre-migration checklist
├── operations_runbook.md  # Process supervisor, retention, dependency audit
└── user_guide.html        # End-user guide
```

## Key Design Decisions

- **IST day-bucketing**: Timestamps stored in UTC, all day math in `Asia/Kolkata` via `zoneinfo`
- **Access control & tenancy**: Only `ALLOWED_USER_IDS` in **private chats** can interact (group/unauthorized use is silently ignored, and callbacks fail closed without a private chat); every read, write, callback, and reminder is scoped to the acting user's numeric ID, so the two ledgers never mix
- **Secret hygiene**: HTTPX request logging (which embeds the bot token) is silenced and the token is redacted from any remaining log output
- **Versioned migrations**: Schema evolves through ordered, atomic migrations keyed on `PRAGMA user_version` (see `bot/migrations.py`); a normalized-name collision stops with a sanitized diagnostic instead of silently mutating data
- **SQLite hardening**: WAL mode, foreign keys ON, busy_timeout, composite indexes; reads and writes share one connection lock so a read never sees an uncommitted, later-rolled-back write
- **Reversible undo**: `/undo` previews the exact entry and deletes only on confirm, via an idempotent delete-by-id — a failed retry can't delete a newer entry
- **Replay-safe mutations**: Pending updates survive restarts (`drop_pending_updates=False`); study/gym/diet writes record a per-update receipt so a replayed Telegram update produces exactly one row, and `/recent` lets a user reconcile a save
- **Durable reminders**: Reminders are opt-in per user; scheduled nudges retry transient failures with bounded backoff and persist per-chunk delivery state, so the next run of a job resumes at the first recorded undelivered chunk instead of re-sending. Because `run_daily` only ever schedules the *next* occurrence, a process that was down at the scheduled minute would otherwise skip that day entirely — so startup replays a job whose slot has passed and which recorded nothing today, once, after a short delay. A crash after send but before recording can still duplicate a chunk.
- **Habit semantics**: Row presence = done (no "completed" column); streaks = consecutive days with rows, scanned in pages with no fixed cap; case/format-insensitive `name_key` keeps a renamed-case habit's streak intact. Activity periods record when each habit was live, so weekly adherence counts only the days a habit actually existed — deactivating mid-week keeps its earlier completions
- **Per-exercise persistence**: Gym loop saves each exercise immediately; abandoning loses only the current one
- **Conversation safety**: `/cancel` fallback, 15-minute timeout, input validation with re-prompt
- **Bounded Telegram UI**: Habit checklists paginate legacy data and reminders split safely across messages
- **Habit setup limit**: New setups support up to 49 active habits, matching Telegram's keyboard limits
- **Routine as data**: Anchors live in an optional `routine.yaml` validated at startup; a missing file falls back to the legacy reminder, so existing installs are unaffected

## Backup & operations

The database is **WAL-backed and live** — `ledger.db` alone is not a complete
snapshot. Always back up with a consistent method to an explicit destination
outside the repository. Never place a plaintext backup in cloud storage:

```bash
# Consistent online backup (no downtime), sanitized and version-aware verification
python -m scripts.backup_db --source ledger.db --dest /path/outside/repo --expect-version latest

# Verify an existing rollback point at whatever schema version it carries
python -m scripts.backup_db --verify-only /path/outside/repo/ledger-v8-20260730-101500Z.db
```

`--dest` has no default and is refused inside the repository; `--expect-version`
is mandatory when creating, so a stale source can never be certified as current.
The schema contract itself lives in two dependency-free root modules,
`ledger_schema.py` and `ledger_backup.py`, which the standalone script, the
startup verifier, and the migration preflight all share.

- **[docs/backup_runbook.md](docs/backup_runbook.md)** — online/clean-shutdown
  backup, restore, and the pre-migration checklist.
- **[docs/operations_runbook.md](docs/operations_runbook.md)** — running under a
  process supervisor, clean restart, dependency audit, and **data retention when a
  user is removed from `ALLOWED_USER_IDS`** (default: retain until an explicit,
  verified deletion request).

Schema migrations run automatically at startup and are non-destructive: they never
delete, merge, or reassign a user's rows, and a normalized-name collision stops the
migration with a sanitized diagnostic rather than mutating data. A pending
migration also **cannot run without a freshly verified backup** of the exact
source it is about to change — set `BACKUP_DEST_DIR` to a directory outside the
repository, or startup refuses and leaves the schema untouched. A populated legacy
(`user_version = 0`) database is additionally rehearsed on a throwaway copy first.

Exactly one polling process may run per database, and startup enforces that with
an OS-level lock taken before the database is opened; a second process exits
non-zero without touching data.

## Testing

```bash
pip install -r requirements-dev.txt   # runtime + pytest
python -m pytest tests/ -v
```

Tests cover startup/job scheduling, schema upgrades and constraints, CRUD and undo ordering, timezone boundaries, streaks, authorization, callback expiry/ownership, guided-flow isolation, input bounds, catalog ownership and unit conversion, recipe scaling and snapshots, legacy habit and diet-macro migrations, analytics aggregation/routing, reminder message limits, routine config
validation, log-aware anchor composition, backup/schema verification contracts,
the single-instance lock (in real subprocesses), and the pre-migration backup gate.

`.github/workflows/tests.yml` runs the same suite on **windows-latest** and
**ubuntu-latest** for every push and pull request — both platforms, because the
instance lock has a distinct branch on each. `tests/conftest.py` pins every
validated setting, so the suite needs no `.env` and cannot pick up deployment
configuration.
