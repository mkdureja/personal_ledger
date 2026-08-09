"""A shared catalog food can carry a usual amount, a pin, and a hide (v16).

Rice and roti are one catalog row each, shared by both users. Until v16 the
preference table's CHECK accepted only ``food`` and ``recipe``, so nobody could
store "my usual is one bowl" against a shared row — and a source with no usual
amount can never render as a ⚡ one-tap row. The only route to a one-tap staple
was a private copy of something the catalog already had, which is the app
routing people into duplicating its own shared data.

What must stay true after the widening: the catalog row is shared and unowned,
the preference is per user, and one person's usual is invisible to the other.
"""

from __future__ import annotations

import pathlib

import pytest
import pytest_asyncio

from bot import keyboards, migrations
from bot.database import DatabaseManager
from bot.keyboards import INSTANT_PREFIX, choice_button_label
from bot.meal_models import PREFERENCE_SOURCE_TYPES, DefaultQuantity
from bot.suggestions import annotate_defaults

MANOJ = 1554408692
RATIKA = 8908288417


@pytest_asyncio.fixture
async def catalog_db():
    """The real bundled catalog, so this tests the rows the users actually see."""
    from bot.catalog_seed import CATALOG_FOODS

    manager = DatabaseManager(":memory:")
    await manager.connect()
    await manager.init_db()
    await manager.ensure_user(MANOJ, "manoj", "Manoj")
    await manager.ensure_user(RATIKA, "ratika", "Ratika")
    await manager.seed_catalog(CATALOG_FOODS)
    rows = await manager.search_catalog("rice", limit=5)
    assert rows, "the bundled catalog is expected to contain rice"
    try:
        yield manager, int(rows[0]["id"])
    finally:
        await manager.close()


class TestAUsualAmount:
    async def test_a_catalog_food_can_carry_one(self, catalog_db):
        db, rice = catalog_db
        stored = await db.set_default_quantity(
            MANOJ, "catalog", rice, DefaultQuantity(amount=150, unit="g")
        )
        assert stored.unit == "g"
        pref = await db.get_food_preference(MANOJ, "catalog", rice)
        assert pref["default_amount"] == 150
        assert pref["default_unit"] == "g"

    async def test_the_usual_is_validated_against_the_real_row(self, catalog_db):
        """A stored default is always something that resolved at least once."""
        db, rice = catalog_db
        with pytest.raises(Exception):
            await db.set_default_quantity(
                MANOJ, "catalog", rice, DefaultQuantity(amount=1, unit="scoop")
            )
        assert await db.get_food_preference(MANOJ, "catalog", rice) is None

    async def test_two_users_keep_different_usuals_on_one_shared_row(
        self, catalog_db
    ):
        """The whole point: one row, no duplication, separate preferences."""
        db, rice = catalog_db
        await db.set_default_quantity(
            MANOJ, "catalog", rice, DefaultQuantity(amount=150, unit="g")
        )
        await db.set_default_quantity(
            RATIKA, "catalog", rice, DefaultQuantity(amount=80, unit="g")
        )

        mine = await db.get_food_preference(MANOJ, "catalog", rice)
        hers = await db.get_food_preference(RATIKA, "catalog", rice)
        assert (mine["default_amount"], mine["default_unit"]) == (150, "g")
        assert (hers["default_amount"], hers["default_unit"]) == (80, "g")
        # And still exactly one catalog row underneath both.
        assert len([r for r in await db.search_catalog("rice", limit=5)
                    if r["id"] == rice]) == 1

    async def test_clearing_one_users_usual_leaves_the_other(self, catalog_db):
        db, rice = catalog_db
        await db.set_default_quantity(
            MANOJ, "catalog", rice, DefaultQuantity(amount=150, unit="g")
        )
        await db.set_default_quantity(
            RATIKA, "catalog", rice, DefaultQuantity(amount=80, unit="g")
        )
        await db.clear_default_quantity(MANOJ, "catalog", rice)

        assert (await db.get_food_preference(MANOJ, "catalog", rice) or {}).get(
            "default_amount"
        ) is None
        hers = await db.get_food_preference(RATIKA, "catalog", rice)
        assert hers["default_amount"] == 80


