# 📒 Ledger — Personal Logging Telegram Bot

A multi-user Telegram bot for tracking **Study**, **Gym**, **Diet**, and **Habits** with daily/weekly analytics, streak tracking, chart generation, and daily reminders.

## Features

| Category | What it tracks |
|---|---|
| 📖 **Study** | Subject, duration (minutes), notes |
| 🏋️ **Gym** | Exercise, sets, reps, weight (or bodyweight) |
| 🍽️ **Diet** | Meal type, food items, calories |
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
cd 12_ledger

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
| `/diet <meal> <food> [calories]` | Quick meal log |

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
├── keyboards.py     # InlineKeyboard builders
├── charts.py        # matplotlib chart generation
└── handlers/
    ├── common.py    # Auth, errors, validators, /cancel, /undo
    ├── start.py     # /start, /help, /menu
    ├── study.py     # Study ConversationHandler
    ├── gym.py       # Gym ConversationHandler
    ├── diet.py      # Diet ConversationHandler
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

## Testing

```bash
python -m pytest tests/ -v
```

Tests cover: schema/pragma verification, CRUD for all log types, streak calculation (consecutive, gap, today-unchecked, month-boundary), day-bucketing boundary cases (12:30 AM IST), and habit reactivation.
