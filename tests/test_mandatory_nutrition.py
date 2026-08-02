"""Nutrition is required on every log, everywhere.

Tracking macros is the point of the ledger. Before this rule the fastest paths
through the app were also the ones that wrote incomplete rows — ``/skip`` at
both guided prompts, a bare ``/diet lunch dal 650``, a ``/food add`` naming one
nutrient — and the real ledger ended up with six of nine meals carrying calories
and no macros, plus one row with no nutrition at all.

There are two legitimate sources for the numbers, and estimating is not one of
them: a stored definition (saved food, recipe, shared catalog) or the user
typing them. Every entry point is covered below, because a rule enforced at four
of five doors is not a rule.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.nutrition import NutritionError, parse_nutrient_labels, require_complete_nutrients

UID = 123456789


def _complete(**overrides):
    item = {
        "source_type": "freetext",
        "source_id": None,
        "source_provider": None,
        "source_revision": None,
        "display_name": "Test item",
        "entered_amount": None,
        "entered_unit": None,
        "resolved_base_amount": None,
        "resolved_base_unit": None,
        "calories": 100,
        "protein_g": 5.0,
        "carbs_g": 10.0,
        "fat_g": 2.0,
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# The shared rule
# ---------------------------------------------------------------------------
class TestTheSharedDefinitionOfComplete:
    """One definition, so no two layers can disagree about what may be stored."""

    def test_zero_is_a_value_not_a_gap(self):
        require_complete_nutrients(
            {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}
        )

    @pytest.mark.parametrize(
        "missing", ["calories", "protein_g", "carbs_g", "fat_g"]
    )
    def test_any_single_gap_is_refused(self, missing):
        values = {"calories": 100, "protein_g": 1, "carbs_g": 2, "fat_g": 3}
        values[missing] = None
        with pytest.raises(NutritionError):
            require_complete_nutrients(values)

    def test_the_message_names_what_is_missing_and_the_subject(self):
        with pytest.raises(NutritionError) as excinfo:
            require_complete_nutrients(
                {"calories": 100, "protein_g": None, "carbs_g": None, "fat_g": 3},
                what="'2 rotis'",
            )
        message = str(excinfo.value)
        assert "2 rotis" in message
        assert "protein" in message and "carbs" in message
        assert "fat" not in message, "fat was supplied; don't report it as missing"


# ---------------------------------------------------------------------------
# Entry point: saved foods
# ---------------------------------------------------------------------------
class TestSavedFoodsMustBeComplete:
    def test_all_four_labels_are_required(self):
        with pytest.raises(NutritionError, match="f="):
            parse_nutrient_labels(["kcal=389", "p=16.9", "c=66"])

    def test_a_complete_definition_parses(self):
        assert parse_nutrient_labels(
            ["kcal=389", "p=16.9", "c=66", "f=6.9"]
        ) == {
            "calories": 389.0,
            "protein_g": 16.9,
            "carbs_g": 66.0,
            "fat_g": 6.9,
        }

    async def test_the_database_refuses_a_partial_food(self, db_with_user, user_id):
        with pytest.raises(NutritionError, match="saved food"):
            await db_with_user.save_food(
                user_id, "oats", "g", 100, 389, 16.9, 66, None
            )
        assert await db_with_user.list_foods(user_id) == []

    async def test_a_complete_food_is_stored_and_reusable(self, db_with_user, user_id):
        result = await db_with_user.save_food(
            user_id, "oats", "g", 100, 389, 16.9, 66, 6.9
        )
        food = result["food"]
        assert (food["calories"], food["protein_g"]) == (389, 16.9)
        assert (food["carbs_g"], food["fat_g"]) == (66, 6.9)


# ---------------------------------------------------------------------------
# Entry point: the write path (the backstop under every handler)
# ---------------------------------------------------------------------------
class TestTheWritePathIsTheBackstop:
    async def test_log_diet_requires_every_nutrient(self, db_with_user, user_id):
        with pytest.raises(NutritionError):
            await db_with_user.log_diet(
                user_id, "lunch", "mystery", 100, None, 2.0, 3.0
            )

    async def test_a_multi_item_meal_names_the_offending_item(
        self, db_with_user, user_id
    ):
        items = [
            _complete(display_name="rice"),
            _complete(display_name="dal", carbs_g=None),
        ]
        with pytest.raises(NutritionError, match="dal"):
            await db_with_user.log_diet_with_items(user_id, "lunch", items)

    async def test_a_refused_meal_writes_nothing_at_all(self, db_with_user, user_id):
        from bot.config import today_local

        items = [_complete(display_name="rice"), _complete(fat_g=None)]
        with pytest.raises(NutritionError):
            await db_with_user.log_diet_with_items(user_id, "lunch", items)

        rows = await db_with_user.get_diet_logs(
            user_id, today_local(), today_local()
        )
        assert rows == []
        cursor = await db_with_user.conn.execute(
            "SELECT COUNT(*) FROM diet_log_items"
        )
        assert (await cursor.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# Entry point: typed meals and voice (they share this planner)
# ---------------------------------------------------------------------------
class TestTypedMealsRefuseIncompleteDefinitions:
    async def test_a_legacy_food_missing_a_macro_is_listed_not_logged(self):
        from bot.meal_text import parse_meal_text
        from bot.services.typed_meal import plan_typed_meal

        legacy = {
            "id": 1, "name": "oats", "base_unit": "g", "basis_amount": 100.0,
            "calories": 380, "protein_g": 13.0, "carbs_g": None, "fat_g": 7.0,
        }
        db = SimpleNamespace(
            list_foods=AsyncMock(return_value=[legacy]),
            list_recipes=AsyncMock(return_value=[]),
            get_food_portions=AsyncMock(return_value=[]),
            search_catalog=AsyncMock(return_value=[]),
            get_catalog_portions=AsyncMock(return_value=[]),
        )

        plan = await plan_typed_meal(db, UID, parse_meal_text("100g oats"))

        assert plan.resolved == ()
        assert plan.unresolved[0].reason == "incomplete_nutrition"

    async def test_the_catalog_resolves_completely(self, db, user_id):
        """The happy path this rule depends on: the catalog supplies all four."""
        from bot.catalog_seed import CATALOG_FOODS
        from bot.meal_text import parse_meal_text
        from bot.services.typed_meal import plan_typed_meal

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)

        plan = await plan_typed_meal(db, user_id, parse_meal_text("100g oats"))

        entry = plan.resolved[0]
        assert entry.calories is not None
        assert entry.protein_g is not None
        assert entry.carbs_g is not None
        assert entry.fat_g is not None

    def test_every_seeded_catalog_food_carries_all_four(self):
        """The shared catalog is the fallback for both users; no holes allowed."""
        from bot.catalog_seed import CATALOG_FOODS

        for food in CATALOG_FOODS:
            for field in ("calories", "protein_g", "carbs_g", "fat_g"):
                assert food.get(field) is not None, f"{food['display_name']}: {field}"


# ---------------------------------------------------------------------------
# The reset script used for the changeover
# ---------------------------------------------------------------------------
class TestResetDietLogsScript:
    def _seed(self, path):
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE diet_logs (id INTEGER PRIMARY KEY, user_id INTEGER);
            CREATE TABLE diet_log_items (id INTEGER PRIMARY KEY, diet_log_id INTEGER);
            CREATE TABLE mutation_receipts (
                telegram_update_id INTEGER, entity_type TEXT, entity_id INTEGER
            );
            CREATE TABLE habits (id INTEGER PRIMARY KEY, habit_name TEXT);
            CREATE TABLE foods (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO diet_logs VALUES (1, 7), (2, 7);
            INSERT INTO diet_log_items VALUES (1, 1), (2, 2);
            INSERT INTO mutation_receipts VALUES (10, 'diet', 1), (11, 'study', 5);
            INSERT INTO habits VALUES (1, 'Read');
            INSERT INTO foods VALUES (1, 'oats');
            """
        )
        conn.commit()
        conn.close()

    def test_dry_run_changes_nothing(self, tmp_path, capsys):
        from scripts import reset_diet_logs

        path = tmp_path / "ledger.db"
        self._seed(path)

        assert reset_diet_logs.main(["--db", str(path), "--dry-run"]) == 0

        conn = sqlite3.connect(str(path))
        assert conn.execute("SELECT COUNT(*) FROM diet_logs").fetchone()[0] == 2
        conn.close()

    def test_deleting_without_a_destination_is_refused(self, tmp_path):
        from scripts import reset_diet_logs

        path = tmp_path / "ledger.db"
        self._seed(path)

        assert reset_diet_logs.main(["--db", str(path)]) == 2

        conn = sqlite3.connect(str(path))
        assert conn.execute("SELECT COUNT(*) FROM diet_logs").fetchone()[0] == 2
        conn.close()

    def test_it_clears_meals_and_their_receipts_only(self, tmp_path):
        from scripts import reset_diet_logs

        path = tmp_path / "ledger.db"
        self._seed(path)

        assert reset_diet_logs.main(["--db", str(path), "--no-backup"]) == 0

        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM diet_logs").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM diet_log_items").fetchone()[0] == 0
            )
            # The diet receipt goes; another category's must not.
            rows = conn.execute(
                "SELECT entity_type FROM mutation_receipts"
            ).fetchall()
            assert [r[0] for r in rows] == ["study"]
            # Nothing outside the diet history is touched.
            assert conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 1
        finally:
            conn.close()

    def test_a_missing_database_is_reported_not_created(self, tmp_path):
        from scripts import reset_diet_logs

        missing = tmp_path / "nope.db"
        assert reset_diet_logs.main(["--db", str(missing), "--no-backup"]) == 2
        assert not missing.exists()