class TestPinAndHide:
    async def test_a_catalog_food_can_be_pinned(self, catalog_db):
        db, rice = catalog_db
        await db.set_food_preference(MANOJ, "catalog", rice, is_pinned=True)
        pref = await db.get_food_preference(MANOJ, "catalog", rice)
        assert pref["is_pinned"] == 1

    async def test_pinning_is_per_user(self, catalog_db):
        db, rice = catalog_db
        await db.set_food_preference(MANOJ, "catalog", rice, is_pinned=True)
        assert await db.get_food_preference(RATIKA, "catalog", rice) is None

    async def test_an_inactive_catalog_row_refuses_a_pin(self, catalog_db):
        """No owner to check, but the row must still exist and be active."""
        db, rice = catalog_db
        await db.conn.execute(
            "UPDATE catalog_foods SET is_active = 0 WHERE id = ?", (rice,)
        )
        await db.conn.commit()
        with pytest.raises(ValueError, match="catalog"):
            await db.set_food_preference(MANOJ, "catalog", rice, is_pinned=True)

    async def test_a_missing_catalog_row_refuses_a_pin(self, catalog_db):
        db, _rice = catalog_db
        with pytest.raises(ValueError, match="catalog"):
            await db.set_food_preference(MANOJ, "catalog", 999999, is_pinned=True)


class TestItActuallyRendersAsInstant:
    """Storing the usual is only half of it — the row has to show as ⚡."""

    def test_a_catalog_choice_with_a_usual_is_annotated(self):
        choices = [{"source_type": "catalog", "id": 7, "name": "White rice"}]
        prefs = {("catalog", 7): {"default_amount": 1.0, "default_unit": "bowl"}}

        annotated = annotate_defaults(choices, prefs)

        assert annotated[0]["default"] == {"amount": 1.0, "unit": "bowl"}
        assert annotated[0]["needs_repair"] is False

    def test_the_label_carries_the_instant_marker(self):
        choices = [{"source_type": "catalog", "id": 7, "name": "White rice"}]
        prefs = {("catalog", 7): {"default_amount": 1.0, "default_unit": "bowl"}}

        annotated = annotate_defaults(choices, prefs)
        label = choice_button_label(annotated[0], quick=True)

        assert label.startswith(INSTANT_PREFIX)
        assert "bowl" in label

    def test_a_half_stored_usual_still_reads_as_needing_repair(self):
        choices = [{"source_type": "catalog", "id": 7, "name": "White rice"}]
        prefs = {("catalog", 7): {"default_amount": 1.0, "default_unit": None}}

        annotated = annotate_defaults(choices, prefs)

        assert annotated[0]["default"] is None
        assert annotated[0]["needs_repair"] is True

    def test_a_catalog_row_without_a_usual_is_not_instant(self):
        choices = [{"source_type": "catalog", "id": 7, "name": "White rice"}]
        annotated = annotate_defaults(choices, {})
        assert annotated[0]["default"] is None
        assert not choice_button_label(annotated[0], quick=True).startswith(
            INSTANT_PREFIX
        )


class TestTheOfferIsActuallyReachable:
    """Storage plus rendering is still not a feature if no screen offers it.

    v16 shipped with the confirm screen still gated on food/recipe, so a catalog
    food could hold a usual amount that nothing would ever ask the user for.
    Six days of the table staying empty was the only symptom.
    """

    @pytest.mark.parametrize("kind", ["food", "recipe", "catalog"])
    async def test_the_confirm_screen_offers_it(self, kind):
        """Drives the real handler, which is where the miss actually was.

        Asserting on ``quick_confirm_keyboard(can_set_default=True)`` would have
        passed throughout the bug: the keyboard was always willing, and it was
        the caller that never said yes for a catalog food.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from bot.handlers import diet

        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            message_id=1,
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=MANOJ, username="m", first_name="M"),
            effective_message=message,
            effective_chat=SimpleNamespace(id=MANOJ, type="private"),
            message=message,
        )
        context = SimpleNamespace(bot_data={}, user_data={"diet_meal_type": "snack"})
        context.user_data["diet_sel_kind"] = kind
        context.user_data["diet_sel_id"] = 7
        entry = SimpleNamespace(
            as_item=lambda: {
                "display_name": "White rice (cooked)",
                "calories": 169,
                "entered_amount": 130.0,
                "entered_unit": "g",
            }
        )

        await diet._show_quick_confirm(update, context, message, entry)

        markup = next(
            call.kwargs["reply_markup"]
            for call in reversed(message.reply_text.call_args_list)
            if call.kwargs.get("reply_markup") is not None
        )
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert any("usual" in label.casefold() for label in labels), labels

    async def test_it_is_not_offered_without_a_resolved_amount(self):
        """No amount means there is nothing to store as the usual."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from bot.handlers import diet

        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            message_id=1,
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=MANOJ, username="m", first_name="M"),
            effective_message=message,
            effective_chat=SimpleNamespace(id=MANOJ, type="private"),
            message=message,
        )
        context = SimpleNamespace(
            bot_data={},
            user_data={
                "diet_meal_type": "snack",
                "diet_sel_kind": "catalog",
                "diet_sel_id": 7,
            },
        )
        entry = SimpleNamespace(
            as_item=lambda: {
                "display_name": "White rice (cooked)",
                "calories": 169,
                "entered_amount": None,
                "entered_unit": None,
            }
        )

        await diet._show_quick_confirm(update, context, message, entry)

        markup = next(
            call.kwargs["reply_markup"]
            for call in reversed(message.reply_text.call_args_list)
            if call.kwargs.get("reply_markup") is not None
        )
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert not any("usual" in label.casefold() for label in labels), labels

    def test_every_preference_source_type_can_set_a_default(self):
        """The gate is one shared constant now; this pins what it must contain."""
        assert set(PREFERENCE_SOURCE_TYPES) == {"food", "recipe", "catalog"}

    def test_freetext_is_deliberately_excluded(self):
        """A typed blob has no source row to hold a preference against."""
        assert "freetext" not in PREFERENCE_SOURCE_TYPES

    def test_no_gate_still_hardcodes_the_old_pair(self):
        """A structural check, because this was missed twice by reading.

        Any surviving literal ('food', 'recipe') pair in the preference paths is
        a gate that a later widening will silently skip again.
        """
        import pathlib

        root = pathlib.Path(food_admin_root())
        offenders = []
        for path in [
            root / "bot" / "keyboards.py",
            root / "bot" / "suggestions.py",
            root / "bot" / "handlers" / "diet.py",
        ]:
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if '"food", "recipe")' in line and "PREFERENCE_SOURCE_TYPES" not in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, "hardcoded gate(s):\n" + "\n".join(offenders)


