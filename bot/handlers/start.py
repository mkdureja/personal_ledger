"""
/start, /help, /menu handlers, and the Home action callbacks.

``/start`` and ``/menu`` both delegate to :func:`bot.handlers.home.open_home`, so
there is exactly one idle surface and only one place that decides how to behave
during a live guided flow.
"""

from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .common import (
    active_conversation_flow,
    authorized_callback,
    describe_abandoned_work,
    finish_conversation,
    reply_html,
)
from ..config import phase1_enabled_for
from ..keyboards import analytics_keyboard

#: Home actions whose ConversationHandler entry point normally claims the tap.
_CONVERSATION_MENU_ACTIONS = frozenset(
    {"menu_study", "menu_gym", "menu_diet", "menu_weight"}
)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
_WELCOME = (
    "👋 Welcome to <b>Ledger</b> — your private log for study, workouts, meals, "
    "and habits.\nTap a button below any time, or send /help for everything."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/start`` — onboard if needed, then show the one idle Home surface.

    ``/start`` used to hand back a wall of text with no buttons. It now prepends a
    one-time welcome to Home so the actions arrive immediately, and only for a
    genuinely first-ever start; a returning user gets plain Home.

    Both onboarding calls are preserved. A missing settings row already reads as
    reminder opt-out, but keeping explicit initialization avoids changing
    onboarding or future settings behavior.
    """
    from .home import open_home

    # No active-flow refusal here: ``open_home`` ends whatever is live and
    # reports anything unsaved. /start is one of the ways out.
    db = context.bot_data["db"]
    user = update.effective_user
    first_ever = await db.get_user_settings(user.id) is None
    await db.ensure_user(user.id, user.username, user.first_name)
    # New users default to reminder opt-out; they enable via /reminders on.
    await db.ensure_user_settings(user.id, default_enabled=False)

    await open_home(update, context, prelude=_WELCOME if first_ever else None)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full command reference.

    The fast-logging block is shown only to users the Phase 1 flag covers, so
    help never advertises an action that would answer "not enabled". Describe,
    voice, and online lookup stay unmentioned until they exist.
    """
    fast_block = ""
    if phase1_enabled_for(update.effective_user.id):
        fast_block = (
            "<b>Fast logging</b>\n"
            "🍽️ <b>Meal</b> — tap a food, tap an amount, done\n"
            "🔁 <b>Repeat last meal</b> — re-log your last meal exactly as it was\n"
            "⚙️ beside a saved item — set the “usual” amount, then it is one tap\n"
            "⚡ on a row means <b>one tap logs it now</b>, at the amount shown; a "
            "row without ⚡ asks how much first, and 🛠 means that item's usual "
            "needs fixing\n"
            "On any receipt: ↩️ <b>Undo</b> (that exact meal, within 24h), "
            "🍽️ <b>Log another</b>, 🔄 <b>Log again at today's values</b>\n"
            "<code>/keyboard hide|show</code> — the quick-action bar. Hiding is "
            "momentary; it returns on your next greeting.\n\n"
        )
    text = (
        "📋 <b>All Commands</b>\n\n"
        "<b>Start here</b>\n"
        "<code>/home</code> — today's totals and the action buttons. "
        "<code>/start</code>, <code>/menu</code>, and saying <b>hi</b> all open the "
        "same page.\n\n"
        f"{fast_block}"
        "<b>Logging</b>\n"
        "<code>/study</code> — Log a study session\n"
        "<code>/study &lt;subject&gt; &lt;minutes&gt; [notes]</code> — Quick log\n"
        "<code>/gym</code> — Log gym exercises\n"
        "<code>/gym &lt;exercise&gt; &lt;sets&gt; &lt;reps&gt; [weight]</code> — Quick log\n"
        "<code>/diet</code> — Log a meal\n"
        "<code>/diet &lt;meal&gt; &lt;food&gt; [calories] "
        "[p=&lt;g&gt; c=&lt;g&gt; f=&lt;g&gt;]</code> — Quick log\n"
        "<code>/describe &lt;items&gt;</code> — Type a whole meal at once\n"
        "<code>/aiparse on|off</code> — Optional AI help reading meals (off by default)\n"
        "<code>/food</code> — Manage saved foods and portions\n"
        "<code>/recipe</code> — Manage saved recipes\n\n"
        "<b>Weight</b>\n"
        "<code>/weight</code> — Log today's weight (tap a number or type one)\n"
        "<code>/weight 72.4</code> — Log it in one message\n"
        "One entry per day; weighing again corrects the day. Missed days are "
        "carried forward on the chart for up to 10 days.\n\n"
        "<b>Habits</b>\n"
        "<code>/habits</code> — Check off today's habits\n"
        "<code>/habits setup</code> — Add/remove habits\n\n"
        "<b>Supplements</b>\n"
        "<code>/supplements</code> — Check off today's supplements\n"
        "<code>/supplements setup</code> — Add/remove supplements\n\n"
        "<b>Monitors</b>\n"
        "<code>/monitor</code> — Log something you're keeping an eye on\n"
        "<code>/monitor setup</code> — Add/remove monitors and their targets\n"
        "A monitor counts occurrences against a target — <i>zero</i>, "
        "<i>1/month</i>, <i>2-4/week</i>, <i>≤5/day</i>. Unlike a habit, each "
        "tap adds one, so ↩️ removes the last one.\n\n"
        "<b>Analytics</b>\n"
        "<code>/summary</code> — Today's summary\n"
        "<code>/summary week</code> — Weekly summary\n"
        "<code>/chart study</code> — Study chart (7 days)\n"
        "<code>/chart gym</code> — Gym volume chart\n"
        "<code>/chart diet</code> — Diet calories chart\n"
        "<code>/chart habits</code> — Habit heatmap (14 days)\n"
        "<code>/chart weight</code> — Weight trend (30 days)\n"
        "<code>/streak</code> — Current habit streaks\n\n"
        "<b>Other</b>\n"
        "<code>/recent</code> — Your latest logged entries\n"
        "<code>/undo</code> — Delete last log entry (within 24h)\n"
        "<code>/reminders on|off</code> — Turn reminders on or off\n"
        "<code>/suggestions on|off|reset</code> — Personalized food ordering\n"
        "<code>/suggest &lt;idea&gt;</code> — Tell me what this bot should do "
        "differently (send <code>/suggest</code> alone to be asked for it)\n"
        "<code>/settings</code> — View your settings\n"
        "<code>/cancel</code> — Cancel current conversation\n"
        "<code>/home</code> — Today and actions\n"
        "<code>/help</code> — This message"
    )
    await reply_html(update.message, text)


# ---------------------------------------------------------------------------
# /menu
# ---------------------------------------------------------------------------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/menu`` — an alias for Home, so both habits lead to the same place."""
    from .home import open_home

    await open_home(update, context)


# ---------------------------------------------------------------------------
# Menu callback handler
# ---------------------------------------------------------------------------
@authorized_callback
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the main menu InlineKeyboard.

    Study/Gym/Diet taps are consumed by their ConversationHandlers' callback
    entry points (registered before this handler), so they enter the guided
    flow directly and never reach here. This handler serves the remaining
    non-conversation categories.
    """
    from .home import open_home

    query = update.callback_query
    data = query.data or ""
    flow = active_conversation_flow(context)

    # A Study/Gym/Diet tap normally never reaches here — their ConversationHandler
    # entry points claim it first. But an *active* conversation offers only its
    # state handlers, so while one is live the same tap falls through here.
    #
    # It used to answer "finish this flow or /cancel first", which made a current
    # button look broken. The flow is ended instead, and Home is re-rendered so
    # the buttons are live again — the section cannot be entered from this
    # handler (only a ConversationHandler entry point can do that), so the tap is
    # a way *out* rather than a way across.
    if flow is not None and data in _CONVERSATION_MENU_ACTIONS:
        await query.answer()
        await open_home(update, context)
        return

    valid_actions = {
        "menu_habits",
        "menu_supplements",
        "menu_monitors",
        "menu_analytics",
        "menu_recent",
    }
    if data not in valid_actions:
        await query.answer("This menu is no longer valid.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    await query.answer()

    # These sections open no conversation of their own, so an active flow can be
    # ended and the tap honoured in one step.
    if flow is not None:
        note = describe_abandoned_work(context, flow)
        finish_conversation(update, context)
        if note:
            await reply_html(query.message, f"✖️ {note}")

    if data == "menu_habits":
        # Import here to avoid circular imports
        from .habits import show_habits_checklist
        await show_habits_checklist(query.message, context, update.effective_user.id)
    elif data == "menu_supplements":
        from .supplements import show_supplements_checklist

        await show_supplements_checklist(
            query.message, context, update.effective_user.id
        )
    elif data == "menu_monitors":
        from .monitors import show_monitor_board

        await show_monitor_board(query.message, context, update.effective_user.id)
    elif data == "menu_recent":
        from .recent import show_recent

        await show_recent(query.message, context, update.effective_user.id)
    elif data == "menu_analytics":
        await reply_html(
            query.message,
            "📊 <b>Analytics</b> — Choose a report:",
            reply_markup=analytics_keyboard(),
        )
