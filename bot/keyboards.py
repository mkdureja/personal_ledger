"""
Reusable InlineKeyboard builders for Ledger bot.
"""

from __future__ import annotations

from datetime import date

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .callback_data import to_base36


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


def home_reply_keyboard() -> ReplyKeyboardMarkup:
    """The persistent Home quick-action bar: ``[Meal] [Repeat]``.

    ``Describe`` is reserved but not rendered in Phase 1. The bar is only sent to
    keyboard-eligible users (plan §8.4); every other Home/compatibility response
    removes it via :func:`reply_keyboard_remove`.
    """
    return ReplyKeyboardMarkup(
        [["Meal", "Repeat"]],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


def reply_keyboard_remove() -> ReplyKeyboardRemove:
    """Remove any persistent reply keyboard from the user's client."""
    return ReplyKeyboardRemove()


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


# The tap-first diet flow lists one saved item per row. Telegram allows 100
# buttons; capping well below that keeps the message readable and leaves room
# for the control rows. A two-user ledger is not expected to approach this.
MAX_FOOD_CHOICES = 40
_MAX_BUTTON_LABEL = 40


def _button_label(text: object) -> str:
    """Bound a dynamic button label so long saved names stay readable."""
    label = str(text)
    if len(label) > _MAX_BUTTON_LABEL:
        return label[: _MAX_BUTTON_LABEL - 1] + "…"
    return label


def _fmt_amount(value: object) -> str:
    """Render an entered amount without a needless decimal (220.0 -> '220')."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:g}"


SUGGESTION_PAGE_SIZE = 8


def paginate_choices(
    choices: list[dict], page: int = 0
) -> tuple[list[dict], int, int]:
    """Return one clamped page of suggestions plus its normalized metadata.

    Clamping rather than rejecting means a stale page number lands somewhere
    sensible instead of erroring.
    """
    if len(choices) <= SUGGESTION_PAGE_SIZE:
        return choices, 0, 1
    page_count = (len(choices) + SUGGESTION_PAGE_SIZE - 1) // SUGGESTION_PAGE_SIZE
    normalized = min(max(page, 0), page_count - 1)
    start = normalized * SUGGESTION_PAGE_SIZE
    return choices[start : start + SUGGESTION_PAGE_SIZE], normalized, page_count


def food_choice_keyboard(
    user_id: int,
    choices: list[dict],
    *,
    manage: bool = False,
    revision: int = 0,
    page: int = 0,
    paginate: bool = False,
    change_meal: bool = False,
) -> InlineKeyboardMarkup:
    """Ranked saved foods/recipes as one-tap buttons, plus type/cancel escapes.

    ``choices`` is an ordered list of ``{"source_type", "id", "name"}`` (already
    ranked and hidden-filtered by the caller). Buttons carry only short numeric
    ids; every id is re-validated against the acting ``user_id`` before any
    lookup.

    With ``manage`` on (Phase 1), each *private* row gains a ⚙️ button opening
    that source's default-quantity menu. Shared catalog rows never get one —
    defaults are a private preference. With ``paginate`` on, only one page of
    suggestions is shown at a time so a long list stays thumb-sized.
    """
    owner = to_base36(user_id)
    rev = to_base36(revision)
    page_count = 1
    if paginate:
        choices, page, page_count = paginate_choices(choices, page)
    rows: list[list[InlineKeyboardButton]] = []
    for choice in choices[:MAX_FOOD_CHOICES]:
        kind = choice["source_type"]
        if kind == "recipe":
            label = f"🍲 {_button_label(choice['name'])} (recipe)"
            data = f"drecipe_{user_id}_{choice['id']}"
        elif kind == "catalog":
            label = f"🔎 {_button_label(choice['name'])}"
            data = f"dcatalog_{user_id}_{choice['id']}"
        else:
            label = f"🥗 {_button_label(choice['name'])}"
            data = f"dfood_{user_id}_{choice['id']}"
        row = [InlineKeyboardButton(label, callback_data=data)]
        if manage and kind in ("food", "recipe"):
            row.append(
                InlineKeyboardButton(
                    "⚙️",
                    callback_data=(
                        f"dmanage_{owner}_{rev}_{kind[0]}_{to_base36(choice['id'])}"
                    ),
                )
            )
        rows.append(row)
    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️", callback_data=f"dpage_{owner}_{to_base36(page - 1)}"
                )
            )
        # The counter is a label; tapping it re-renders the page it names.
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{page_count}",
                callback_data=f"dpage_{owner}_{to_base36(page)}",
            )
        )
        if page + 1 < page_count:
            nav.append(
                InlineKeyboardButton(
                    "▶️", callback_data=f"dpage_{owner}_{to_base36(page + 1)}"
                )
            )
        rows.append(nav)
    if change_meal:
        rows.append(
            [
                InlineKeyboardButton(
                    "🕒 Change meal type", callback_data=f"dchangemeal_{owner}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔎 Search catalog", callback_data=f"dsearch_{user_id}")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "✍️ Type it instead", callback_data=f"dtype_{user_id}"
            ),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"dcancel_{user_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def quick_confirm_keyboard(
    user_id: int, revision: int, *, can_set_default: bool
) -> InlineKeyboardMarkup:
    """Confirm a Quick-mode single-item meal (plan §9.4).

    ``Log + set default`` is offered only for a user's own food/recipe: it is the
    one tap that turns this amount into the "usual", which shared catalog rows
    cannot have. Payloads carry owner/revision/action only — never nutrition.
    """
    owner = to_base36(user_id)
    rev = to_base36(revision)
    rows = [[InlineKeyboardButton("✅ Log it", callback_data=f"dq_log_{owner}_{rev}")]]
    if can_set_default:
        rows.append(
            [
                InlineKeyboardButton(
                    "⭐ Log + set as my usual",
                    callback_data=f"dq_default_{owner}_{rev}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✍️ Change amount", callback_data=f"dq_amount_{owner}_{rev}"
            ),
            InlineKeyboardButton(
                "✖️ Cancel", callback_data=f"dq_cancel_{owner}_{rev}"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def default_menu_keyboard(
    user_id: int, revision: int, *, has_default: bool, needs_repair: bool
) -> InlineKeyboardMarkup:
    """Manage one source's default quantity (plan §9.4).

    Three shapes: no default (Set), a valid one (Change/Remove), and a partial or
    no-longer-resolvable one (Repair/Remove). A broken default is never silently
    ignored — it is shown as needing a decision.
    """
    owner = to_base36(user_id)
    rev = to_base36(revision)
    rows = [
        [
            InlineKeyboardButton(
                "✍️ Use a different amount", callback_data=f"dd_use_{owner}_{rev}"
            )
        ]
    ]
    if needs_repair:
        edit_label = "🔧 Repair my usual"
    elif has_default:
        edit_label = "✏️ Change my usual"
    else:
        edit_label = "⭐ Set my usual"
    rows.append(
        [InlineKeyboardButton(edit_label, callback_data=f"dd_edit_{owner}_{rev}")]
    )
    if has_default or needs_repair:
        rows.append(
            [
                InlineKeyboardButton(
                    "🗑 Remove my usual", callback_data=f"dd_clear_{owner}_{rev}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔙 Back", callback_data=f"dd_back_{owner}_{rev}")]
    )
    return InlineKeyboardMarkup(rows)


def default_confirm_keyboard(user_id: int, revision: int) -> InlineKeyboardMarkup:
    """Confirm the resolved amount before it becomes the stored default."""
    owner = to_base36(user_id)
    rev = to_base36(revision)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Save as my usual", callback_data=f"dd_save_{owner}_{rev}"
                )
            ],
            [
                InlineKeyboardButton(
                    "✍️ Re-enter", callback_data=f"dd_reenter_{owner}_{rev}"
                ),
                InlineKeyboardButton(
                    "✖️ Cancel", callback_data=f"dd_cancel_{owner}_{rev}"
                ),
            ],
        ]
    )


def _recent_quantity_rows(
    user_id: int, recent: list[dict]
) -> list[list[InlineKeyboardButton]]:
    """One button per recent entered quantity, resolved by list position."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, quantity in enumerate(recent):
        label = _button_label(
            f"🕘 {_fmt_amount(quantity['entered_amount'])} "
            f"{quantity['entered_unit']}"
        )
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"drecent_{user_id}_{index}")]
        )
    return rows


