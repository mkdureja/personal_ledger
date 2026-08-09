"""AI parsing is on by default, without anyone's consent being invented.

The owner asked for it on by default for both users. The interesting part is not
the default — it is the distinction the default rests on: a user who has *not*
chosen is not the same as a user who chose "off", and only the first is answered
by a deployment setting. Schema v15 exists to keep those two apart.
"""

from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio

from bot import migrations
from bot.database import DatabaseManager
from ledger_schema import LATEST_SCHEMA_VERSION

MANOJ = 1554408692
RATIKA = 8908288417


@pytest_asyncio.fixture
async def two_user_db():
    manager = DatabaseManager(":memory:")
    await manager.connect()
    await manager.init_db()
    await manager.ensure_user(MANOJ, "manoj", "Manoj")
    await manager.ensure_user(RATIKA, "ratika", "Ratika")
    yield manager
    await manager.close()


class TestDefault:
    async def test_a_user_who_never_chose_gets_the_default(self, two_user_db, monkeypatch):
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", True)
        assert await two_user_db.get_ai_parsing_enabled(MANOJ) is True

    async def test_both_users_get_it_without_a_row_being_rewritten(
        self, two_user_db, monkeypatch
    ):
        """The point of the change: on for both, with no consent asserted."""
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", True)
        for user_id in (MANOJ, RATIKA):
            assert await two_user_db.get_ai_parsing_enabled(user_id) is True
            assert await two_user_db.has_chosen_ai_parsing(user_id) is False

    async def test_no_consent_timestamp_is_invented(self, two_user_db, monkeypatch):
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", True)
        await two_user_db.get_ai_parsing_enabled(RATIKA)
        row = await two_user_db._query_one(
            "SELECT ai_parsing_consented_at FROM user_settings WHERE user_id = ?",
            (RATIKA,),
        )
        assert row is None or row["ai_parsing_consented_at"] is None

    async def test_the_default_can_be_turned_off_for_the_deployment(
        self, two_user_db, monkeypatch
    ):
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", False)
        assert await two_user_db.get_ai_parsing_enabled(MANOJ) is False

    async def test_a_missing_settings_row_is_not_chosen_either(
        self, two_user_db, monkeypatch
    ):
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", True)
        assert await two_user_db.get_ai_parsing_enabled(999999) is True
        assert await two_user_db.has_chosen_ai_parsing(999999) is False


class TestAChoiceBeatsTheDefault:
    async def test_an_explicit_off_survives_a_default_of_on(
        self, two_user_db, monkeypatch
    ):
        """The whole reason for the tri-state: a decision is not overridden."""
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", True)
        await two_user_db.set_ai_parsing_enabled(RATIKA, False)
        assert await two_user_db.get_ai_parsing_enabled(RATIKA) is False
        assert await two_user_db.has_chosen_ai_parsing(RATIKA) is True

    async def test_an_explicit_on_survives_a_default_of_off(
        self, two_user_db, monkeypatch
    ):
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", False)
        await two_user_db.set_ai_parsing_enabled(MANOJ, True)
        assert await two_user_db.get_ai_parsing_enabled(MANOJ) is True

    async def test_opting_in_still_records_when(self, two_user_db):
        await two_user_db.set_ai_parsing_enabled(MANOJ, True)
        row = await two_user_db._query_one(
            "SELECT ai_parsing_consented_at FROM user_settings WHERE user_id = ?",
            (MANOJ,),
        )
        assert row["ai_parsing_consented_at"] is not None

    async def test_opting_out_clears_the_consent_record(self, two_user_db):
        """A revoked consent must leave no 'they agreed once' evidence."""
        await two_user_db.set_ai_parsing_enabled(MANOJ, True)
        await two_user_db.set_ai_parsing_enabled(MANOJ, False)
        row = await two_user_db._query_one(
            "SELECT ai_parsing_enabled, ai_parsing_consented_at "
            "FROM user_settings WHERE user_id = ?",
            (MANOJ,),
        )
        assert row["ai_parsing_consented_at"] is None
        # ...but the 0 is still a decision, not an absence.
        assert row["ai_parsing_enabled"] == 0

    async def test_one_users_choice_does_not_move_the_other(self, two_user_db, monkeypatch):
        monkeypatch.setattr("bot.config.AI_PARSING_DEFAULT_ON", True)
        await two_user_db.set_ai_parsing_enabled(RATIKA, False)
        assert await two_user_db.get_ai_parsing_enabled(MANOJ) is True