def food_admin_root():
    import bot

    return pathlib.Path(bot.__file__).resolve().parent.parent


class TestMigration:
    async def _v15_db(self, monkeypatch, rows):
        manager = DatabaseManager(":memory:")
        await manager.connect()
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 15)
            await migrations.run_migrations(manager.conn)
        assert await migrations.get_user_version(manager.conn) == 15
        for user_id, source_type, source_id, amount, unit in rows:
            await manager.conn.execute(
                "INSERT INTO users (user_id) VALUES (?)", (user_id,)
            )
            await manager.conn.execute(
                "INSERT INTO user_food_preferences "
                "(user_id, source_type, source_id, default_amount, default_unit) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, source_type, source_id, amount, unit),
            )
        await manager.conn.commit()
        return manager

    async def test_a_catalog_preference_is_refused_before_the_migration(
        self, monkeypatch
    ):
        """Proves the CHECK was the actual blocker, not a code-level guard."""
        import sqlite3

        manager = await self._v15_db(monkeypatch, [])
        try:
            await manager.conn.execute("INSERT INTO users (user_id) VALUES (1)")
            with pytest.raises(sqlite3.IntegrityError):
                await manager.conn.execute(
                    "INSERT INTO user_food_preferences "
                    "(user_id, source_type, source_id) VALUES (1, 'catalog', 5)"
                )
        finally:
            await manager.close()

    async def test_existing_preferences_survive_the_rebuild(self, monkeypatch):
        manager = await self._v15_db(
            monkeypatch, [(MANOJ, "food", 3, 40.0, "g")]
        )
        try:
            await migrations.run_migrations(manager.conn)
            row = await manager._query_one(
                "SELECT * FROM user_food_preferences WHERE user_id = ?", (MANOJ,)
            )
            assert row["source_type"] == "food"
            assert row["default_amount"] == 40.0
            assert row["default_unit"] == "g"
        finally:
            await manager.close()

    async def test_catalog_is_accepted_afterwards(self, monkeypatch):
        manager = await self._v15_db(monkeypatch, [])
        try:
            await migrations.run_migrations(manager.conn)
            await manager.conn.execute("INSERT INTO users (user_id) VALUES (1)")
            await manager.conn.execute(
                "INSERT INTO user_food_preferences "
                "(user_id, source_type, source_id) VALUES (1, 'catalog', 5)"
            )
        finally:
            await manager.close()

    async def test_a_bogus_source_type_is_still_refused(self, monkeypatch):
        """Widened, not opened: 'freetext' has no preference to store."""
        import sqlite3

        manager = await self._v15_db(monkeypatch, [])
        try:
            await migrations.run_migrations(manager.conn)
            await manager.conn.execute("INSERT INTO users (user_id) VALUES (1)")
            with pytest.raises(sqlite3.IntegrityError):
                await manager.conn.execute(
                    "INSERT INTO user_food_preferences "
                    "(user_id, source_type, source_id) VALUES (1, 'freetext', 5)"
                )
        finally:
            await manager.close()

    async def test_running_it_twice_is_a_no_op(self, monkeypatch):
        manager = await self._v15_db(
            monkeypatch, [(MANOJ, "recipe", 2, 1.0, "serving")]
        )
        try:
            await migrations.run_migrations(manager.conn)
            await migrations._migration_0016_catalog_preferences(manager.conn)
            rows = await manager._query_all(
                "SELECT * FROM user_food_preferences WHERE user_id = ?", (MANOJ,)
            )
            assert len(rows) == 1
        finally:
            await manager.close()
