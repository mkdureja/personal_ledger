# 📒 Ledger — Personal Logging Telegram Bot

A multi-user Telegram bot for tracking **Study**, **Gym**, **Diet**, and **Habits** with daily/weekly analytics, streak tracking, chart generation, and daily reminders.

## Features

| Category | What it tracks |
|---|---|
| 📖 **Study** | Subject, duration (minutes), notes |
| 🏋️ **Gym** | Exercise, sets, reps, weight (or bodyweight) |
| 🍽️ **Diet** | Meal type, food items, calories, and protein/carbs/fat macros |
| ✅ **Habits** | Predefined habits, daily check-off, streaks |

**Extras:**
- 📊 Charts — Study hours, gym volume, calorie intake, habit heatmaps
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
| `/gym` | Guided workout log (multi-exercise) |
| `/gym <exercise> <sets> <reps> [kg]` | Quick single exercise |
| `/diet` | Guided meal log |
| `/diet <meal> <food> [calories] [p=<g> c=<g> f=<g>]` | Quick meal log |

The study duration must be unambiguous: a single bare number
(`/study maths 45 revised trig`), or — when notes also contain a number — a
token marked with `m`, e.g. `/study physics 60m reviewed chapter 2`. Two bare
numbers are rejected with a hint rather than guessed.

Diet macros are optional decimal grams. For example,
`/diet lunch dal+rice 650 p=25 c=80 f=15` records 25 g protein,
80 g carbs, and 15 g fat; quick logs may include any subset of those labels.
In the guided flow, enter all three values in
protein/carbs/fat order (for example, `25 80 15`) or use `/skip`. Daily and
weekly summaries total the known values and clearly flag meals whose macros
were not recorded.

### Saved foods and recipes

| Command | Description |
|---|---|
| `/food add <key> per=<qtyunit> [kcal=<n> p=<g> c=<g> f=<g>]` | Save or update a food (at least one nutrient is required) |
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

Saving an existing food key replaces its nutrition profile; nutrient labels
left out of that update become unknown.

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

### Fast logging (Phase 1)

Gated per user by `PHASE1_ENABLED_USER_IDS` — see
[docs/operations_runbook.md](docs/operations_runbook.md) for the rollout flags.
With it off, everything above behaves exactly as documented.

Say **hi** (or `home`) to open a page with today's totals and the main menu.
Enabled users also get a persistent `[🍽️ Meal] [🔁 Repeat]` bar
(`/keyboard hide|show`).

| Action | What it does |
|---|---|
| 🍽️ **Meal** | Opens a one-item Quick log: tap a food, tap an amount, done |
| 🔁 **Repeat** | Re-logs your most recent meal as an *exact copy* — same items, same numbers, nothing re-priced |
| ⚙️ beside a saved item | Set, change, repair, or remove that item's "usual" amount |

Once a food has a **usual** amount, tapping it in Quick mode logs the whole meal
in one tap. Without one you pick an amount and get
`Log it` / `Log + set as my usual`.

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
| `/habits` | Today's habit checklist |
| `/habits setup` | Add/remove habits |

### Analytics
| Command | Description |
|---|---|
| `/summary` | Today's summary |
| `/summary week` | Last 7 days summary |
| `/chart study` | Study hours chart |
| `/chart gym` | Gym volume chart |
| `/chart diet` | Calorie intake chart |
| `/chart habits` | 14-day habit heatmap |
| `/streak` | Current habit streaks |

### Utility
| Command | Description |
|---|---|
| `/recent` | List your latest study/gym/diet entries (reconcile a save) |
| `/undo` | Undo last log — preview, then confirm (within 24h) |
| `/reminders on\|off` | Turn your scheduled reminders on or off |
| `/settings` | View your settings (reminders, routine profile) |
| `/cancel` | Cancel current conversation |
| `/menu` | Interactive main menu |
| `/help` | Command reference |

Reminders are **opt-in**: a newly authorized user receives no scheduled messages
until they run `/reminders on`.

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
    └── reminders.py # Daily reminder + routine anchor jobs

scripts/
├── backup_db.py        # WAL-safe online backup (sanitized output)
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
- **Durable reminders**: Reminders are opt-in per user; scheduled nudges retry transient failures with bounded backoff and persist per-chunk delivery state, so a restart resumes at the first recorded undelivered chunk. Operate exactly one bot process; the current code does not yet enforce that invariant, and a crash after send but before recording can still duplicate a chunk.
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
# Consistent online backup (no downtime) with sanitized verification output
python scripts/backup_db.py --source ledger.db --dest /path/outside/repo/ledger-backup.db
```

- **[docs/backup_runbook.md](docs/backup_runbook.md)** — online/clean-shutdown
  backup, restore, and the pre-migration checklist.
- **[docs/operations_runbook.md](docs/operations_runbook.md)** — running under a
  process supervisor, clean restart, dependency audit, and **data retention when a
  user is removed from `ALLOWED_USER_IDS`** (default: retain until an explicit,
  verified deletion request).

Schema migrations run automatically at startup and are non-destructive: they never
delete, merge, or reassign a user's rows, and a normalized-name collision stops the
migration with a sanitized diagnostic rather than mutating data. The current build
does not take an automatic pre-migration backup, so complete the runbook checklist
before starting any build whose schema version is newer than the live database.

## Testing

```bash
pip install -r requirements-dev.txt   # runtime + pytest
python -m pytest tests/ -v
```

Tests cover startup/job scheduling, schema upgrades and constraints, CRUD and undo ordering, timezone boundaries, streaks, authorization, callback expiry/ownership, guided-flow isolation, input bounds, catalog ownership and unit conversion, recipe scaling and snapshots, legacy habit and diet-macro migrations, analytics aggregation/routing, reminder message limits, routine config
validation, and log-aware anchor composition.