class TestMigration:
    """The v14 to v15 step, rehearsed on a populated database.

    The runner has no target argument: the version is pinned by monkeypatching
    ``migrations.LATEST_VERSION`` inside a context, which is what guarantees the
    global is restored even when an assertion fails. A leaked value would
    silently mis-target every later test in the session.
    """

    async def _v14_db(self, monkeypatch, rows):
        manager = DatabaseManager(":memory:")
        await manager.connect()
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 14)
            await migrations.run_migrations(manager.conn)
        assert await migrations.get_user_version(manager.conn) == 14
        for user_id, enabled, consented in rows:
            await manager.conn.execute(
                "INSERT INTO users (user_id) VALUES (?)", (user_id,)
            )
            await manager.conn.execute(
                "INSERT INTO user_settings "
                "(user_id, ai_parsing_enabled, ai_parsing_consented_at) "
                "VALUES (?, ?, ?)",
                (user_id, enabled, consented),
            )
        await manager.conn.commit()
        return manager

    async def test_a_never_chosen_row_becomes_unset(self, monkeypatch):
        """The live state of both users: 0 with no timestamp = never asked."""
        manager = await self._v14_db(monkeypatch, [(MANOJ, 0, None)])
        try:
            await migrations.run_migrations(manager.conn)
            row = await manager._query_one(
                "SELECT ai_parsing_enabled FROM user_settings WHERE user_id = ?",
                (MANOJ,),
            )
            assert row["ai_parsing_enabled"] is None
        finally:
            await manager.close()

    async def test_an_explicit_opt_in_is_preserved(self, monkeypatch):
        manager = await self._v14_db(monkeypatch, [(MANOJ, 1, "2026-08-01 10:00:00")])
        try:
            await migrations.run_migrations(manager.conn)
            row = await manager._query_one(
                "SELECT ai_parsing_enabled, ai_parsing_consented_at "
                "FROM user_settings WHERE user_id = ?",
                (MANOJ,),
            )
            assert row["ai_parsing_enabled"] == 1
            assert row["ai_parsing_consented_at"] == "2026-08-01 10:00:00"
        finally:
            await manager.close()

    async def test_other_settings_survive_the_rebuild(self, monkeypatch):
        manager = await self._v14_db(monkeypatch, [(MANOJ, 0, None)])
        try:
            await manager.conn.execute(
                "UPDATE user_settings SET reminders_enabled = 0, "
                "suggestions_enabled = 0, routine_profile = 'evening' "
                "WHERE user_id = ?",
                (MANOJ,),
            )
            await manager.conn.commit()
            await migrations.run_migrations(manager.conn)
            row = await manager._query_one(
                "SELECT * FROM user_settings WHERE user_id = ?", (MANOJ,)
            )
            assert row["reminders_enabled"] == 0
            assert row["suggestions_enabled"] == 0
            assert row["routine_profile"] == "evening"
        finally:
            await manager.close()

    async def test_the_column_is_nullable_afterwards(self, monkeypatch):
        manager = await self._v14_db(monkeypatch, [(MANOJ, 0, None)])
        try:
            await migrations.run_migrations(manager.conn)
            cursor = await manager.conn.execute("PRAGMA table_info(user_settings)")
            columns = {row["name"]: row for row in await cursor.fetchall()}
            assert columns["ai_parsing_enabled"]["notnull"] == 0
        finally:
            await manager.close()

    async def test_a_bad_value_is_still_refused(self, monkeypatch):
        """Nullable is not unchecked: 2 was never valid and still is not."""
        manager = await self._v14_db(monkeypatch, [(MANOJ, 0, None)])
        try:
            await migrations.run_migrations(manager.conn)
            with pytest.raises(sqlite3.IntegrityError):
                await manager.conn.execute(
                    "UPDATE user_settings SET ai_parsing_enabled = 2 "
                    "WHERE user_id = ?",
                    (MANOJ,),
                )
        finally:
            await manager.close()

    async def test_running_it_twice_is_a_no_op(self, monkeypatch):
        manager = await self._v14_db(monkeypatch, [(MANOJ, 0, None)])
        try:
            await migrations.run_migrations(manager.conn)
            await migrations._migration_0015_ai_parsing_tristate(manager.conn)
            row = await manager._query_one(
                "SELECT ai_parsing_enabled FROM user_settings WHERE user_id = ?",
                (MANOJ,),
            )
            assert row["ai_parsing_enabled"] is None
        finally:
            await manager.close()

    async def test_it_lands_on_the_latest_version(self, monkeypatch):
        manager = await self._v14_db(monkeypatch, [(MANOJ, 0, None)])
        try:
            await migrations.run_migrations(manager.conn)
            assert await migrations.get_user_version(manager.conn) == (
                LATEST_SCHEMA_VERSION
            )
        finally:
            await manager.close()
