"""Regression tests for the defects raised in the second Codex project review.

One test per confirmed finding, each written to fail against the code as it was
before the fix. Grouped by the review's own numbering so a future reader can map
a test back to the finding it protects — see ``docs/codex_review_response.md``.

These are deliberately behavioural: they assert what a user or an operator would
observe, not how the fix is implemented, so a later refactor is free to change
the mechanism without silently retiring the guarantee.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, time

import pytest

import ledger_schema
from bot import migrations
from bot.meal_text import MAX_SEGMENTS, MAX_SEGMENT_LENGTH, parse_meal
from bot.nutrition_resolution import complete_bare_count
from bot.services.typed_meal import (
    UnresolvedSegment,
    augment_plan_with_parser,
    plan_typed_meal,
)
from bot.services.voice import VoiceTranscriber
from ledger_backup import VerificationFailed, verify_backup_file
from ledger_schema import required_columns_for


# ---------------------------------------------------------------------------
# Finding 3 — startup database safeguards
# ---------------------------------------------------------------------------
class TestSchemaManifestCoversEveryVersion:
    """The column manifest used to stop at v8, so v10 could not be verified."""

    def test_v10_consent_columns_are_required(self):
        columns = required_columns_for(ledger_schema.LATEST_SCHEMA_VERSION)
        assert "user_settings" in columns
        assert {"ai_parsing_enabled", "ai_parsing_consented_at"} <= columns[
            "user_settings"
        ]

    def test_supplements_are_required_from_v9(self):
        assert "supplements" in required_columns_for(9)
        assert "supplements" not in required_columns_for(8)

    def test_a_version_is_never_asked_for_a_later_versions_columns(self):
        """A v9 rollback point predates the consent columns and must still pass."""
        assert "ai_parsing_enabled" not in required_columns_for(9)["user_settings"]

    async def test_startup_verifier_rejects_a_stripped_consent_column(self, db):
        """Dropping a v10 column from a v10 database must fail startup.

        Reproduces the review's probe: ``user_version`` still reads 10 and every
        required table is present, so a table-only check certifies it as sound.
        """
        await db.conn.execute("ALTER TABLE user_settings RENAME TO user_settings_old")
        await db.conn.execute(
            "CREATE TABLE user_settings ("
            "user_id INTEGER PRIMARY KEY, reminders_enabled INTEGER, "
            "routine_profile TEXT)"
        )
        with pytest.raises(migrations.SchemaVerificationError, match="ai_parsing"):
            await migrations.verify_current_schema(db.conn)

    def test_backup_verifier_rejects_the_same_database(self, tmp_path):
        """The standalone tool must refuse what the bot refuses."""
        path = tmp_path / "stripped.db"
        conn = sqlite3.connect(str(path))
        try:
            required = required_columns_for(ledger_schema.LATEST_SCHEMA_VERSION)
            for table in sorted(
                ledger_schema.required_tables_for(ledger_schema.LATEST_SCHEMA_VERSION)
            ):
                columns = set(required.get(table, frozenset()))
                if table == "user_settings":
                    columns -= {"ai_parsing_enabled", "ai_parsing_consented_at"}
                body = ", ".join(f"{c} TEXT" for c in sorted(columns)) or "x TEXT"
                conn.execute(f"CREATE TABLE {table} ({body})")
            conn.execute(f"PRAGMA user_version = {ledger_schema.LATEST_SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(VerificationFailed, match="ai_parsing"):
            verify_backup_file(path)


class TestUnknownDatabaseIsNotTreatedAsNew:
    """A stale DB_PATH pointing at somebody else's SQLite file."""

    async def test_populated_foreign_database_is_refused(self, tmp_path):
        import aiosqlite

        from bot.migration_preflight import (
            MigrationPreflightError,
            prepare_database_for_startup,
        )

        path = tmp_path / "not-ledger.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, total REAL)")
        conn.execute("INSERT INTO invoices (total) VALUES (42.0)")
        conn.commit()
        conn.close()

        async with aiosqlite.connect(str(path)) as connection:
            with pytest.raises(MigrationPreflightError, match="not a Ledger database"):
                await prepare_database_for_startup(
                    connection, db_path=str(path), backup_dest_dir=None
                )

        # The refusal must be total: nothing was created in the foreign file.
        conn = sqlite3.connect(str(path))
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert names == {"invoices"}
        assert "users" not in names

    async def test_genuinely_empty_database_is_still_created(self, tmp_path):
        import aiosqlite

        from bot.migration_preflight import prepare_database_for_startup

        path = tmp_path / "brand-new.db"
        async with aiosqlite.connect(str(path)) as connection:
            outcome = await prepare_database_for_startup(
                connection, db_path=str(path), backup_dest_dir=None
            )
        assert outcome.action == "new-database"


