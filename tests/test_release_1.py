"""Release 1 — one idle Home, honest instant-write labels, no false targets.

Three properties are worth more than the rest and are asserted from several
angles:

1. **One surface.** ``/start``, ``/home``, ``/menu``, and a supported greeting all
   render the same Home, and none of them can replace or end a live guided draft.
2. **A tap that writes looks like it writes.** In Quick Meal a stored usual makes
   the tap log immediately, so the row is marked ⚡ with the exact amount. The
   identical source in the Builder — where the tap only opens amount selection —
   renders normally.
3. **No dead targets.** The obvious thing to tap on a habit checklist is the
   habit's name, and it now toggles that habit; a zero-result search offers real
   exits instead of a dead end.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from telegram import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReplyKeyboardRemove,
    Update,
    User,
)
from telegram.constants import ChatType
from telegram.ext import ExtBot

from bot import keyboards, main as main_module, suggestions
from bot.config import today_local
from bot.database import DatabaseManager
from bot.handlers import diet, habits, home, start
from bot.handlers.common import activate_conversation, active_conversation_flow
from bot.handlers.diet import diet_conv_handler
from bot.handlers.gym import EXERCISE, gym_conv_handler
from bot.handlers.habits import ADDING_HABIT, habits_setup_conv_handler
from bot.handlers.study import SUBJECT, study_conv_handler
from bot.handlers.weight import ASK as WEIGHT_ASK, weight_conv_handler
from bot.meal_models import RepeatStatus

UID = 123456789  # matches conftest ALLOWED_USER_IDS
OTHER = 987654321

_BOT = MagicMock()
_BOT.username = "LedgerTestBot"

_FLOW_STATES = {
    "study": (study_conv_handler, SUBJECT),
    "gym": (gym_conv_handler, EXERCISE),
    "diet": (diet_conv_handler, diet.FOOD_CHOICE),
    "habits": (habits_setup_conv_handler, ADDING_HABIT),
    "weight": (weight_conv_handler, WEIGHT_ASK),
}


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------
def _update(text: str | None = None, user_id: int = UID):
    # reply_text returns a message-like object: the diet flow records the id of the
    # keyboard it just sent so the next tap can be validated against it.
    message = SimpleNamespace(
        text=text,
        message_id=100,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=100)),
    )
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        callback_query=None,
        update_id=1,
    )


def _context(db=None, user_data=None, args=None):
    return SimpleNamespace(
        bot_data={"db": db},
        user_data={} if user_data is None else user_data,
        args=args or [],
    )


def _snapshot_db(last_meal=None):
    return SimpleNamespace(
        get_today_meal_count=AsyncMock(return_value=1),
        get_today_calories=AsyncMock(return_value=(400, False)),
        get_today_study_total=AsyncMock(return_value=0),
        get_today_gym_count=AsyncMock(return_value=0),
        get_active_habits=AsyncMock(return_value=[]),
        get_checked_habits=AsyncMock(return_value=set()),
        get_last_meal_summary=AsyncMock(return_value=last_meal),
    )


def _enable_phase1(monkeypatch, *, keyboard="on"):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", keyboard)
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_PILOT_USER_IDS", frozenset())


def _labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _first_kwargs(mock) -> dict:
    return mock.call_args_list[0].kwargs


@pytest_asyncio.fixture
async def real_dispatch_app():
    """A real handler table usable through ``Application.process_update``.

    ``initialize()`` would call Telegram's ``getMe`` endpoint. The routing tests
    instead mark the locally constructed application initialized and replace only
    the Bot API methods that the selected handlers call. ConversationHandler,
    CallbackContext, handler ordering, filters, and state transitions all remain
    production objects.

    A real database is attached because escaping a flow now *renders* Home rather
    than refusing, so these routing tests reach the Today snapshot query.
    """
    for handler, _state in _FLOW_STATES.values():
        handler._conversations.clear()
    app = main_module.build_application()
    manager = DatabaseManager(":memory:")
    await manager.connect()
    await manager.init_db()
    await manager.ensure_user(UID, "t", "Test")
    app.bot_data["db"] = manager
    # CommandHandler needs the cached getMe result to parse /command@bot. Seed
    # only that immutable identity instead of performing a network initialize.
    app.bot._bot_user = User(
        id=999,
        is_bot=True,
        first_name="Ledger",
        username="LedgerTestBot",
    )
    app._initialized = True
    try:
        yield app
    finally:
        app._initialized = False
        app._user_data.clear()
        app.bot_data.clear()
        await manager.close()
        for handler, _state in _FLOW_STATES.values():
            handler._conversations.clear()


def _real_text_update(app, text: str) -> Update:
    chat = Chat(id=UID, type="private")
    user = User(id=UID, is_bot=False, first_name="Test", username="t")
    entities = None
    if text.startswith("/"):
        command = text.split()[0]
        entities = [
            MessageEntity(
                type=MessageEntity.BOT_COMMAND, offset=0, length=len(command)
            )
        ]
    message = Message(
        message_id=100,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=entities,
    )
    message.set_bot(app.bot)
    return Update(update_id=1, message=message)


def _real_callback_update(app, data: str) -> Update:
    chat = Chat(id=UID, type="private")
    user = User(id=UID, is_bot=False, first_name="Test", username="t")
    message = Message(
        message_id=100,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
    )
    message.set_bot(app.bot)
    query = CallbackQuery(
        id="release-1-query",
        from_user=user,
        chat_instance="release-1-chat",
        data=data,
        message=message,
    )
    query.set_bot(app.bot)
    return Update(update_id=1, callback_query=query)


def _seed_real_flow(app, flow: str) -> tuple[object, int]:
    handler, state = _FLOW_STATES[flow]
    handler._conversations[(UID, UID)] = state
    app._user_data[UID].update(
        {
            "_ledger_active_conversation": (flow, UID),
            "release_1_draft_probe": "keep me",
        }
    )
    return handler, state


# ---------------------------------------------------------------------------
# §1.1 One idle Home
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry", ["start", "home", "menu", "greeting"])
async def test_every_idle_entry_renders_the_same_home(db, entry):
    """One surface: no entry point is a dead end that shows different buttons."""
    await db.ensure_user(UID, "t", "Test")
    update = _update("hi")
    context = _context(db)

    if entry == "start":
        await start.start_command(update, context)
    elif entry == "home":
        await home.home_command(update, context)
    elif entry == "menu":
        await start.menu_command(update, context)
    else:
        await home.home_text_router(update, context)

    text = update.effective_message.reply_text.call_args_list[0].args[0]
    markup = _first_kwargs(update.effective_message.reply_text)["reply_markup"]
    assert "here's today" in text
    assert _labels(markup) == [
        "🍽️ Log meal", "⚖️ Weight", "✅ Habits", "💊 Supplements",
        "🏋️ Workout", "📖 Study", "🗒️ Recent", "📊 Analytics",
    ]


@pytest.mark.parametrize("flow", ["study", "gym", "diet", "habits", "weight"])
@pytest.mark.parametrize("entry", ["start", "home", "menu", "greeting"])
async def test_every_idle_entry_escapes_a_live_flow(db, flow, entry):
    """Home always opens, from inside any flow, by any entry point.

    This asserted the opposite until live testing: Home refused while a flow was
    active, so the one button people reach for when they feel stuck was the one
    that would not respond, and the way out (``/cancel``) was the thing they had
    to already know. Home now ends the flow instead of blocking on it.
    """
    await db.ensure_user(UID, "t", "Test")
    update = _update("hi")
    context = _context(db)
    activate_conversation(update, context, flow)

    if entry == "start":
        await start.start_command(update, context)
    elif entry == "home":
        await home.home_command(update, context)
    elif entry == "menu":
        await start.menu_command(update, context)
    else:
        await home.home_text_router(update, context)

    said = " ".join(
        str(call.args[0])
        for call in update.effective_message.reply_text.call_args_list
        if call.args
    )
    assert "here's today" in said
    assert "Finish this flow" not in said
    assert active_conversation_flow(context) is None


async def test_escaping_a_flow_names_what_it_dropped(db):
    """Unsaved work is reported, not silently binned."""
    await db.ensure_user(UID, "t", "Test")
    update = _update("/home")
    context = _context(db)
    activate_conversation(update, context, "gym")
    context.user_data["gym_current_exercise"] = "Bench press"

    await home.home_command(update, context)

    said = " ".join(
        str(call.args[0])
        for call in update.effective_message.reply_text.call_args_list
        if call.args
    )
    assert "Bench press" in said
    assert "Dropped" in said


async def test_escaping_a_flow_with_nothing_pending_says_nothing_extra(db):
    """Announcing a loss that did not happen is noise."""
    await db.ensure_user(UID, "t", "Test")
    update = _update("/home")
    context = _context(db)
    activate_conversation(update, context, "gym")

    await home.home_command(update, context)

    said = " ".join(
        str(call.args[0])
        for call in update.effective_message.reply_text.call_args_list
        if call.args
    )
    assert "Dropped" not in said
    assert "here's today" in said


@pytest.mark.parametrize("flow", ["study", "gym", "diet", "habits", "weight"])
@pytest.mark.parametrize(
    "text", ["/start", "/home", "/menu", "hi"], ids=["start", "home", "menu", "greeting"]
)
async def test_real_dispatcher_escapes_every_active_flow(
    real_dispatch_app, monkeypatch, flow, text
):
    """Drive the actual application and ConversationHandler state matrix.

    The unit test above proves ``open_home`` ends the flow; this proves the
    dispatcher actually routes there. It is the part that could silently not
    work: PTB gives a live conversation first refusal on an update, so Home has
    to be registered in each conversation's ``fallbacks`` — a handler outside
    could never return ``END`` into it, and the state would linger and swallow
    the next ordinary message.
    """
    app = real_dispatch_app
    owner, _state = _seed_real_flow(app, flow)
    send_message = AsyncMock()
    monkeypatch.setattr(ExtBot, "send_message", send_message)

    await app.process_update(_real_text_update(app, text))

    assert send_message.await_count >= 1
    said = " ".join(
        str(call.kwargs.get("text", "")) for call in send_message.call_args_list
    )
    assert "here's today" in said
    assert "Finish" not in said
    # The conversation is really over, not merely hidden behind Home.
    assert owner._conversations.get((UID, UID)) is None
    assert "_ledger_active_conversation" not in app._user_data[UID]


async def test_real_dispatcher_handles_unknown_idle_text_once(
    real_dispatch_app, monkeypatch
):
    app = real_dispatch_app
    send_message = AsyncMock()
    monkeypatch.setattr(ExtBot, "send_message", send_message)

    await app.process_update(_real_text_update(app, "what did i eat"))

    assert send_message.await_count == 1
    assert "didn't recognize" in send_message.call_args.kwargs["text"]
    assert isinstance(
        send_message.call_args.kwargs["reply_markup"], InlineKeyboardMarkup
    )


async def test_first_ever_start_onboards_opted_out_and_still_shows_home(db):
    update = _update("/start")
    await start.start_command(update, _context(db))

    settings = await db.get_user_settings(UID)
    assert settings is not None and not settings["reminders_enabled"]
    text = update.effective_message.reply_text.call_args_list[0].args[0]
    assert "Welcome to <b>Ledger</b>" in text
    # The welcome is prepended to Home, not sent instead of it.
    assert "here's today" in text
    assert _first_kwargs(update.effective_message.reply_text)["reply_markup"] is not None


def test_command_menu_is_only_the_everyday_set():
    # /weight is here because "/weight 72.4" is a complete daily interaction on
    # its own; every other logging command opens a flow and belongs on Home.
    assert [command for command, _ in main_module.COMMAND_MENU] == [
        "home", "weight", "recent", "undo", "help",
    ]
    assert all(description for _, description in main_module.COMMAND_MENU)


async def test_post_init_publishes_the_picker_only_on_a_real_run(tmp_path, monkeypatch):
    """Publishing the picker is a Bot API call, so only ``main()`` asks for it."""
    from telegram.ext import ApplicationBuilder

    published: list[object] = []

    async def _spy(application):
        published.append(application)

    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(main_module, "BACKUP_DEST_DIR", "")
    monkeypatch.setattr(main_module, "register_command_menu", _spy)

    default_app = ApplicationBuilder().token("123456:TEST_TOKEN").build()
    await main_module.post_init(default_app)
    try:
        assert published == []
    finally:
        await main_module.post_shutdown(default_app)

    real_run_app = ApplicationBuilder().token("123456:TEST_TOKEN").build()
    await main_module.post_init(real_run_app, register_commands=True)
    try:
        assert published == [real_run_app]
    finally:
        await main_module.post_shutdown(real_run_app)


async def test_register_command_menu_sends_only_the_everyday_set():
    calls: list[list] = []

    class _Bot:
        async def set_my_commands(self, commands):
            calls.append(list(commands))

    await main_module.register_command_menu(SimpleNamespace(bot=_Bot()))

    assert [c.command for c in calls[0]] == [
        "home", "weight", "recent", "undo", "help",
    ]


async def test_register_command_menu_degrades_instead_of_failing_startup():
    class _Bot:
        async def set_my_commands(self, commands):
            raise RuntimeError("Bot API unreachable")

    # A picker that cannot be published costs discoverability, never the process.
    await main_module.register_command_menu(SimpleNamespace(bot=_Bot()))


async def test_home_names_the_meal_repeat_would_log(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update()
    db = _snapshot_db(
        {"id": 4, "meal_type": "lunch", "food_items": "Oats and milk", "calories": 350}
    )
    await home.show_home(update, _context(db))

    text = update.effective_message.reply_text.call_args_list[0].args[0]
    assert "Repeat last meal" in text and "Oats and milk" in text and "lunch" in text


async def test_home_bounds_a_long_last_meal_summary(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update()
    db = _snapshot_db({"meal_type": "dinner", "food_items": "rice " * 60})
    await home.show_home(update, _context(db))

    line = [
        part
        for part in update.effective_message.reply_text.call_args_list[0]
        .args[0]
        .splitlines()
        if "Repeat last meal" in part
    ][0]
    assert "…" in line and len(line) < 140


async def test_home_omits_the_last_meal_when_repeat_is_unavailable():
    """Phase 1 off: Home must not describe an action the user has no button for."""
    update = _update()
    db = _snapshot_db({"meal_type": "lunch", "food_items": "Oats"})
    await home.show_home(update, _context(db))

    text = update.effective_message.reply_text.call_args_list[0].args[0]
    assert "Repeat last meal" not in text
    db.get_last_meal_summary.assert_not_awaited()


async def test_last_meal_summary_matches_what_repeat_copies(db):
    """The Home label and the Repeat write must name the same row."""
    await db.ensure_user(UID, "t", "Test")
    await db.log_diet(UID, "breakfast", "Toast", 200, protein_g=1, carbs_g=2, fat_g=3)
    await db.log_diet(UID, "lunch", "Salad", 300, protein_g=1, carbs_g=2, fat_g=3)

    summary = await db.get_last_meal_summary(UID)
    result = await db.repeat_last_meal(UID)

    assert summary["food_items"] == "Salad"
    assert result.receipt.header.food_items == "Salad"


# ---------------------------------------------------------------------------
# §1.2 Honest instant-write labels
# ---------------------------------------------------------------------------
_FOOD = {"source_type": "food", "id": 5, "name": "Banana"}
_RECIPE = {"source_type": "recipe", "id": 7, "name": "Chicken curry"}


def _prefs(**rows):
    """Build a preference map like ``get_food_preferences`` returns."""
    return {
        ("food", 5): rows.get("food", {}),
        ("recipe", 7): rows.get("recipe", {}),
    }


def test_quick_row_with_a_usual_is_marked_and_shows_the_amount():
    decorated = suggestions.annotate_defaults(
        [_FOOD, _RECIPE],
        _prefs(
            food={"default_amount": 1.0, "default_unit": "medium"},
            recipe={"default_amount": 1.0, "default_unit": "serving"},
        ),
    )
    quick = [keyboards.choice_button_label(c, quick=True) for c in decorated]
    assert quick == ["⚡ Banana · 1 medium", "⚡ Chicken curry (recipe) · 1 serving"]

    # The identical sources in the Builder open amount selection, so they must
    # not wear the instant marker.
    builder = [keyboards.choice_button_label(c, quick=False) for c in decorated]
    assert builder == ["🥗 Banana", "🍲 Chicken curry (recipe)"]


def test_a_source_without_a_usual_renders_normally():
    decorated = suggestions.annotate_defaults([_FOOD, _RECIPE], _prefs())
    labels = [keyboards.choice_button_label(c, quick=True) for c in decorated]
    assert labels == ["🥗 Banana", "🍲 Chicken curry (recipe)"]


def test_a_broken_usual_reads_as_needing_repair_never_as_instant():
    decorated = suggestions.annotate_defaults(
        [_FOOD, _RECIPE],
        _prefs(
            food={"default_amount": 2.0, "default_unit": None},
            recipe={"default_amount": None, "default_unit": "serving"},
        ),
    )
    assert all(c["needs_repair"] and c["default"] is None for c in decorated)
    labels = [keyboards.choice_button_label(c, quick=True) for c in decorated]
    assert labels == [
        "🛠 Banana — fix usual",
        "🛠 Chicken curry (recipe) — fix usual",
    ]
    assert not any(keyboards.INSTANT_PREFIX in label for label in labels)


def test_a_catalog_row_with_a_usual_is_instant_from_v16():
    """This asserted the opposite until schema v16.

    A catalog preference was unstorable then, so a shared staple could never be
    a one-tap row and the only way to get one was a private copy of something
    the catalog already had. The row is still shared; the usual amount is this
    user's alone.
    """
    catalog = {"source_type": "catalog", "id": 3, "name": "Banana, raw"}
    decorated = suggestions.annotate_defaults(
        [catalog], {("catalog", 3): {"default_amount": 1.0, "default_unit": "g"}}
    )
    assert decorated[0]["default"] == {"amount": 1.0, "unit": "g"}
    assert keyboards.choice_button_label(decorated[0], quick=True).startswith("⚡ ")


def test_a_catalog_row_without_a_usual_is_still_a_plain_search_result():
    catalog = {"source_type": "catalog", "id": 3, "name": "Banana, raw"}
    decorated = suggestions.annotate_defaults([catalog], {})
    assert decorated[0]["default"] is None
    assert keyboards.choice_button_label(decorated[0], quick=True) == "🔎 Banana, raw"


def test_a_long_name_cannot_displace_the_consequence_or_the_amount():
    long_recipe = {
        "source_type": "recipe",
        "id": 7,
        "name": "Slow cooked chicken curry with extra coriander",
    }
    decorated = suggestions.annotate_defaults(
        [long_recipe], _prefs(recipe={"default_amount": 1.0, "default_unit": "serving"})
    )
    label = keyboards.choice_button_label(decorated[0], quick=True)
    assert label.startswith("⚡ ")
    assert label.endswith(" (recipe) · 1 serving")
    assert "…" in label  # only the dynamic name was truncated


def test_a_long_food_name_keeps_its_amount_visible():
    long_food = {
        "source_type": "food",
        "id": 5,
        "name": "Organic rolled oats from the big tin",
    }
    decorated = suggestions.annotate_defaults(
        [long_food], _prefs(food={"default_amount": 45.0, "default_unit": "g"})
    )
    label = keyboards.choice_button_label(decorated[0], quick=True)
    assert label.startswith("⚡ ") and label.endswith(" · 45 g")


def test_a_pathological_unit_stays_semantically_intact():
    """Only the source name may be shortened; the consequence is never rewritten."""
    unit = "family breakfast bowl with the blue patterned rim"
    long_food = {
        "source_type": "food",
        "id": 5,
        "name": "Organic rolled oats from the big tin",
    }
    decorated = suggestions.annotate_defaults(
        [long_food],
        _prefs(food={"default_amount": 1.0, "default_unit": unit}),
    )
    label = keyboards.choice_button_label(decorated[0], quick=True)
    assert label.startswith("⚡ Organic rol…")
    assert label.endswith(f" · 1 {unit}")
    assert len(label) > 40  # the cap is intentionally soft when truth needs room


def test_a_precise_amount_is_not_rounded_in_the_instant_label():
    amount = 0.123456789012345
    decorated = suggestions.annotate_defaults(
        [_FOOD],
        _prefs(food={"default_amount": amount, "default_unit": "medium"}),
    )
    assert keyboards.choice_button_label(decorated[0], quick=True).endswith(
        f"· {amount} medium"
    )


def test_amounts_render_without_a_needless_decimal():
    decorated = suggestions.annotate_defaults(
        [_FOOD], _prefs(food={"default_amount": 220.0, "default_unit": "g"})
    )
    assert keyboards.choice_button_label(decorated[0], quick=True).endswith("· 220 g")


def test_the_keyboard_marks_only_the_instant_rows():
    decorated = suggestions.annotate_defaults(
        [_FOOD, _RECIPE], _prefs(food={"default_amount": 1.0, "default_unit": "medium"})
    )
    markup = keyboards.food_choice_keyboard(UID, decorated, quick=True)
    labels = _labels(markup)
    assert labels[0] == "⚡ Banana · 1 medium"
    assert labels[1] == "🍲 Chicken curry (recipe)"


# --- one batched preference read per render path ---------------------------
def _picker_db(*, suggestions_on: bool, prefs=None):
    return SimpleNamespace(
        list_foods=AsyncMock(return_value=[{"id": 5, "name": "Banana", "name_key": "banana"}]),
        list_recipes=AsyncMock(
            return_value=[{"id": 7, "name": "Chicken curry", "name_key": "chicken curry"}]
        ),
        get_suggestions_enabled=AsyncMock(return_value=suggestions_on),
        get_user_catalog_history=AsyncMock(return_value=[]),
        get_meal_shortcuts=AsyncMock(return_value=set()),
        get_shortcut_targets=AsyncMock(return_value=[]),
        get_diet_item_stats=AsyncMock(return_value={}),
        get_food_preferences=AsyncMock(return_value=prefs or {}),
        search_catalog=AsyncMock(return_value=[]),
    )


@pytest.mark.parametrize("suggestions_on", [True, False])
async def test_choice_decoration_uses_one_batched_read(suggestions_on):
    """No per-row get_default_quantity / has_partial_default calls."""
    db = _picker_db(
        suggestions_on=suggestions_on,
        prefs={("food", 5): {"default_amount": 1.0, "default_unit": "medium"}},
    )
    context = _context(db)
    choices = await diet._ranked_choices(context, UID, "lunch")

    db.get_food_preferences.assert_awaited_once()
    assert not hasattr(db, "get_default_quantity")
    by_key = {(c["source_type"], c["id"]): c for c in choices}
    assert by_key[("food", 5)]["default"] == {"amount": 1.0, "unit": "medium"}
    assert by_key[("recipe", 7)]["default"] is None


async def test_search_results_are_decorated_from_one_batched_read(monkeypatch):
    _enable_phase1(monkeypatch)
    db = _picker_db(
        suggestions_on=True,
        prefs={("food", 5): {"default_amount": 1.0, "default_unit": "medium"}},
    )
    update = _update("banana")
    context = _context(db, {"diet_entry_mode": diet.DietEntryMode.QUICK})

    state = await diet.receive_search_query(update, context)

    assert state == diet.FOOD_CHOICE
    db.get_food_preferences.assert_awaited_once()
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert "⚡ Banana · 1 medium" in _labels(markup)


async def test_the_builder_picker_never_marks_a_row_instant(monkeypatch):
    """Same data, Builder entry mode: the tap opens amount selection."""
    _enable_phase1(monkeypatch)
    db = _picker_db(
        suggestions_on=True,
        prefs={("food", 5): {"default_amount": 1.0, "default_unit": "medium"}},
    )
    update = _update()
    context = _context(db, {"diet_entry_mode": diet.DietEntryMode.BUILDER})

    await diet._prompt_food_choice(update, context, update.effective_message, "lunch")

    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert "🥗 Banana" in _labels(markup)
    assert not any(label.startswith("⚡") for label in _labels(markup))


async def test_pagination_keeps_the_distinction(monkeypatch):
    """Every page of a long list is decorated, not just the first render."""
    _enable_phase1(monkeypatch)
    foods = [
        {"id": i, "name": f"Food {i:02d}", "name_key": f"food {i:02d}"}
        for i in range(1, 12)
    ]
    db = SimpleNamespace(
        list_foods=AsyncMock(return_value=foods),
        list_recipes=AsyncMock(return_value=[]),
        get_suggestions_enabled=AsyncMock(return_value=False),
        get_food_preferences=AsyncMock(
            return_value={("food", 11): {"default_amount": 2.0, "default_unit": "cup"}}
        ),
    )
    update = _update()
    context = _context(
        db, {"diet_entry_mode": diet.DietEntryMode.QUICK, "diet_choice_page": 1}
    )

    await diet._prompt_food_choice(update, context, update.effective_message, "lunch")

    labels = _labels(update.effective_message.reply_text.call_args.kwargs["reply_markup"])
    assert "⚡ Food 11 · 2 cup" in labels


# --- both persistent Repeat labels route during the compatibility window ---
@pytest.mark.parametrize("label", ["Repeat last meal", "Repeat", "repeat"])
async def test_both_repeat_labels_reach_the_repeat_action(monkeypatch, label):
    _enable_phase1(monkeypatch)
    db = _snapshot_db()
    db.ensure_user = AsyncMock()
    db.repeat_last_meal = AsyncMock(
        return_value=SimpleNamespace(status=RepeatStatus.EMPTY, receipt=None)
    )
    update = _update(label)
    await home.home_text_router(update, _context(db))

    db.repeat_last_meal.assert_awaited_once()


@pytest.mark.parametrize("label", ["Repeat last meal", "Repeat"])
def test_both_repeat_labels_are_intercepted_during_an_active_flow(label):
    """An old bar cannot bypass a live draft either."""
    from bot.handlers.common import ACTIVE_CONTROL_FILTER, DIET_NONMEAL_CONTROL_FILTER

    message = SimpleNamespace(text=label)
    assert ACTIVE_CONTROL_FILTER.filter(message)
    assert DIET_NONMEAL_CONTROL_FILTER.filter(message)


@pytest.mark.parametrize("label", ["Repeat last meal", "Repeat"])
async def test_a_disabled_repeat_label_retires_its_stale_keyboard(label):
    update = _update(label)

    await home.home_text_router(update, _context())

    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardRemove)
    assert "Not enabled yet" in update.effective_message.reply_text.call_args.args[0]


def test_the_persistent_bar_renders_the_new_label():
    rows = keyboards.home_reply_keyboard().keyboard
    assert [button.text for button in rows[0]] == ["Meal", "Repeat last meal"]


def test_a_repeat_label_still_routes_through_the_real_dispatcher():
    app = main_module.build_application()
    for label in ("Repeat last meal", "Repeat"):
        chat = Chat(id=UID, type="private")
        user = User(id=UID, is_bot=False, first_name="U")
        message = Message(
            message_id=1, date=datetime.now(timezone.utc), chat=chat,
            from_user=user, text=label,
        )
        message.set_bot(_BOT)
        update = Update(update_id=1, message=message)
        handler = next(
            (
                h
                for group in sorted(app.handlers)
                for h in app.handlers[group]
                if h.check_update(update)
            ),
            None,
        )
        assert getattr(handler.callback, "__name__", None) == "home_text_router"


# ---------------------------------------------------------------------------
# §1.3 No false targets or invisible exits
# ---------------------------------------------------------------------------
def test_each_habit_is_one_full_width_toggle():
    rows = keyboards.habit_checklist_keyboard(
        [{"id": 1, "habit_name": "Read"}, {"id": 2, "habit_name": "Walk"}],
        {2},
        date(2026, 7, 30),
        UID,
    ).inline_keyboard

    assert len(rows[0]) == 1 and len(rows[1]) == 1
    assert rows[0][0].text == "⬜ Read"
    assert rows[0][0].callback_data == f"habit_c_{UID}_1_2026-07-30"
    assert rows[1][0].text == "✅ Walk"
    assert rows[1][0].callback_data == f"habit_u_{UID}_2_2026-07-30"
    # No habit row carries an inert label any more.
    assert not any(
        (button.callback_data or "").startswith("habit_noop_")
        and button.callback_data != f"habit_noop_{UID}_date"
        for row in rows
        for button in row
    )


async def test_tapping_a_habit_name_toggles_only_that_users_date(db):
    await db.ensure_user(UID, "t", "Test")
    await db.ensure_user(OTHER, "o", "Other")
    mine, _ = await db.add_habit(UID, "Read")
    theirs, _ = await db.add_habit(OTHER, "Read")
    # The handler only accepts today or yesterday, so this date must track the
    # real clock; a frozen literal turns this test into a time bomb that starts
    # asserting the "expired checklist" path instead of the toggle.
    today = today_local()

    row = keyboards.habit_checklist_keyboard(
        [{"id": mine, "habit_name": "Read"}], set(), today, UID
    ).inline_keyboard[0][0]

    query = SimpleNamespace(
        data=row.callback_data,
        answer=AsyncMock(),
        message=SimpleNamespace(
            reply_text=AsyncMock(), edit_text=AsyncMock(), message_id=1
        ),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=UID, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
        update_id=1,
    )
    await habits.habit_check_callback(update, _context(db))

    assert mine in await db.get_checked_habits(UID, today)
    assert theirs not in await db.get_checked_habits(OTHER, today)


@pytest.mark.parametrize("target", ["date", "habit"])
async def test_a_stale_noop_label_never_mutates(db, target):
    await db.ensure_user(UID, "t", "Test")
    habit_id, _ = await db.add_habit(UID, "Read")
    suffix = "date" if target == "date" else str(habit_id)
    query = SimpleNamespace(
        data=f"habit_noop_{UID}_{suffix}",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=UID),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )

    await habits.habit_noop_callback(update, _context(db))

    query.answer.assert_awaited_once()
    assert await db.get_checked_habits(UID, today_local()) == set()
    if target == "habit":
        # An old checklist's habit label explains itself instead of looking dead.
        assert "refresh" in query.answer.call_args.args[0]


async def test_a_current_habit_setup_label_explains_setup_without_mutating(db):
    await db.ensure_user(UID, "t", "Test")
    habit_id, _ = await db.add_habit(UID, "Read")
    message = SimpleNamespace(
        chat_id=UID,
        message_id=44,
        reply_text=AsyncMock(),
    )
    query = SimpleNamespace(
        data=f"habit_noop_{UID}_{habit_id}",
        answer=AsyncMock(),
        message=message,
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=UID),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )
    context = _context(
        db,
        {
            "_ledger_active_conversation": ("habits", UID),
            "habit_setup_prompt": (UID, 44),
        },
    )

    await habits.habit_noop_callback(update, context)

    assert "Habit Setup" in query.answer.call_args.args[0]
    assert "Remove" in query.answer.call_args.args[0]
    assert await db.get_checked_habits(UID, today_local()) == set()
    query.edit_message_reply_markup.assert_not_awaited()


async def test_zero_result_search_offers_the_three_real_exits(monkeypatch):
    _enable_phase1(monkeypatch)
    db = SimpleNamespace(
        list_foods=AsyncMock(return_value=[]),
        list_recipes=AsyncMock(return_value=[]),
        search_catalog=AsyncMock(return_value=[]),
    )
    update = _update("zzzz")
    context = _context(db, {"diet_food_items": ["oats"]})

    state = await diet.receive_search_query(update, context)

    assert state == diet.SEARCH  # still able to type another word
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert _labels(markup) == ["◀️ Back to my items", "🔎 Search again", "✍️ Type it instead"]
    # The draft is intact: a dead end must not cost the user their work.
    assert context.user_data["diet_food_items"] == ["oats"]


@pytest.mark.parametrize(
    "data,expected",
    [
        ("dback_%d", "search_back_to_choices"),
        ("dsearch_%d", "start_search"),
        ("dtype_%d", "type_food_instead"),
    ],
)
def test_the_zero_result_exits_are_handled_in_the_search_state(data, expected):
    """A rendered button with no handler is the same dead end in a new costume."""
    handlers = diet_conv_handler.states[diet.SEARCH]
    chat = Chat(id=UID, type="private")
    user = User(id=UID, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user
    )
    message.set_bot(_BOT)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data % UID, message=message
    )
    query.set_bot(_BOT)
    update = Update(update_id=1, callback_query=query)

    matched = [h for h in handlers if h.check_update(update)]
    assert [getattr(h.callback, "__name__", None) for h in matched][:1] == [expected]


async def test_search_back_returns_to_the_picker_without_losing_the_draft(monkeypatch):
    _enable_phase1(monkeypatch)
    db = _picker_db(suggestions_on=False)
    query = SimpleNamespace(
        data=f"dback_{UID}",
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=100,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=101)),
        ),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_user=SimpleNamespace(id=UID, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )
    context = _context(
        db,
        {
            "diet_ui_message_id": 100,
            "diet_ui_revision": 0,
            "diet_food_items": ["oats"],
            "diet_meal_type": "lunch",
        },
    )

    state = await diet.search_back_to_choices(update, context)

    assert state == diet.FOOD_CHOICE
    assert context.user_data["diet_food_items"] == ["oats"]


async def test_a_stale_search_exit_stays_in_the_search_state():
    db = _picker_db(suggestions_on=False)
    query = SimpleNamespace(
        data=f"dback_{UID}",
        answer=AsyncMock(),
        message=SimpleNamespace(message_id=999, reply_text=AsyncMock()),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_user=SimpleNamespace(id=UID),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )
    # Tracked message id does not match: the tap came from a superseded screen.
    context = _context(db, {"diet_ui_message_id": 100, "diet_ui_revision": 0})

    assert await diet.search_back_to_choices(update, context) == diet.SEARCH


async def test_unrecognized_idle_text_gets_one_reply_with_the_home_actions():
    update = _update("what did i eat")
    context = _context()

    await home.home_text_router(update, context)

    reply = update.effective_message.reply_text
    reply.assert_awaited_once()  # one message, not a multi-message Home refresh
    assert "didn't recognize" in reply.call_args.args[0]
    assert isinstance(reply.call_args.kwargs.get("reply_markup"), InlineKeyboardMarkup)


async def test_unrecognized_idle_text_is_answered_with_phase1_on(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update("what did i eat")
    db = _snapshot_db()

    await home.home_text_router(update, _context(db))

    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert "didn't recognize" in reply.call_args.args[0]
    # A typo must not cost a snapshot query or a two-message Home.
    db.get_today_meal_count.assert_not_awaited()


async def test_a_guided_diet_prompt_teaches_cancel(monkeypatch):
    """/cancel is the Release 1 recovery control, so a prompt must name it."""
    _enable_phase1(monkeypatch)
    db = _picker_db(suggestions_on=False)
    update = _update()
    context = _context(db, {"diet_entry_mode": diet.DietEntryMode.QUICK})

    await diet._prompt_food_choice(update, context, update.effective_message, "lunch")

    labels = _labels(update.effective_message.reply_text.call_args.kwargs["reply_markup"])
    assert "✖️ Cancel" in labels


@pytest.mark.parametrize(
    "flow,data",
    [("study", "menu_study"), ("gym", "menu_gym"), ("diet", "menu_diet")],
)
async def test_a_section_tap_during_its_own_flow_escapes_to_home(db, flow, data):
    """The button is current, so tapping it must do something.

    An active conversation offers only its state handlers, so a Home section tap
    falls through to ``menu_callback`` while that section's flow is live. It used
    to answer "finish this flow or /cancel first", which made a live button look
    broken. It now ends the flow and re-renders Home, so the buttons work again.

    The section itself is *not* re-entered: only a ConversationHandler entry point
    can do that, and this handler is not one.
    """
    await db.ensure_user(UID, "t", "Test")
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=UID, username="t", first_name="Test"),
        effective_message=query.message,
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )
    context = _context(db)
    activate_conversation(update, context, flow)

    await start.menu_callback(update, context)

    query.answer.assert_awaited()
    said = " ".join(
        str(call.args[0])
        for call in query.message.reply_text.call_args_list
        if call.args
    )
    assert "here's today" in said
    assert "Finish this flow" not in said
    assert active_conversation_flow(context) is None


@pytest.mark.parametrize(
    "flow,data",
    [("study", "menu_study"), ("gym", "menu_gym"), ("diet", "menu_diet")],
)
async def test_real_dispatcher_routes_same_section_taps_out_of_the_flow(
    real_dispatch_app, monkeypatch, flow, data
):
    """The dispatcher really reaches ``menu_callback`` for these taps."""
    app = real_dispatch_app
    owner, _state = _seed_real_flow(app, flow)
    answer_callback = AsyncMock()
    edit_markup = AsyncMock()
    send_message = AsyncMock()
    monkeypatch.setattr(ExtBot, "answer_callback_query", answer_callback)
    monkeypatch.setattr(ExtBot, "edit_message_reply_markup", edit_markup)
    monkeypatch.setattr(ExtBot, "send_message", send_message)

    await app.process_update(_real_callback_update(app, data))

    assert answer_callback.await_count == 1
    said = " ".join(
        str(call.kwargs.get("text", "")) for call in send_message.call_args_list
    )
    assert "here's today" in said
    edit_markup.assert_not_awaited()
    assert app._user_data[UID].get("_ledger_active_conversation") is None


def test_menu_recent_is_a_served_action_not_an_expired_button():
    """The new Home button must be routed, or it would answer "no longer valid"."""
    app = main_module.build_application()
    chat = Chat(id=UID, type="private")
    user = User(id=UID, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user
    )
    message.set_bot(_BOT)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data="menu_recent", message=message
    )
    query.set_bot(_BOT)
    update = Update(update_id=1, callback_query=query)

    handler = next(
        (
            h
            for group in sorted(app.handlers)
            for h in app.handlers[group]
            if h.check_update(update)
        ),
        None,
    )
    assert getattr(handler.callback, "__name__", None) == "menu_callback"


async def test_home_command_routes_through_the_real_dispatcher():
    app = main_module.build_application()
    chat = Chat(id=UID, type="private")
    user = User(id=UID, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user,
        text="/home",
        entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=5)],
    )
    message.set_bot(_BOT)
    update = Update(update_id=1, message=message)

    handler = next(
        (
            h
            for group in sorted(app.handlers)
            for h in app.handlers[group]
            if h.check_update(update)
        ),
        None,
    )
    assert getattr(handler.callback, "__name__", None) == "home_command"
