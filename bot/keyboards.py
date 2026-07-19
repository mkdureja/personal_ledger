"""
Reusable InlineKeyboard builders for Ledger bot.
"""

from __future__ import annotations

from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# A checklist uses two buttons per habit plus a date row and a day-toggle row.
# Telegram permits at most 100 buttons in one inline keyboard, so 49 is the
# largest safe active-habit count.
MAX_ACTIVE_HABITS = 49
HABIT_PAGE_SIZE = 48


def paginate_habits(
    habits: list[dict], page: int = 0
) -> tuple[list[dict], int, int]:
    """Return a Telegram-safe habit page and normalized page metadata.

    Up to 49 habits fit without navigation. Legacy databases with more than
    that use 48-item pages, leaving room for date, toggle, and navigation rows.
    """
    if len(habits) <= MAX_ACTIVE_HABITS:
        return habits, 0, 1

    page_count = (len(habits) + HABIT_PAGE_SIZE - 1) // HABIT_PAGE_SIZE
    normalized_page = min(max(page, 0), page_count - 1)
    start = normalized_page * HABIT_PAGE_SIZE
    return (
        habits[start : start + HABIT_PAGE_SIZE],
        normalized_page,
        page_count,
    )


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


def meal_type_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Meal selection keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🌅 Breakfast", callback_data=f"meal_{user_id}_breakfast"
                ),
                InlineKeyboardButton("🌞 Lunch", callback_data=f"meal_{user_id}_lunch"),
            ],
            [
                InlineKeyboardButton("🌙 Dinner", callback_data=f"meal_{user_id}_dinner"),
                InlineKeyboardButton("🍿 Snack", callback_data=f"meal_{user_id}_snack"),
            ],
        ]
    )


def yes_no_keyboard(prefix: str, user_id: int) -> InlineKeyboardMarkup:
    """Generic Yes/No keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes", callback_data=f"{prefix}_{user_id}_yes"
                ),
                InlineKeyboardButton("❌ No", callback_data=f"{prefix}_{user_id}_no"),
            ]
        ]
    )


def habit_checklist_keyboard(
    habits: list[dict],
    checked_ids: set[int],
    showing_date: date,
    user_id: int,
    is_today: bool = True,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Dynamic habit checklist with ✅/⬜ and yesterday toggle.

    Args:
        habits: List of dicts with 'id' and 'habit_name'.
        checked_ids: Set of habit_ids that are checked for this date.
        showing_date: The date being displayed.
        user_id: Owner of the checklist, embedded in callbacks for validation.
        is_today: Whether we're showing today or yesterday.
    """
    rows: list[list[InlineKeyboardButton]] = []

    page_habits, current_page, page_count = paginate_habits(habits, page)
    page_suffix = f"_p{current_page}" if page_count > 1 else ""

    for habit in page_habits:
        hid = habit["id"]
        name = habit["habit_name"]
        date_str = showing_date.isoformat()

        if hid in checked_ids:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✅ {name}",
                        callback_data=f"habit_noop_{user_id}_{hid}",
                    ),
                    InlineKeyboardButton(
                        "Undo ↩",
                        callback_data=(
                            f"habit_u_{user_id}_{hid}_{date_str}{page_suffix}"
                        ),
                    ),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"⬜ {name}",
                        callback_data=f"habit_noop_{user_id}_{hid}",
                    ),
                    InlineKeyboardButton(
                        "Done ✓",
                        callback_data=(
                            f"habit_c_{user_id}_{hid}_{date_str}{page_suffix}"
                        ),
                    ),
                ]
            )

    # Date label
    date_label = showing_date.strftime("%b %d")
    rows.append(
        [
            InlineKeyboardButton(
                f"📅 {date_label} ({'Today' if is_today else 'Yesterday'})",
                callback_data=f"habit_noop_{user_id}_date",
            )
        ]
    )

    # Toggle button
    if is_today:
        rows.append(
            [
                InlineKeyboardButton(
                    "← Yesterday",
                    callback_data=f"habit_toggle_{user_id}_yesterday{page_suffix}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "→ Today",
                    callback_data=f"habit_toggle_{user_id}_today{page_suffix}",
                )
            ]
        )

    if page_count > 1:
        navigation: list[InlineKeyboardButton] = []
        if current_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "← Previous",
                    callback_data=(
                        f"habit_page_{user_id}_{showing_date.isoformat()}_"
                        f"{current_page - 1}"
                    ),
                )
            )
        if current_page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    "Next →",
                    callback_data=(
                        f"habit_page_{user_id}_{showing_date.isoformat()}_"
                        f"{current_page + 1}"
                    ),
                )
            )
        rows.append(navigation)

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
    user_id: int,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Habit setup view with delete buttons."""
    rows: list[list[InlineKeyboardButton]] = []

    page_habits, current_page, page_count = paginate_habits(habits, page)
    page_suffix = f"_p{current_page}" if page_count > 1 else ""

    for habit in page_habits:
        hid = habit["id"]
        name = habit["habit_name"]
        rows.append(
            [
                InlineKeyboardButton(
                    f"📌 {name}", callback_data=f"habit_noop_{user_id}_{hid}"
                ),
                InlineKeyboardButton(
                    "❌ Remove",
                    callback_data=f"habit_remove_{user_id}_{hid}{page_suffix}",
                ),
            ]
        )

    if page_count > 1:
        navigation: list[InlineKeyboardButton] = []
        if current_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "← Previous",
                    callback_data=f"habit_setup_page_{user_id}_{current_page - 1}",
                )
            )
        if current_page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    "Next →",
                    callback_data=f"habit_setup_page_{user_id}_{current_page + 1}",
                )
            )
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                "🔙 Back to Habits", callback_data=f"habit_setup_done_{user_id}"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)