def test_production_entrypoint_refuses_an_in_memory_database(monkeypatch):
    """``DB_PATH=:memory:`` must not start; it would discard every write."""
    from bot import main as main_module

    monkeypatch.setattr(main_module, "DB_PATH", ":memory:")
    called = False

    def _should_not_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(main_module, "build_application", _should_not_run)

    with pytest.raises(SystemExit):
        main_module.main()
    assert not called, "build_application must not be reached"


# ---------------------------------------------------------------------------
# Finding 5 — typed-meal correctness
# ---------------------------------------------------------------------------
class _FakeDB:
    """Just enough database for the planner: one private per-100g food."""

    def __init__(self, foods=None):
        self.foods = foods if foods is not None else []
        self.catalog: list[dict] = []

    async def list_foods(self, user_id):
        return self.foods

    async def list_recipes(self, user_id):
        return []

    async def get_food_portions(self, user_id, food_id):
        return []

    async def get_recipe_ingredients(self, user_id, recipe_id):
        return []

    async def get_catalog_portions(self, catalog_id):
        return []

    async def search_catalog(self, name):
        key = name.strip().casefold()
        return [row for row in self.catalog if key in str(row["name"]).casefold()]


def _parser_returning(*names_and_tokens):
    class _Parser:
        async def parse(self, text):
            items = []
            for food, tokens in names_and_tokens:
                items.append(
                    type(
                        "Item",
                        (),
                        {"food": food, "as_tokens": lambda self, t=tokens: t},
                    )()
                )
            return items

    return _Parser()


_OATS = {
    "id": 1, "name": "oats", "base_unit": "g", "basis_amount": 100,
    "calories": 380, "protein_g": 13.0, "carbs_g": 67.0, "fat_g": 7.0,
}


class TestModelAssistNeverDropsAnItem:
    """Finding 5.1 — a partial model answer used to erase the rest."""

    async def test_unanswered_segments_survive(self):
        db = _FakeDB([_OATS])
        parsed = parse_meal("mystery pie, unicorn steak")
        plan = await plan_typed_meal(db, 1, list(parsed.segments))
        assert len(plan.unresolved) == 2

        # The model answers about something else entirely and says nothing about
        # either item the user typed.
        after = await augment_plan_with_parser(
            db, 1, plan, _parser_returning(("oats", ("50", "g")))
        )

        surviving = {item.name for item in after.unresolved}
        assert "mystery pie" in surviving
        assert "unicorn steak" in surviving

    async def test_an_answered_segment_is_not_reported_twice(self):
        db = _FakeDB([_OATS])
        parsed = parse_meal("a bowl of oats this morning, unicorn steak")
        plan = await plan_typed_meal(db, 1, list(parsed.segments))

        after = await augment_plan_with_parser(
            db, 1, plan, _parser_returning(("oats", ("50", "g")))
        )

        assert [entry.display_text for entry in after.resolved] == ["50 g oats"]
        assert [item.name for item in after.unresolved] == ["unicorn steak"]
        assert after.model_assisted is True

    async def test_every_typed_item_appears_somewhere(self):
        """The invariant behind this finding, stated directly."""
        db = _FakeDB([_OATS])
        parsed = parse_meal("mystery pie, unicorn steak, 100g oats")
        plan = await plan_typed_meal(db, 1, list(parsed.segments))
        after = await augment_plan_with_parser(
            db, 1, plan, _parser_returning(("oats", ("50", "g")))
        )
        assert len(after.resolved) + len(after.unresolved) >= len(parsed.segments)


