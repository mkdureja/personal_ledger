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
- ⏰ Reminders — Daily evening nudge for unchecked habits
- ↩️ Undo — Delete last log entry (within 24h)
- ⚡ Shortcuts — Quick-log via inline args (e.g., `/study maths 45`)

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
| `/study <subject> <min> [notes]` | Quick study log |
| `/gym` | Guided workout log (multi-exercise) |
| `/gym <exercise> <sets> <reps> [kg]` | Quick single exercise |
| `/diet` | Guided meal log |
| `/diet <meal> <food> [calories] [p=<g> c=<g> f=<g>]` | Quick meal log |

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
| `/recipe list` / `/recipe show <key>` / `/recipe remove <key>` | Browse or archive recipes |

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
| `/undo` | Delete last log (within 24h) |
| `/cancel` | Cancel current conversation |
| `/menu` | Interactive main menu |
| `/help` | Command reference |

## Architecture

```
bot/
├── main.py          # Entry point, handler registration
├── config.py        # .env loader, TZ helpers
├── database.py      # Async SQLite (aiosqlite)
├── nutrition.py     # Exact unit parsing and nutrition scaling
├── keyboards.py     # InlineKeyboard builders
├── charts.py        # matplotlib chart generation
└── handlers/
    ├── common.py    # Auth, errors, validators, /cancel, /undo
    ├── start.py     # /start, /help, /menu
    ├── study.py     # Study ConversationHandler
    ├── gym.py       # Gym ConversationHandler
    ├── diet.py      # Diet ConversationHandler
    ├── catalog.py   # Saved food and recipe commands
    ├── habits.py    # Habit setup + check-off
    ├── analytics.py # Summaries, charts, streaks
    └── reminders.py # Daily JobQueue reminder
```

## Key Design Decisions

- **IST day-bucketing**: Timestamps stored in UTC, all day math in `Asia/Kolkata` via `zoneinfo`
- **Access control**: Only `ALLOWED_USER_IDS` can interact; everyone else is silently ignored
- **SQLite hardening**: WAL mode, foreign keys ON, busy_timeout, composite indexes
- **Habit semantics**: Row presence = done (no "completed" column); streaks = consecutive days with rows
- **Per-exercise persistence**: Gym loop saves each exercise immediately; abandoning loses only the current one
- **Conversation safety**: `/cancel` fallback, 5-min timeout, input validation with re-prompt
- **Bounded Telegram UI**: Habit checklists paginate legacy data and reminders split safely across messages
- **Habit setup limit**: New setups support up to 49 active habits, matching Telegram's keyboard limits

## Testing

```bash
python -m pytest tests/ -v
```

Tests cover startup/job scheduling, schema upgrades and constraints, CRUD and undo ordering, timezone boundaries, streaks, authorization, callback expiry/ownership, guided-flow isolation, input bounds, catalog ownership and unit conversion, recipe scaling and snapshots, legacy habit and diet-macro migrations, analytics aggregation/routing, and reminder message limits.
