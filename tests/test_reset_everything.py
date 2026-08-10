"""``--everything``: a genuine clean slate that costs no setup.

The distinction this file exists to pin down is *log* versus *definition*. A log
records something that happened and is what a fresh start throws away. A
definition is work the user did to configure the app — a saved food, a usual
amount, a habit, an entry in the shared catalog — and no combination of flags
may delete one, because then a wipe would silently punish setting the app up
properly.
"""

from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio

from bot.database import DatabaseManager
from bot.meal_models import DefaultQuantity
from scripts import reset_diet_logs

UID = 1554408692
OTHER = 8908288417

#: Every table a fresh start must empty.
LOG_TABLES = (
    "diet_logs",
    "diet_log_items",
    "gym_logs",
    "gym_sets",
    "weight_logs",
    "supplement_logs",
    "habit_logs",
    "study_logs",
    "app_suggestions",
    "reminder_deliveries",
    "mutation_receipts",
)

#: Every table it must leave completely alone.
DEFINITION_TABLES = (
    "users",
    "user_settings",
    "foods",
    "food_portions",
    "recipes",
    "recipe_ingredients",
    "habits",
    "habit_activity_periods",
    "supplements",
    "exercises",
    "meal_shortcuts",
    "user_food_preferences",
    "catalog_foods",
    "catalog_portions",
    "catalog_aliases",
)


@pytest_asyncio.fixture
async def populated(tmp_path):
    """A ledger with one of everything: logs to clear, definitions to keep."""
    from bot.catalog_seed import CATALOG_FOODS
    from bot.config import today_local
    from bot.exercise_seed import seed_rows

    path = tmp_path / "ledger.db"
    db = DatabaseManager(str(path))
    await db.connect()
    try:
        await db.init_db()
        await db.ensure_user(UID, "manoj", "Manoj")
        await db.ensure_user(OTHER, "ratika", "Ratika")
        await db.seed_catalog(CATALOG_FOODS)
        await db.seed_exercises(seed_rows())

        # --- definitions (must survive) ---
        saved = await db.save_food(UID, "Oats", "g", 100, 389, 16.9, 66.3, 6.9)
        food_id = saved["food"]["id"]
        await db.save_food_portion(UID, food_id, "bowl", 40, "g")
        await db.set_default_quantity(UID, "food", food_id, DefaultQuantity(40, "g"))
        await db.set_food_preference(UID, "food", food_id, is_pinned=True)
        habit_id, _ = await db.add_habit(UID, "Read")
        supplement_id, _ = await db.add_supplement(UID, "Creatine")

        # --- logs (must go) ---
        await db.log_diet(UID, "lunch", "dal", 300, 10.0, 40.0, 5.0)
        await db.log_gym_sets(UID, "Bench press", [{"reps": 10, "weight_kg": 50.0}])
        await db.log_gym(UID, "Chest", 1, None)
        await db.log_weight(UID, 71.5, today_local())
        await db.take_supplement(UID, supplement_id, today_local())
        await db.check_habit(UID, habit_id, today_local())
        await db.log_study(UID, "Maths", 30)
        await db.add_app_suggestion(UID, "make it faster")
    finally:
        # Always: an exception above would otherwise leave the aiosqlite worker
        # thread alive, and the whole session hangs at teardown rather than
        # reporting the failure.
        await db.close()
    return path


def _counts(path, tables):
    conn = sqlite3.connect(str(path))
    try:
        out = {}
        for table in tables:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = None
        return out
    finally:
        conn.close()