def _pref_row(
    user_id: int, is_pinned: bool, hidden: bool, revision: int
) -> list[InlineKeyboardButton]:
    """A pin/unpin + hide/unhide control row for the selected source.

    The callbacks carry the acting user, the current UI revision, and the
    *desired* next state (``1`` to pin/hide, ``0`` to unpin/unhide) as strict
    base-36 tokens, so repeated delivery converges instead of toggling and a
    stale revision is rejected (plan §7.5 / §9.2).
    """
    owner = to_base36(user_id)
    rev = to_base36(revision)
    return [
        InlineKeyboardButton(
            "📌 Unpin" if is_pinned else "📌 Pin",
            callback_data=f"dpin_{owner}_{rev}_{0 if is_pinned else 1}",
        ),
        InlineKeyboardButton(
            "👁 Unhide" if hidden else "🙈 Hide",
            callback_data=f"dhide_{owner}_{rev}_{0 if hidden else 1}",
        ),
    ]


def food_portion_keyboard(
    user_id: int,
    portions: list[dict],
    recent: list[dict] | None = None,
    *,
    is_pinned: bool = False,
    hidden: bool = False,
    show_prefs: bool = True,
    revision: int = 0,
) -> InlineKeyboardMarkup:
    """Recent quantities + named portions for a food, with custom/back/pref rows.

    ``show_prefs`` is off for shared-catalog foods, which cannot be pinned/hidden
    (preferences apply only to a user's own foods and recipes).
    """
    rows: list[list[InlineKeyboardButton]] = _recent_quantity_rows(
        user_id, recent or []
    )
    rows.extend(
        [
            InlineKeyboardButton(
                _button_label(portion["name"]),
                callback_data=f"dport_{user_id}_{portion['id']}",
            )
        ]
        for portion in portions[:MAX_FOOD_CHOICES]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "✍️ Custom amount", callback_data=f"dcustom_{user_id}"
            ),
            InlineKeyboardButton("🔙 Back", callback_data=f"dback_{user_id}"),
        ]
    )
    if show_prefs:
        rows.append(_pref_row(user_id, is_pinned, hidden, revision))
    return InlineKeyboardMarkup(rows)


