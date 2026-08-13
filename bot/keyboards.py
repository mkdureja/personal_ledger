"""
Reusable InlineKeyboard builders for Ledger bot.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .callback_data import parse_base36, to_base36
from .meal_models import PREFERENCE_SOURCE_TYPES
from .nutrition import NutritionError, format_decimal
from .weight_series import format_kg, nudge_values


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


# The persistent quick-action bar's exact button text. It lives here, with the
# rendering, and `bot.handlers.common` derives its routing filters from these same
# constants — so a renamed button cannot leave a handler matching the old text.
MEAL_BUTTON_LABEL = "Meal"
#: A bare "Repeat" never said what it repeats, and it sits next to a button that
#: writes immediately. The label now names the consequence.
REPEAT_BUTTON_LABEL = "Repeat last meal"
#: A persistent keyboard already on a user's client keeps sending the old text
#: until they receive a new one. Routing accepts it for one compatibility
#: release so an old bar cannot misroute or silently do nothing.
LEGACY_REPEAT_BUTTON_LABEL = "Repeat"


def home_reply_keyboard() -> ReplyKeyboardMarkup:
    """The persistent Home quick-action bar: ``[Meal] [Repeat last meal]``.

    ``Describe`` is reserved but not rendered in Phase 1. The bar is only sent to
    keyboard-eligible users (plan §8.4); every other Home/compatibility response
    removes it via :func:`reply_keyboard_remove`.
    """
    return ReplyKeyboardMarkup(
        [[MEAL_BUTTON_LABEL, REPEAT_BUTTON_LABEL]],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


def reply_keyboard_remove() -> ReplyKeyboardRemove:
    """Remove any persistent reply keyboard from the user's client."""
    return ReplyKeyboardRemove()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """The Home action grid — the everyday surface for both users.

    Labels lead with the verb a user is looking for ("Log meal", "Workout")
    rather than the section name, and 🗒️ Recent is promoted onto Home because
    "did my save land?" is a normal daily question. The ``callback_data`` values
    are unchanged so a Home message sent by an older build still routes: Study,
    Gym, Diet, and Weight are claimed by their ConversationHandler entry points,
    and Habits/Recent/Analytics by :func:`bot.handlers.start.menu_callback`.

    ⚖️ Weight sits beside 🍽️ Log meal because both are daily and both are the
    reason someone opens the bot at all; the sections you visit weekly stay
    further down.

    🎯 Monitors sits last, on its own row. Its taps *count* something rather than
    tick it off, and the extra width is a small, cheap signal that it is not
    another checklist.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🍽️ Log meal", callback_data="menu_diet"),
                InlineKeyboardButton("⚖️ Weight", callback_data="menu_weight"),
            ],
            [
                InlineKeyboardButton("✅ Habits", callback_data="menu_habits"),
                InlineKeyboardButton("💊 Supplements", callback_data="menu_supplements"),
            ],
            [
                InlineKeyboardButton("🏋️ Workout", callback_data="menu_gym"),
                InlineKeyboardButton("📖 Study", callback_data="menu_study"),
            ],
            [
                InlineKeyboardButton("🗒️ Recent", callback_data="menu_recent"),
                InlineKeyboardButton("📊 Analytics", callback_data="menu_analytics"),
            ],
            [
                InlineKeyboardButton("🎯 Monitors", callback_data="menu_monitors"),
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
# However tight the budget gets, a name must stay identifiable. When the fixed
# decoration is unusually long the total may exceed the soft cap rather than
# reduce a food's name to a couple of letters — a slightly wide button is better
# than an unreadable one.
_MIN_NAME_CHARS = 12


def _button_label(text: object, limit: int = _MAX_BUTTON_LABEL) -> str:
    """Bound a dynamic button label so long saved names stay readable."""
    label = str(text)
    if len(label) > limit:
        return label[: limit - 1] + "…"
    return label


def _fmt_amount(value: object) -> str:
    """Render the stored amount losslessly, without a needless decimal.

    ``format_decimal`` starts from the float's shortest round-trippable text, so
    the label can feed the same decimal back to the resolver that the write path
    uses. A malformed legacy preference must not make the whole picker fail to
    render; the commit path will still reject it and open repair.
    """
    try:
        return format_decimal(value)
    except NutritionError:
        return str(value)


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


# Consequence markers. ⚡ means "this tap writes a meal now"; 🛠 means "this
# source's usual amount is broken and needs repair". Neither is ever used for a
# row that merely opens the amount screen.
INSTANT_PREFIX = "⚡ "
REPAIR_PREFIX = "🛠 "
_REPAIR_SUFFIX = " — fix usual"


def choice_button_label(choice: dict, *, quick: bool) -> str:
    """Compose one choice's button label under a source-aware character budget.

    Every fixed component is preserved and only the dynamic name is truncated, so
    a long food name can never displace the ⚡ consequence prefix, the
    ``(recipe)`` source marker, or the visible ``· amount unit`` suffix. For
    example: ``⚡ Chicken curr… (recipe) · 1 serving``.

    ``quick`` is the whole point of the distinction. In Quick Meal a stored usual
    makes the tap write immediately, so the row is marked ⚡ and shows the exact
    amount it would log. In the guided Builder the identical source renders
    normally, because there the tap only opens amount selection. A source with no
    usual, and any shared catalog row (v8 stores no catalog preference), also
    renders normally.
    """
    kind = choice.get("source_type")
    if kind == "recipe":
        prefix, suffix = "🍲 ", " (recipe)"
    elif kind == "catalog":
        prefix, suffix = "🔎 ", ""
    else:
        prefix, suffix = "🥗 ", ""

    # An item the user marked as belonging to this meal is at the top *because
    # they said so*, not because it happened to score well. Saying which is the
    # difference between a list they trust and one that seems to reorder itself.
    if choice.get("is_meal_shortcut"):
        prefix = "⭐ "

    quantity = ""
    if quick and kind in PREFERENCE_SOURCE_TYPES:
        default = choice.get("default")
        if choice.get("needs_repair"):
            prefix, suffix = REPAIR_PREFIX, suffix + _REPAIR_SUFFIX
        elif default:
            prefix = INSTANT_PREFIX
            # The amount and unit describe the consequence of this tap. They are
            # semantic data, not decoration, so never abbreviate either one.
            quantity = f" · {_fmt_amount(default['amount'])} {default['unit']}"

    budget = max(_MIN_NAME_CHARS, _MAX_BUTTON_LABEL - len(prefix + suffix + quantity))
    return f"{prefix}{_button_label(choice['name'], budget)}{suffix}{quantity}"


def food_choice_keyboard(
    user_id: int,
    choices: list[dict],
    *,
    manage: bool = False,
    revision: int = 0,
    page: int = 0,
    paginate: bool = False,
    change_meal: bool = False,
    quick: bool = False,
    draft_count: int = 0,
    phase1_enabled: bool = False,
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

    With ``quick`` on, rows whose tap would write immediately are marked ⚡ with
    the amount they would log (see :func:`choice_button_label`). This function
    performs **no I/O**: ``choices`` arrive already decorated by
    :func:`bot.suggestions.annotate_defaults` from one batched preference read.
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
            data = f"drecipe_{user_id}_{choice['id']}"
        elif kind == "catalog":
            data = f"dcatalog_{user_id}_{choice['id']}"
        else:
            data = f"dfood_{user_id}_{choice['id']}"
        label = choice_button_label(choice, quick=quick)
        row = [InlineKeyboardButton(label, callback_data=data)]
        if manage and kind in PREFERENCE_SOURCE_TYPES:
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
    # With items already in the draft, this screen needs a way to *finish*.
    # Without one, the only visible exit from "add another item" was ✖️ Cancel —
    # which discards the whole meal — so someone who could not find their third
    # item lost the two they had already entered. Save is drawn first and full
    # width because it is the tap that keeps the work.
    if draft_count > 0:
        items = "item" if draft_count == 1 else "items"
        # Same two payload shapes ``diet_save_keyboard`` emits, chosen the same
        # way, so a draft started on one keyboard can always be saved from the
        # other: revisioned under Phase 1, decimal on the legacy path.
        save_data = f"dsave_{owner}_{rev}" if phase1_enabled else f"dsave_{user_id}"
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ Save meal ({draft_count} {items})",
                    callback_data=save_data,
                )
            ]
        )
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


def search_empty_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """The three real exits from a zero-result catalog search.

    A dead end with no buttons left the only ways out invisible: remember an exact
    command, or abandon the draft. Every action here reuses an existing callback
    family, so nothing new has to be retired later.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "◀️ Back to my items", callback_data=f"dback_{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 Search again", callback_data=f"dsearch_{user_id}"
                ),
                InlineKeyboardButton(
                    "✍️ Type it instead", callback_data=f"dtype_{user_id}"
                ),
            ],
        ]
    )


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

    ``show_prefs`` controls the pin/hide row. It applies to shared-catalog foods
    too since schema v16 — the row is shared, the preference is this user's.
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
                f"#{index + 1} 🔁 replace",
                callback_data=f"dedit_{owner}_{rev}_{position}",
            )
        )
        # Numbered too: with several items, three unlabelled bins in a column
        # give no way to tell which row you are about to delete from.
        row.append(
            InlineKeyboardButton(
                f"#{index + 1} 🗑",
                callback_data=f"dremove_{owner}_{rev}_{position}",
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


#: Callback prefix for "keep this typed entry as a saved food". Its own family,
#: outside the ``d*`` diet-tap shapes, because the button outlives the flow that
#: drew it and must not be retired by a stale-diet-tap handler.
KEEP_FOOD_PREFIX = "kf"
#: Its inverse: un-keep a food a just-logged typed entry created.
DROP_FOOD_PREFIX = "kd"

#: A keep-food label is mostly decoration, so the name gets a tighter budget than
#: a picker row — but never less than :data:`_MIN_NAME_CHARS`.
_KEEP_FOOD_DECORATION = len("💾 Save “”")
_DROP_FOOD_DECORATION = len("🗑 Don't keep “”")


def keep_food_data(user_id: int, meal_id: int, item_order: int) -> str:
    """Encode "save item N of meal M" as ``kf_<owner36>_<meal36>_<order36>``.

    The item is named by position rather than by value: the name and all four
    nutrients are read back from ``diet_log_items`` at tap time, so nothing large
    has to survive in a 64-byte callback and a restart cannot change what the
    button means.
    """
    return (
        f"{KEEP_FOOD_PREFIX}_{to_base36(user_id)}"
        f"_{to_base36(int(meal_id))}_{to_base36(int(item_order))}"
    )


def drop_food_data(user_id: int, meal_id: int, food_id: int) -> str:
    """Encode "un-keep food F, saved by meal M" as ``kd_<owner36>_<meal36>_<food36>``.

    The meal travels with the food id so the tap can check that this food is
    still the one that meal saved. Without it a keyboard left in the scrollback
    would be an archive button for whatever food happens to hold that id later.
    """
    return (
        f"{DROP_FOOD_PREFIX}_{to_base36(user_id)}"
        f"_{to_base36(int(meal_id))}_{to_base36(int(food_id))}"
    )


def parse_drop_food(data: str, user_id: int) -> tuple[int, int] | None:
    """Decode a ``kd_*`` callback to ``(meal_id, food_id)``, or ``None``.

    Rejects for every reason at once, like :func:`parse_keep_food`.
    """
    parts = (data or "").split("_")
    if len(parts) != 4 or parts[0] != DROP_FOOD_PREFIX:
        return None
    try:
        owner = parse_base36(parts[1])
        meal_id = parse_base36(parts[2])
        food_id = parse_base36(parts[3])
    except (TypeError, ValueError):
        return None
    if owner != user_id or meal_id <= 0 or food_id <= 0:
        return None
    return meal_id, food_id


def parse_keep_food(data: str, user_id: int) -> tuple[int, int] | None:
    """Decode a ``kf_*`` callback to ``(meal_id, item_order)``, or ``None``.

    ``None`` covers every rejection — malformed payload, non-canonical base-36,
    another household member's button — so the caller answers all of them with
    one message and reveals nothing about which it was.
    """
    parts = (data or "").split("_")
    if len(parts) != 4 or parts[0] != KEEP_FOOD_PREFIX:
        return None
    try:
        owner = parse_base36(parts[1])
        meal_id = parse_base36(parts[2])
        item_order = parse_base36(parts[3])
    except (TypeError, ValueError):
        return None
    if owner != user_id or meal_id <= 0:
        return None
    return meal_id, item_order


def log_another_keyboard(
    user_id: int,
    *,
    meal_id: int | None = None,
    keepable: Sequence[tuple[int, str]] = (),
    kept: Sequence[tuple[int, str]] = (),
) -> InlineKeyboardMarkup:
    """After a save, offer to keep logging or finish the diet flow.

    ``kept`` adds one 🗑 row per typed item the meal just turned into a saved
    food, and ``keepable`` one 💾 row per typed item that could not be kept
    automatically. Both name the same thing from opposite directions, so an item
    appears in one list or the other and never both.

    The rows come first because the Log another / Done pair is the habitual tap
    and would otherwise move under a new row the user did not expect.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if meal_id is not None:
        rows.extend(
            [
                InlineKeyboardButton(
                    "🗑 Don't keep “"
                    + _button_label(
                        name,
                        max(
                            _MAX_BUTTON_LABEL - _DROP_FOOD_DECORATION,
                            _MIN_NAME_CHARS,
                        ),
                    )
                    + "”",
                    callback_data=drop_food_data(user_id, meal_id, food_id),
                )
            ]
            for food_id, name in kept
        )
        rows.extend(
            [
                InlineKeyboardButton(
                    "💾 Save “"
                    + _button_label(
                        name,
                        max(
                            _MAX_BUTTON_LABEL - _KEEP_FOOD_DECORATION,
                            _MIN_NAME_CHARS,
                        ),
                    )
                    + "”",
                    callback_data=keep_food_data(user_id, meal_id, item_order),
                )
            ]
            for item_order, name in keepable
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🍽️ Log another", callback_data=f"dmore_{user_id}_yes"
            ),
            InlineKeyboardButton("✅ Done", callback_data=f"dmore_{user_id}_no"),
        ]
    )
    return InlineKeyboardMarkup(rows)


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

    Each habit is **one full-width button** carrying the real toggle action. The
    older two-button row put an inert ``habit_noop_*`` label next to a working
    ``Done ✓`` / ``Undo ↩``, so the obvious target — the habit's own name — did
    nothing. Tapping the name now toggles it, and the ✅/⬜ prefix says which way.
    ``habit_noop_*`` remains a live, non-mutating pattern so labels on checklists
    already sitting in a chat stay harmless.

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
        done = hid in checked_ids
        action = "habit_u" if done else "habit_c"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if done else '⬜'} {name}",
                    callback_data=f"{action}_{user_id}_{hid}_{date_str}{page_suffix}",
                )
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


def supplement_dose_label(supplement: dict) -> str:
    """Render the dose/timing suffix for a supplement row, or an empty string.

    Dose and timing are both optional, so this yields ``" · 2 capsules"``,
    ``" · with dinner"``, ``" · 2 capsules, with dinner"``, or nothing at all.
    The amount drops a redundant trailing ``.0`` so "2 capsules" never renders as
    "2.0 capsules".
    """
    amount = supplement.get("dose_amount")
    unit = (supplement.get("dose_unit") or "").strip()
    timing = (supplement.get("timing") or "").strip()

    parts: list[str] = []
    if amount is not None:
        rendered = f"{float(amount):g}"
        parts.append(f"{rendered} {unit}".strip())
    if timing:
        parts.append(timing)
    return f" · {', '.join(parts)}" if parts else ""


def supplement_target(supplement: dict) -> int:
    """A supplement's daily target — how many taps make the day complete.

    ``1`` for everything that has never been given one, which is every
    supplement before schema v18 and every plain check-off since.
    """
    raw = supplement.get("target_count")
    try:
        target = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1
    return target if target >= 1 else 1


def supplement_progress_label(supplement: dict, count: int) -> str:
    """The ``⬜ Creatine 1/2`` state of one supplement on one day.

    A counted supplement always shows both numbers, including at zero: the
    target is the reason the row exists, and a row that only revealed it after
    the first tap would hide the very thing being aimed at. A plain check-off
    keeps the bare tick it has always had — there is no second number to show.
    """
    target = supplement_target(supplement)
    done = count >= target
    box = "✅" if done else ("🔸" if count else "⬜")
    label = f"{box} {supplement['name']}"
    if target > 1:
        label = f"{label} {count}/{target}"
    return label


def supplement_checklist_keyboard(
    supplements: list[dict],
    taken_ids: set[int],
    showing_date: date,
    user_id: int,
    is_today: bool = True,
    page: int = 0,
    counts: dict[int, int] | None = None,
) -> InlineKeyboardMarkup:
    """Daily supplement adherence checklist.

    Deliberately the same shape as :func:`habit_checklist_keyboard` — one
    full-width button per row carrying the real toggle, an inert date label, a
    yesterday/today switch, and pagination — because the two screens do the same
    job and a user should not have to learn a second interaction model. The
    callback prefix differs (``supp_*``) so a habit keyboard can never route into
    supplement writes, or the reverse.

    A supplement carrying a daily target (v18) breaks the one-button shape on
    purpose. Its row *adds* one rather than toggling, and gains a ➖ so the count
    can come back down — a single toggle cannot express "I have had one of the
    two I am aiming for", and silently reading the second tap as "undo" would
    delete a scoop the user had just taken.
    """
    rows: list[list[InlineKeyboardButton]] = []
    counts = counts or {}

    page_items, current_page, page_count = paginate_habits(supplements, page)
    page_suffix = f"_p{current_page}" if page_count > 1 else ""

    for supplement in page_items:
        sid = supplement["id"]
        date_str = showing_date.isoformat()
        count = int(counts.get(sid, 1 if sid in taken_ids else 0))
        counted = supplement_target(supplement) > 1
        # A counted row always adds; a plain one still toggles, so the check-off
        # everyone already knows keeps behaving exactly as it did.
        action = "supp_c" if counted or sid not in taken_ids else "supp_u"
        label = supplement_progress_label(supplement, count)
        row = [
            InlineKeyboardButton(
                f"{label}{supplement_dose_label(supplement)}",
                callback_data=f"{action}_{user_id}_{sid}_{date_str}{page_suffix}",
            )
        ]
        if counted and count > 0:
            row.append(
                InlineKeyboardButton(
                    "➖",
                    callback_data=(
                        f"supp_m_{user_id}_{sid}_{date_str}{page_suffix}"
                    ),
                )
            )
        rows.append(row)

    date_label = showing_date.strftime("%b %d")
    rows.append(
        [
            InlineKeyboardButton(
                f"📅 {date_label} ({'Today' if is_today else 'Yesterday'})",
                callback_data=f"supp_noop_{user_id}_date",
            )
        ]
    )

    if is_today:
        rows.append(
            [
                InlineKeyboardButton(
                    "← Yesterday",
                    callback_data=f"supp_toggle_{user_id}_yesterday{page_suffix}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "→ Today",
                    callback_data=f"supp_toggle_{user_id}_today{page_suffix}",
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
                        f"supp_page_{user_id}_{showing_date.isoformat()}_"
                        f"{current_page - 1}"
                    ),
                )
            )
        if current_page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    "Next →",
                    callback_data=(
                        f"supp_page_{user_id}_{showing_date.isoformat()}_"
                        f"{current_page + 1}"
                    ),
                )
            )
        rows.append(navigation)

    return InlineKeyboardMarkup(rows)


#: Supplement setup draws *three* buttons per row (name, 🎯, ❌), so it cannot
#: use the checklist's page size: 49 supplements would be 147 buttons and
#: Telegram rejects a keyboard over 100 outright — the whole screen, not the
#: overflow. 32 rows leaves room for navigation and the back button.
SUPPLEMENT_SETUP_PAGE_SIZE = 32


def paginate_supplement_setup(
    supplements: list[dict], page: int = 0
) -> tuple[list[dict], int, int]:
    """One Telegram-safe page of the supplement setup list, clamped."""
    if len(supplements) <= SUPPLEMENT_SETUP_PAGE_SIZE:
        return supplements, 0, 1
    page_count = (
        len(supplements) + SUPPLEMENT_SETUP_PAGE_SIZE - 1
    ) // SUPPLEMENT_SETUP_PAGE_SIZE
    normalized = min(max(page, 0), page_count - 1)
    start = normalized * SUPPLEMENT_SETUP_PAGE_SIZE
    return (
        supplements[start : start + SUPPLEMENT_SETUP_PAGE_SIZE],
        normalized,
        page_count,
    )


def supplement_setup_keyboard(
    supplements: list[dict],
    user_id: int,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Supplement setup view with per-supplement target and remove buttons.

    The 🎯 button is where a daily target is set, because a target is setup — it
    describes what you are aiming at, not what happened today, and the checklist
    is no place to be editing intentions while ticking things off.
    """
    rows: list[list[InlineKeyboardButton]] = []

    page_items, current_page, page_count = paginate_supplement_setup(supplements, page)
    page_suffix = f"_p{current_page}" if page_count > 1 else ""

    for supplement in page_items:
        sid = supplement["id"]
        target = supplement_target(supplement)
        rows.append(
            [
                InlineKeyboardButton(
                    f"💊 {supplement['name']}{supplement_dose_label(supplement)}",
                    callback_data=f"supp_noop_{user_id}_{sid}",
                ),
                InlineKeyboardButton(
                    f"🎯 {target}×" if target > 1 else "🎯",
                    callback_data=f"supp_tgt_{user_id}_{sid}{page_suffix}",
                ),
                InlineKeyboardButton(
                    "❌ Remove",
                    callback_data=f"supp_remove_{user_id}_{sid}{page_suffix}",
                ),
            ]
        )

    if page_count > 1:
        navigation: list[InlineKeyboardButton] = []
        if current_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "← Previous",
                    callback_data=f"supp_setup_page_{user_id}_{current_page - 1}",
                )
            )
        if current_page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    "Next →",
                    callback_data=f"supp_setup_page_{user_id}_{current_page + 1}",
                )
            )
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                "🔙 Back to Supplements",
                callback_data=f"supp_setup_done_{user_id}",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