class TestTruncationIsAnnounced:
    """Finding 5.2 — the caps used to discard input in silence."""

    def test_items_past_the_cap_are_counted_and_reported(self):
        parsed = parse_meal(", ".join(f"item{i} 1g" for i in range(1, 26)))
        assert len(parsed.segments) == MAX_SEGMENTS
        assert parsed.dropped_items == 5
        assert parsed.truncated
        assert "5 more" in parsed.notice

    def test_an_over_long_item_is_reported(self):
        parsed = parse_meal("100g " + "a" * (MAX_SEGMENT_LENGTH + 50))
        assert parsed.shortened_items == 1
        assert str(MAX_SEGMENT_LENGTH) in parsed.notice

    def test_ordinary_input_carries_no_notice(self):
        assert parse_meal("100g oats, 2 eggs").notice is None

    def test_the_confirm_screen_shows_the_notice(self):
        from bot.handlers.describe import render_plan
        from bot.services.typed_meal import TypedMealPlan

        text = render_plan(TypedMealPlan(), notice="Only the first 20 items were read.")
        assert "Only the first 20 items were read." in text


class TestBareCountResolves:
    """Finding 5.4 — the advertised ``2 eggs`` example could not resolve."""

    def test_a_countable_definition_supplies_its_own_unit(self):
        assert complete_bare_count(("2",), "piece") == ("2", "piece")
        assert complete_bare_count(("2",), "serving") == ("2", "serving")

    def test_a_weighed_definition_still_refuses(self):
        """"2" of a per-100g food is a half-typed amount, not two grams."""
        assert complete_bare_count(("2",), "g") == ("2",)
        assert complete_bare_count(("2",), "ml") == ("2",)

    def test_an_explicit_quantity_is_never_rewritten(self):
        assert complete_bare_count(("100", "g"), "piece") == ("100", "g")

    async def test_the_advertised_example_resolves(self, db, user_id):
        """``/describe 2 eggs`` against the shared catalog, end to end."""
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)

        parsed = parse_meal("2 eggs")
        plan = await plan_typed_meal(db, user_id, list(parsed.segments))

        assert not plan.unresolved, [u.explanation for u in plan.unresolved]
        assert len(plan.resolved) == 1
        assert plan.resolved[0].calories == 156  # 2 x the catalog's 78 per piece

    async def test_a_weighed_catalog_food_still_needs_a_unit(self, db, user_id):
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)

        plan = await plan_typed_meal(db, user_id, list(parse_meal("2 oats").segments))
        assert not plan.resolved
        assert plan.unresolved[0].reason == "bad_quantity"