def recipe_quantity_keyboard(
    user_id: int,
    yield_unit: str,
    recent: list[dict] | None = None,
    *,
    is_pinned: bool = False,
    hidden: bool = False,
    revision: int = 0,
) -> InlineKeyboardMarkup:
    """Recent quantities + a quick '1 <yield_unit>' button, with custom/back/pref."""
    rows: list[list[InlineKeyboardButton]] = _recent_quantity_rows(
        user_id, recent or []
    )
    rows.append(
        [
            InlineKeyboardButton(
                f"1 {_button_label(yield_unit)}",
                callback_data=f"drq_{user_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "✍️ Custom amount", callback_data=f"dcustom_{user_id}"
            ),
            InlineKeyboardButton("🔙 Back", callback_data=f"dback_{user_id}"),
        ]
    )
    rows.append(_pref_row(user_id, is_pinned, hidden, revision))
    return InlineKeyboardMarkup(rows)


def diet_save_keyboard(
    user_id: int,
    *,
    phase1_enabled: bool = False,
    revision: int = 0,
    items: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    """Meal preview: add another item, save the meal, or cancel.

    Two shapes on purpose (plan §9.2). With Phase 1 off, this emits the original
    decimal payloads so a Release A rollback still has a fully saveable Builder.
    With it on, Add/Save carry the UI revision and each drafted item gains
    per-item edit controls — so an older keyboard left on screen stops working
    the moment the draft changes.
    """
    if not phase1_enabled:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Add another item", callback_data=f"dadd_{user_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✅ Save meal", callback_data=f"dsave_{user_id}"
                    ),
                    InlineKeyboardButton(
                        "✖️ Cancel", callback_data=f"dcancel_{user_id}"
                    ),
                ],
            ]
        )

    owner = to_base36(user_id)
    rev = to_base36(revision)
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items or []):
        position = to_base36(index)
        structured = str(item.get("source_type", "freetext")) != "freetext"
        row = []
        if structured:
            row.append(
                InlineKeyboardButton(
                    f"#{index + 1} ✍️ amount",
                    callback_data=f"dqty_{owner}_{rev}_{position}",
                )
            )
        row.append(
            InlineKeyboardButton(
                f"#{index + 1} 🔁 replace" if structured else f"#{index + 1} 🔁",
                callback_data=f"dedit_{owner}_{rev}_{position}",
            )
        )
        row.append(
            InlineKeyboardButton(
                "🗑", callback_data=f"dremove_{owner}_{rev}_{position}"
            )
        )
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                "➕ Add another item", callback_data=f"dadd_{owner}_{rev}"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Save meal", callback_data=f"dsave_{owner}_{rev}"
            ),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"dcancel_{user_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def log_another_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """After a save, offer to keep logging or finish the diet flow."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🍽️ Log another", callback_data=f"dmore_{user_id}_yes"
                ),
                InlineKeyboardButton("✅ Done", callback_data=f"dmore_{user_id}_no"),
            ]
        ]
    )