class TestEverything:
    def test_every_log_table_is_emptied(self, populated):
        before = _counts(populated, LOG_TABLES)
        assert sum(v for v in before.values() if v), "fixture logged nothing"

        assert reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--no-backup"]
        ) == 0

        after = _counts(populated, LOG_TABLES)
        assert {k: v for k, v in after.items() if v} == {}, after

    def test_no_definition_is_touched(self, populated):
        before = _counts(populated, DEFINITION_TABLES)

        reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--no-backup"]
        )

        assert _counts(populated, DEFINITION_TABLES) == before

    def test_the_usual_amount_survives(self, populated):
        """The single most valuable thing a user configures, and the one whose
        loss would make a wipe feel like punishment for setting the app up."""
        reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--no-backup"]
        )
        conn = sqlite3.connect(str(populated))
        try:
            row = conn.execute(
                "SELECT default_amount, default_unit, is_pinned "
                "FROM user_food_preferences"
            ).fetchone()
        finally:
            conn.close()
        assert row == (40.0, "g", 1)

    def test_habit_activity_periods_survive(self, populated):
        """They are adherence's denominator, not a record of anything done.

        Deleting them leaves a surviving habit with no eligible days, which reads
        as "no data" rather than as a clean slate.
        """
        conn = sqlite3.connect(str(populated))
        before = conn.execute("SELECT COUNT(*) FROM habit_activity_periods").fetchone()[0]
        conn.close()
        assert before > 0

        reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--no-backup"]
        )

        conn = sqlite3.connect(str(populated))
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM habit_activity_periods").fetchone()[0]
                == before
            )
        finally:
            conn.close()

    def test_it_leaves_a_sound_database(self, populated):
        reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--no-backup"]
        )
        conn = sqlite3.connect(str(populated))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

    def test_the_schema_version_is_unchanged(self, populated):
        """It clears rows; it must never look like a migration."""
        conn = sqlite3.connect(str(populated))
        before = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--no-backup"]
        )

        conn = sqlite3.connect(str(populated))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == before
        finally:
            conn.close()

    def test_a_dry_run_deletes_nothing(self, populated):
        before = _counts(populated, LOG_TABLES)
        assert reset_diet_logs.main(
            ["--db", str(populated), "--everything", "--dry-run"]
        ) == 0
        assert _counts(populated, LOG_TABLES) == before

    def test_it_still_refuses_without_a_backup_destination(self, populated):
        before = _counts(populated, LOG_TABLES)
        assert reset_diet_logs.main(["--db", str(populated), "--everything"]) == 2
        assert _counts(populated, LOG_TABLES) == before


class TestSelectiveGroups:
    """Each kind can be cleared alone, so a wipe is never all-or-nothing."""

    def test_weight_alone(self, populated):
        reset_diet_logs.main(
            ["--db", str(populated), "--weight", "--gym-only", "--no-backup"]
        )
        counts = _counts(populated, ("weight_logs", "diet_logs", "habit_logs"))
        assert counts["weight_logs"] == 0
        assert counts["habit_logs"] == 1

    def test_check_offs_alone_keep_their_definitions(self, populated):
        reset_diet_logs.main(
            [
                "--db", str(populated),
                "--habits", "--supplements", "--gym-only", "--no-backup",
            ]
        )
        counts = _counts(
            populated, ("habit_logs", "supplement_logs", "habits", "supplements")
        )
        assert counts["habit_logs"] == 0
        assert counts["supplement_logs"] == 0
        assert counts["habits"] > 0
        assert counts["supplements"] > 0

    def test_the_default_run_still_clears_only_meals(self, populated):
        """Backwards compatibility: the old invocation is unchanged."""
        reset_diet_logs.main(["--db", str(populated), "--no-backup"])
        counts = _counts(
            populated, ("diet_logs", "gym_logs", "weight_logs", "habit_logs")
        )
        assert counts["diet_logs"] == 0
        assert counts["gym_logs"] > 0
        assert counts["weight_logs"] == 1
        assert counts["habit_logs"] == 1


class TestCoverage:
    def test_every_log_table_in_the_schema_has_a_flag(self):
        """A table added later must not silently escape ``--everything``.

        The failure this prevents is invisible: a new log table simply survives
        the wipe, and nobody notices until stale rows show up in a chart that
        was supposed to start empty.
        """
        swept = set()
        for _label, statement in (
            reset_diet_logs.DIET_TARGETS
            + reset_diet_logs.GYM_TARGETS
            + reset_diet_logs.ALL_RECEIPTS_TARGET
            + tuple(
                target
                for targets, _c, _d in reset_diet_logs.OPTIONAL_GROUPS.values()
                for target in targets
            )
        ):
            swept.add(statement.split("FROM")[1].split()[0].strip())

        assert set(LOG_TABLES) <= swept, set(LOG_TABLES) - swept

    def test_no_definition_table_is_ever_a_target(self):
        """The other direction, and the more damaging one to get wrong."""
        swept = set()
        for _label, statement in (
            reset_diet_logs.DIET_TARGETS
            + reset_diet_logs.GYM_TARGETS
            + reset_diet_logs.ALL_RECEIPTS_TARGET
            + tuple(
                target
                for targets, _c, _d in reset_diet_logs.OPTIONAL_GROUPS.values()
                for target in targets
            )
        ):
            swept.add(statement.split("FROM")[1].split()[0].strip())

        assert swept & set(DEFINITION_TABLES) == set()