#: The daily targets offered as taps. Anything rarer than "once" is not a target
#: and anything above this is better typed than tapped — the point of the row is
#: that the common answers (two scoops, three) are one tap away.
SUPPLEMENT_TARGET_CHOICES = (2, 3, 4, 5, 6)


def supplement_target_keyboard(
    supplement: dict,
    user_id: int,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Pick how many times a day one supplement is aimed at.

    ``1×`` is offered as an explicit choice rather than a "cancel", because
    turning a target *off* is a decision with a consequence — the row goes back
    to a single check-off and its streak stops asking for the second dose.
    """
    sid = supplement["id"]
    current = supplement_target(supplement)
    page_suffix = f"_p{page}" if page else ""

    def _cell(value: int) -> InlineKeyboardButton:
        label = f"{value}×" if value > 1 else "1× (off)"
        return InlineKeyboardButton(
            f"✓ {label}" if value == current else label,
            callback_data=f"supp_tset_{user_id}_{sid}_{value}{page_suffix}",
        )

    rows = [[_cell(1)], [_cell(value) for value in SUPPLEMENT_TARGET_CHOICES]]
    rows.append(
        [
            InlineKeyboardButton(
                "🔙 Back to Setup",
                callback_data=f"supp_setup_page_{user_id}_{page}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


#: Callback prefix for the day-by-day meal breakdown. Its own family: ``meal_``
#: already means "pick a meal type" and ``menu_`` opens a section, so a third
#: shape sharing either prefix would be one typo away from routing into a write.
MEAL_DAY_PREFIX = "mday"


def meal_day_data(user_id: int, day: date) -> str:
    """Encode one day as ``mday_<owner36>_<YYYYMMDD>``.

    The date travels in full rather than as an offset from today: an offset
    would mean a different day tomorrow, so a keyboard left in the scrollback
    overnight would quietly answer about the wrong day.
    """
    return f"{MEAL_DAY_PREFIX}_{to_base36(user_id)}_{day.strftime('%Y%m%d')}"


def parse_meal_day(data: str, user_id: int) -> date | None:
    """Decode an ``mday_*`` callback to its date, or ``None``."""
    parts = (data or "").split("_")
    if len(parts) != 3 or parts[0] != MEAL_DAY_PREFIX:
        return None
    stamp = parts[2]
    # Exactly eight digits, checked before slicing: "2026081" would otherwise
    # slice cleanly into 2026-08-1 and answer confidently about a day nobody
    # asked for.
    if len(stamp) != 8 or not stamp.isdigit():
        return None
    try:
        owner = parse_base36(parts[1])
        day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
    except (TypeError, ValueError):
        return None
    if owner != user_id:
        return None
    return day


def meal_day_keyboard(
    user_id: int, day: date, *, has_next: bool = True
) -> InlineKeyboardMarkup:
    """Step to the day before or after the one being shown.

    There is no forward button on today: the next day holds nothing, and a
    button that can only ever answer "nothing logged" invites the reading that
    something was lost.
    """
    previous = day - timedelta(days=1)
    row = [
        InlineKeyboardButton(
            f"◀️ {previous.strftime('%d %b')}",
            callback_data=meal_day_data(user_id, previous),
        )
    ]
    if has_next:
        following = day + timedelta(days=1)
        row.append(
            InlineKeyboardButton(
                f"{following.strftime('%d %b')} ▶️",
                callback_data=meal_day_data(user_id, following),
            )
        )
    return InlineKeyboardMarkup([row])


def summary_breakdown_keyboard(user_id: int, day: date) -> InlineKeyboardMarkup:
    """The one control on a daily summary: show that day's food item by item."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🍽️ Meal breakdown", callback_data=meal_day_data(user_id, day)
                )
            ]
        ]
    )


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
                InlineKeyboardButton("⚖️ Weight Chart", callback_data="chart_weight"),
            ],
            [
                InlineKeyboardButton("🔥 Streaks", callback_data="analytics_streak"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Weight entry
# ---------------------------------------------------------------------------
#: Callback prefix for a tappable weight. The value travels as an integer number
#: of *hundredths* of a kilogram: callback data is a string either way, and an
#: integer cannot pick up a locale's decimal comma or a float's repr on the way
#: back.
WEIGHT_TAP_PREFIX = "wt"

#: Nudge buttons per row. Three keeps a ``72.35``-width label readable on a
#: phone and renders the nine-value ladder as a square with the unchanged
#: weight in the middle.
WEIGHT_GRID_COLUMNS = 3


def weight_tap_data(user_id: int, weight_kg: float) -> str:
    """Encode one tappable weight as ``wt_v_<user>_<hundredths>``."""
    return f"{WEIGHT_TAP_PREFIX}_v_{user_id}_{int(round(float(weight_kg) * 100))}"


def weight_clear_data(user_id: int) -> str:
    """Encode the "remove today's entry" tap."""
    return f"{WEIGHT_TAP_PREFIX}_x_{user_id}"


def parse_weight_tap(data: str, user_id: int) -> tuple[str, float | None] | None:
    """Decode a ``wt_*`` callback, or ``None`` if it is not this user's button.

    Returns ``(action, weight_kg)`` where ``action`` is ``"v"`` (log this value)
    or ``"x"`` (clear the day). The user check is what stops one household member
    tapping a button rendered for the other in a shared chat.
    """
    parts = (data or "").split("_")
    if len(parts) < 3 or parts[0] != WEIGHT_TAP_PREFIX:
        return None
    action = parts[1]
    try:
        if int(parts[2]) != user_id:
            return None
    except ValueError:
        return None

    if action == "x":
        return ("x", None)
    if action != "v" or len(parts) < 4:
        return None
    try:
        hundredths = int(parts[3])
    except ValueError:
        return None
    return ("v", round(hundredths / 100, 2))


def weight_entry_keyboard(
    user_id: int,
    last_kg: float | None,
    *,
    has_today: bool = False,
) -> InlineKeyboardMarkup | None:
    """Nudge buttons around ``last_kg``, or ``None`` when there is nothing to nudge.

    Each button is labelled with the weight it would log, and the grid ascends
    left to right and top to bottom, so what a tap does is readable without a
    legend. Returning ``None`` for a first-ever weigh-in is deliberate: a grid
    centred on a guess would invite a tap that records a number nobody measured.

    ``WEIGHT_GRID_COLUMNS`` values per row. The nine-value ±0.4 kg ladder in one
    row would squeeze each label past legibility on a phone; three rows of three
    keep the labels full width *and* put the unchanged weight in the visual
    centre, which is where the hand goes on a day the scale has not moved.
    """
    values = nudge_values(last_kg)
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(values), WEIGHT_GRID_COLUMNS):
        rows.append(
            [
                InlineKeyboardButton(
                    format_kg(value), callback_data=weight_tap_data(user_id, value)
                )
                for value in values[start : start + WEIGHT_GRID_COLUMNS]
            ]
        )
    if has_today:
        rows.append(
            [
                InlineKeyboardButton(
                    "🗑 Clear today's weight",
                    callback_data=weight_clear_data(user_id),
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------
#: Callback prefix for the monitor board. Its own family — distinct from
#: ``habit_`` and ``supp_`` — because a monitor tap is *not* idempotent: routing
#: one into a checklist handler would turn "already done" into a second
#: occurrence. The date rides along so a board left in the chat overnight is
#: recognisably stale rather than silently filing today's tap under yesterday.
MONITOR_PREFIX = "mon"

#: How many recent variants become quick-tap buttons in the detail prompt.
MONITOR_VARIANT_CHOICES = 3


def monitor_tap_data(
    user_id: int, action: str, monitor_id: int, extra: int | None = None
) -> str:
    """Encode one monitor button as ``mon_<action>_<user>_<id>[_<extra>]``."""
    tail = "" if extra is None else f"_{int(extra)}"
    return f"{MONITOR_PREFIX}_{action}_{user_id}_{int(monitor_id)}{tail}"


def parse_monitor_tap(
    data: str, user_id: int
) -> tuple[str, int, int | None] | None:
    """Decode a ``mon_*`` callback, or ``None`` if it is not this user's button.

    Returns ``(action, monitor_id, extra)``. The user check is what stops one
    household member tapping a button rendered for the other in a shared chat.
    """
    parts = (data or "").split("_")
    if len(parts) < 4 or parts[0] != MONITOR_PREFIX:
        return None
    action = parts[1]
    try:
        if int(parts[2]) != user_id:
            return None
        monitor_id = int(parts[3])
        extra = int(parts[4]) if len(parts) > 4 else None
    except ValueError:
        return None
    return action, monitor_id, extra


def monitor_label(monitor: dict) -> str:
    """The monitor's name with its glyph, if it has one."""
    emoji = (monitor.get("emoji") or "").strip()
    name = str(monitor.get("name") or "").strip()
    return f"{emoji} {name}".strip()


def monitor_board_keyboard(
    monitors: Sequence[dict],
    user_id: int,
    today_counts: dict[int, int],
) -> InlineKeyboardMarkup | None:
    """One row per monitor: log an occurrence, optionally with detail, and undo.

    The name lives *inside* the logging button rather than on a label row above
    it. Four monitors would otherwise be eight rows, and the board is meant to be
    read and tapped in one glance.

    Undo appears only once the day has something to undo. That is not decoration:
    an occurrence is not idempotent, so the button that fixes a double tap has to
    be next to the button that caused it.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for monitor in monitors:
        monitor_id = int(monitor["id"])
        row = [
            InlineKeyboardButton(
                f"{monitor_label(monitor)} +1",
                callback_data=monitor_tap_data(user_id, "a", monitor_id),
            )
        ]
        if monitor.get("tracks_quantity") or monitor.get("tracks_variant"):
            row.append(
                InlineKeyboardButton(
                    "📝",
                    callback_data=monitor_tap_data(user_id, "d", monitor_id),
                )
            )
        if today_counts.get(monitor_id, 0) > 0:
            row.append(
                InlineKeyboardButton(
                    "↩️",
                    callback_data=monitor_tap_data(user_id, "z", monitor_id),
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


def monitor_variant_keyboard(
    user_id: int, monitor_id: int, variants: Sequence[str]
) -> InlineKeyboardMarkup | None:
    """Quick-tap buttons for recently used variants, plus a way out.

    A variant travels as its *index* in this list, never as its text: callback
    data is 64 bytes and a strain name is arbitrary user text. The handler
    re-reads the same ordered list to resolve the index, so the button means the
    same thing when it is tapped as it did when it was drawn.
    """
    rows = [
        [
            InlineKeyboardButton(
                f"🔁 {variant}",
                callback_data=monitor_tap_data(user_id, "v", monitor_id, index),
            )
        ]
        for index, variant in enumerate(variants[:MONITOR_VARIANT_CHOICES])
    ]
    rows.append(
        [
            InlineKeyboardButton(
                "✖️ Cancel",
                callback_data=monitor_tap_data(user_id, "x", monitor_id),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def monitor_setup_keyboard(
    monitors: Sequence[dict], user_id: int
) -> InlineKeyboardMarkup:
    """Monitor setup: each monitor beside its remove button, then Done."""
    rows: list[list[InlineKeyboardButton]] = []
    for monitor in monitors:
        monitor_id = int(monitor["id"])
        rows.append(
            [
                InlineKeyboardButton(
                    monitor_label(monitor),
                    callback_data=monitor_tap_data(user_id, "noop", monitor_id),
                ),
                InlineKeyboardButton(
                    "❌",
                    callback_data=monitor_tap_data(user_id, "rm", monitor_id),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Done",
                callback_data=monitor_tap_data(user_id, "done", 0),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# App-suggestion receipt
# ---------------------------------------------------------------------------
# "Suggestion" already means a ranked food row elsewhere in this module
# (``SUGGESTION_PAGE_SIZE``, ``paginate_choices``). Everything to do with a
# suggestion *about the app* carries the ``app_`` qualifier, here and in the
# database layer, so the two never read as the same thing.
#
#: Callback prefix for withdrawing a filed suggestion. Its own family, distinct
#: from ``sc_`` (shortcuts) and ``supp_`` (supplements), so no pattern can route
#: a tap into the wrong table.
APP_SUGGESTION_PREFIX = "sug"


def app_suggestion_remove_data(user_id: int, suggestion_id: int) -> str:
    """Encode "withdraw this suggestion" as ``sug_x_<user>_<id>``."""
    return f"{APP_SUGGESTION_PREFIX}_x_{user_id}_{int(suggestion_id)}"


def parse_app_suggestion_remove(data: str, user_id: int) -> int | None:
    """Decode a ``sug_x_*`` callback, or ``None`` if it is not this user's.

    The owner is carried in the payload and matched here so that a button
    rendered for one household member cannot be acted on by the other; the
    delete itself is scoped by owner again in SQL.
    """
    parts = (data or "").split("_")
    if len(parts) != 4 or parts[0] != APP_SUGGESTION_PREFIX or parts[1] != "x":
        return None
    try:
        owner = int(parts[2])
        suggestion_id = int(parts[3])
    except ValueError:
        return None
    if owner != user_id or suggestion_id <= 0:
        return None
    return suggestion_id


def app_suggestion_receipt_keyboard(
    user_id: int, suggestion_id: int
) -> InlineKeyboardMarkup:
    """The one control a filed suggestion needs: take it back.

    Offered because a suggestion is sent in one shot with no confirmation step —
    the price of that speed is that a half-finished thought can land, and the
    person who sent it should be able to remove it themselves.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑 Withdraw",
                    callback_data=app_suggestion_remove_data(user_id, suggestion_id),
                )
            ]
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
