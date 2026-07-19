"""
Reusable InlineKeyboard builders for Ledger bot.
"""

from __future__ import annotations

from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """2×2 category grid + analytics row."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📖 Study", callback_data="menu_study"),
                InlineKeyboardButton("🏋️ Gym", callback_data="menu_gym"),
            ],
            [
                InlineKeyboardButton("🍽️ Diet", callback_data="menu_diet"),
                InlineKeyboardButton("✅ Habits", callback_data="menu_habits"),
            ],
            [
                InlineKeyboardButton("📊 Analytics", callback_data="menu_analytics"),
            ],
        ]
    )


def meal_type_keyboard() -> InlineKeyboardMarkup:
    """Meal selection keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌅 Breakfast", callback_data="meal_breakfast"),
                InlineKeyboardButton("🌞 Lunch", callback_data="meal_lunch"),
            ],
            [
                InlineKeyboardButton("🌙 Dinner", callback_data="meal_dinner"),
                InlineKeyboardButton("🍿 Snack", callback_data="meal_snack"),
            ],
        ]
    )


def yes_no_keyboard(prefix: str = "yn") -> InlineKeyboardMarkup:
    """Generic Yes/No keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"{prefix}_yes"),
                InlineKeyboardButton("❌ No", callback_data=f"{prefix}_no"),
            ]
        ]
    )


def habit_checklist_keyboard(
    habits: list[dict],
    checked_ids: set[int],
    showing_date: date,
    is_today: bool = True,
) -> InlineKeyboardMarkup:
    """Dynamic habit checklist with ✅/⬜ and yesterday toggle.

    Args:
        habits: List of dicts with 'id' and 'habit_name'.
        checked_ids: Set of habit_ids that are checked for this date.
        showing_date: The date being displayed.
        is_today: Whether we're showing today or yesterday.
    """
    rows: list[list[InlineKeyboardButton]] = []

    for habit in habits:
        hid = habit["id"]
        name = habit["habit_name"]
        date_str = showing_date.isoformat()

        if hid in checked_ids:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✅ {name}",
                        callback_data=f"habit_noop_{hid}",
                    ),
                    InlineKeyboardButton(
                        "Undo ↩",
                        callback_data=f"habit_uncheck_{hid}_{date_str}",
                    ),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"⬜ {name}",
                        callback_data=f"habit_noop_{hid}",
                    ),
                    InlineKeyboardButton(
                        "Done ✓",
                        callback_data=f"habit_check_{hid}_{date_str}",
                    ),
                ]
            )

    # Date label
    date_label = showing_date.strftime("%b %d")
    rows.append(
        [
            InlineKeyboardButton(
                f"📅 {date_label} ({'Today' if is_today else 'Yesterday'})",
                callback_data="habit_noop_date",
            )
        ]
    )

    # Toggle button
    if is_today:
        rows.append(
            [
                InlineKeyboardButton(
                    "← Yesterday",
                    callback_data="habit_toggle_yesterday",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "→ Today",
                    callback_data="habit_toggle_today",
                )
            ]
        )

    return InlineKeyboardMarkup(rows)


def analytics_keyboard() -> InlineKeyboardMarkup:
    """Analytics sub-menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📝 Today's Summary", callback_data="analytics_summary"),
            ],
            [
                InlineKeyboardButton("📅 Weekly Summary", callback_data="analytics_week"),
            ],
            [
                InlineKeyboardButton("📊 Study Chart", callback_data="chart_study"),
                InlineKeyboardButton("📊 Gym Chart", callback_data="chart_gym"),
            ],
            [
                InlineKeyboardButton("📊 Diet Chart", callback_data="chart_diet"),
                InlineKeyboardButton("📊 Habit Chart", callback_data="chart_habits"),
            ],
            [
                InlineKeyboardButton("🔥 Streaks", callback_data="analytics_streak"),
            ],
        ]
    )


def habit_setup_keyboard(
    habits: list[dict],
) -> InlineKeyboardMarkup:
    """Habit setup view with delete buttons."""
    rows: list[list[InlineKeyboardButton]] = []

    for habit in habits:
        hid = habit["id"]
        name = habit["habit_name"]
        rows.append(
            [
                InlineKeyboardButton(f"📌 {name}", callback_data=f"habit_noop_{hid}"),
                InlineKeyboardButton("❌ Remove", callback_data=f"habit_remove_{hid}"),
            ]
        )

    rows.append(
        [InlineKeyboardButton("🔙 Back to Habits", callback_data="menu_habits")]
    )

    return InlineKeyboardMarkup(rows)