class TestPluralNamesFindSingularEntries:
    """The other half of finding 5.4: the catalog stores "Egg", people type "eggs"."""

    async def test_plural_resolves_to_the_catalog_entry(self, db, user_id):
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)

        plan = await plan_typed_meal(db, user_id, list(parse_meal("2 eggs").segments))
        assert plan.resolved[0].display_text.endswith("Egg")

    async def test_an_unknown_plural_still_reports_what_was_typed(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        plan = await plan_typed_meal(
            db, user_id, list(parse_meal("3 unicorns").segments)
        )
        assert plan.unresolved[0].name == "unicorns"
        assert plan.unresolved[0].reason == "unknown"


# ---------------------------------------------------------------------------
# Finding 6 — voice work must not block both users
# ---------------------------------------------------------------------------
class _Blocking(VoiceTranscriber):
    """A transcriber whose worker thread blocks until the test releases it.

    An Event rather than ``sleep``: the point is to model a hang, and a sleeping
    thread would outlive the assertion and hold the suite open at teardown for
    as long as it was told to sleep.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = threading.Event()

    def _block(self):
        # Bounded so a mistake in a test cannot wedge the whole run.
        assert self.release.wait(timeout=30), "test never released the worker"


class _HangingLoad(_Blocking):
    def _load_model(self):
        self._block()
        return object()


class _HangingDecode(_Blocking):
    def _load_model(self):
        return object()

    def _transcribe_sync(self, path):
        self._block()
        return "never"


@contextmanager
def _blocking(transcriber):
    """Release the blocked worker inside the test, not at fixture teardown.

    The event loop drains its thread-pool executor while tearing down, which
    happens *before* a fixture finalizer gets to release anything — so a
    fixture-based release left every one of these tests waiting out the full
    block at teardown.
    """
    try:
        yield transcriber
    finally:
        transcriber.release.set()


class TestVoiceIsBounded:
    async def test_a_stalled_model_load_gives_up(self):
        with _blocking(
            _HangingLoad("base", load_timeout=0.2, transcribe_timeout=5)
        ) as transcriber:
            result = await transcriber.transcribe("note.ogg")
        assert result.reason == "timed_out"
        assert not result.ok

    async def test_a_stalled_load_is_latched_so_the_next_note_fails_fast(self):
        with _blocking(
            _HangingLoad("base", load_timeout=0.2, transcribe_timeout=5)
        ) as transcriber:
            await transcriber.transcribe("note.ogg")

            loop = asyncio.get_running_loop()
            started = loop.time()
            result = await transcriber.transcribe("note.ogg")
        assert result.reason == "model_unavailable"
        assert loop.time() - started < 0.2, "must not queue behind the stalled load"

    async def test_a_wedged_decoder_gives_up(self):
        with _blocking(
            _HangingDecode("base", load_timeout=5, transcribe_timeout=0.2)
        ) as transcriber:
            result = await transcriber.transcribe("note.ogg")
        assert result.reason == "timed_out"

    async def test_the_event_loop_keeps_running_during_a_wedged_decode(self):
        """The property that matters: updates are processed sequentially."""
        transcriber = _HangingDecode("base", load_timeout=5, transcribe_timeout=0.5)
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            await transcriber.transcribe("note.ogg")
        finally:
            beat.cancel()
            transcriber.release.set()
        assert ticks >= 3, "the bot stopped answering while transcribing"

    def test_every_failure_reason_has_a_sentence(self):
        from bot.services.voice import REASON_TEXT, Transcript

        for reason in ("timed_out", "too_large", "failed", "empty"):
            assert REASON_TEXT[reason]
            assert Transcript(reason=reason).message == REASON_TEXT[reason]


class TestPreloadSharesOneModel:
    """A preload that built its own instance would buy the user nothing."""

    async def test_startup_and_handlers_use_the_same_transcriber(self):
        from types import SimpleNamespace

        from bot.handlers.voice import (
            get_transcriber,
            preload_transcriber,
            transcriber_for,
        )

        bot_data: dict = {}
        loaded: list[str] = []

        class _Recording(VoiceTranscriber):
            def _load_model(self):
                loaded.append(self._model_size)
                return object()

        bot_data["voice_transcriber"] = _Recording("base")
        await preload_transcriber(bot_data)

        context = SimpleNamespace(bot_data=bot_data)
        assert get_transcriber(context) is transcriber_for(bot_data)
        assert get_transcriber(context).loaded, "the handler saw an unloaded model"
        assert loaded == ["base"], "the model must be loaded exactly once"

    async def test_a_preload_failure_does_not_stop_startup(self):
        from bot import main as main_module

        class _Failing:
            async def ensure_loaded(self):
                raise RuntimeError("no disk")

        application = SimpleNamespaceApplication({"voice_transcriber": _Failing()})
        from bot import config

        original = config.VOICE_ENABLED
        config.VOICE_ENABLED = True
        try:
            await main_module._preload_voice_model(application)  # must not raise
        finally:
            config.VOICE_ENABLED = original


class SimpleNamespaceApplication:
    def __init__(self, bot_data):
        self.bot_data = bot_data


async def test_an_oversized_voice_note_is_refused_before_download(monkeypatch):
    """Duration is declared metadata; size is what actually reaches the decoder."""
    from types import SimpleNamespace

    from bot import config
    from bot.handlers import voice as voice_handler

    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "VOICE_MAX_FILE_BYTES", 1024)

    replies = []

    async def _reply(text, **kwargs):
        replies.append(text)
        return SimpleNamespace(delete=lambda: None)

    downloaded = False

    async def _get_file(file_id):
        nonlocal downloaded
        downloaded = True
        raise AssertionError("must not download an oversized note")

    message = SimpleNamespace(
        voice=SimpleNamespace(duration=5, file_size=99999, file_id="x"),
        reply_text=_reply,
        reply_html=_reply,
    )
    update = SimpleNamespace(
        effective_message=message, effective_user=SimpleNamespace(id=1)
    )
    context = SimpleNamespace(bot=SimpleNamespace(get_file=_get_file), bot_data={})

    await voice_handler.handle_voice_meal(update, context)
    assert not downloaded
    assert any("too large" in text.lower() or "limit is" in text for text in replies)


# ---------------------------------------------------------------------------
# Finding 7 — a missed reminder slot is replayed after a restart
# ---------------------------------------------------------------------------
def _pin_hour(monkeypatch, hour: int) -> None:
    """Pin startup's view of the clock. Reverted automatically after the test."""
    from bot import main as main_module

    pinned = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(main_module, "_local_now", lambda: pinned)


class TestReminderCatchUp:
    async def test_a_missed_slot_is_replayed(self, db, monkeypatch):
        from bot import main as main_module

        application = _FakeApplication(db)
        _pin_hour(monkeypatch, 23)

        await main_module._catch_up_missed_jobs(
            application,
            [(_noop, "daily_habit_reminder", time(hour=20), None)],
        )
        assert [job["name"] for job in application.job_queue.scheduled] == [
            "daily_habit_reminder_catchup"
        ]

    async def test_a_future_slot_is_left_alone(self, db, monkeypatch):
        from bot import main as main_module

        application = _FakeApplication(db)
        _pin_hour(monkeypatch, 6)

        await main_module._catch_up_missed_jobs(
            application,
            [(_noop, "daily_habit_reminder", time(hour=20), None)],
        )
        assert application.job_queue.scheduled == []

    async def test_a_slot_that_already_ran_is_not_replayed(
        self, db, user_id, monkeypatch
    ):
        """A normal restart after a completed run must schedule nothing."""
        from bot import main as main_module
        from bot.config import today_local

        await db.ensure_user(user_id, None, None)
        await db.record_chunk_delivery(
            user_id,
            "daily_habit_reminder",
            today_local().isoformat(),
            0,
            delivered=True,
            error_category=None,
        )

        application = _FakeApplication(db)
        _pin_hour(monkeypatch, 23)

        await main_module._catch_up_missed_jobs(
            application,
            [(_noop, "daily_habit_reminder", time(hour=20), None)],
        )
        assert application.job_queue.scheduled == []


async def _noop(context):  # pragma: no cover - never executed
    return None


class _FakeJobQueue:
    def __init__(self):
        self.scheduled = []

    def run_once(self, callback, *, when, name, data=None):
        self.scheduled.append({"name": name, "when": when, "data": data})


class _FakeApplication:
    def __init__(self, db):
        self.bot_data = {"db": db}
        self.job_queue = _FakeJobQueue()


# ---------------------------------------------------------------------------
# Finding 8 — the test suite must not read a deployment .env
# ---------------------------------------------------------------------------
def test_the_suite_declares_its_own_configuration():
    """``LEDGER_SKIP_DOTENV`` closes the leak for every setting, not one by one."""
    import os

    assert os.environ.get("LEDGER_SKIP_DOTENV") == "1"


def test_unresolved_segment_identity_supports_carrying_items_through():
    """The assist fix compares segments; they must be hashable and comparable."""
    first = UnresolvedSegment(raw="a", name="a", reason="unknown")
    second = UnresolvedSegment(raw="a", name="a", reason="unknown")
    assert first == second
    assert len({first, second}) == 1