def meal_receipt_keyboard(
    user_id: int, meal_id: int, *, can_use_current: bool = False
) -> InlineKeyboardMarkup:
    """Durable controls for one completed meal (plan §10.1).

    Unlike the ephemeral guided-flow keyboards, this one stays valid after later
    meals are logged: ``Undo`` names the exact meal it was rendered for, so a
    receipt from three meals ago still removes *that* meal and nothing else.
    Tokens are base-36 to stay far below Telegram's 64-byte callback limit.

    ``Use current values`` appears only when the meal has a structured item to
    re-price; an all-freetext meal has nothing to re-resolve.
    """
    owner = to_base36(user_id)
    meal = to_base36(meal_id)
    rows = [
        [
            InlineKeyboardButton("↩️ Undo", callback_data=f"mr_undo_{owner}_{meal}"),
            InlineKeyboardButton("🍽️ Log another", callback_data=f"mr_more_{owner}"),
        ]
    ]
    if can_use_current:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Log again at today's values",
                    callback_data=f"mr_current_{owner}_{meal}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def current_values_keyboard(
    user_id: int, revision: int, proposals, *, can_save: bool
) -> InlineKeyboardMarkup:
    """Repair controls for a current-value preview (plan §10.5).

    One Keep/Remove pair per item that could not be re-resolved. ``Save`` only
    appears once every issue has an explicit answer and something is left to log
    — there is no "save anyway" that quietly drops items.
    """
    owner = to_base36(user_id)
    rev = to_base36(revision)
    rows: list[list[InlineKeyboardButton]] = []
    for position, proposal in enumerate(proposals, 1):
        if str(proposal.decision) != "unresolved":
            continue
        child = to_base36(proposal.source_child_id)
        rows.append(
            [
                InlineKeyboardButton(
                    f"↩️ Keep #{position} as logged",
                    callback_data=f"cv_keep_{owner}_{rev}_{child}",
                ),
                InlineKeyboardButton(
                    f"🗑 Drop #{position}",
                    callback_data=f"cv_remove_{owner}_{rev}_{child}",
                ),
            ]
        )
    final = []
    if can_save:
        final.append(
            InlineKeyboardButton(
                "✅ Log it", callback_data=f"cv_save_{owner}_{rev}"
            )
        )
    final.append(
        InlineKeyboardButton("✖️ Cancel", callback_data=f"cv_cancel_{owner}_{rev}")
    )
    rows.append(final)
    return InlineKeyboardMarkup(rows)


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
