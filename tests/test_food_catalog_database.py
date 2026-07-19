"""Database contracts for user foods, portions, recipes, and ingredients."""

from __future__ import annotations

import sqlite3

import pytest

from bot import database as database_module

pytestmark = pytest.mark.asyncio


async def _food(db, user_id, name="Apple", unit="g"):
    result = await db.save_food(
        user_id,
        name,
        unit,
        100,
        calories=52,
        protein_g=0.3,
        carbs_g=14,
        fat_g=0.2,
    )
    assert result["status"] == "added"
    return result["food"]


async def _recipe(db, user_id, name="Fruit bowl"):
    result = await db.save_recipe(user_id, name, 2, "serving")
    assert result["status"] == "added"
    return result["recipe"]


class TestCatalogSchema:
    async def test_catalog_tables_indexes_and_units_exist(self, db):
        await db.init_db()

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        assert {"foods", "food_portions", "recipes", "recipe_ingredients"} <= tables

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
        indexes = {row["name"] for row in await cursor.fetchall()}
        assert {
            "idx_foods_active_name",
            "idx_food_portions_lookup",
            "idx_recipes_active_name",
            "idx_recipe_ingredients_lookup",
        } <= indexes

        cursor = await db.conn.execute("PRAGMA table_info(foods)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert {
            "base_unit",
            "basis_amount",
            "calories",
            "protein_g",
            "carbs_g",
            "fat_g",
            "is_active",
        } <= columns

    async def test_composite_ownership_foreign_keys_reject_cross_user_rows(
        self, db_with_user, user_id
    ):
        other_user = user_id + 1
        await db_with_user.ensure_user(other_user, "other", "Other")
        food = await _food(db_with_user, user_id)
        recipe = await _recipe(db_with_user, other_user)

        with pytest.raises(sqlite3.IntegrityError):
            await db_with_user.conn.execute(
                "INSERT INTO recipe_ingredients "
                "(user_id, recipe_id, food_id, base_amount, display_amount, "
                "display_unit) VALUES (?, ?, ?, ?, ?, ?)",
                (other_user, recipe["id"], food["id"], 100, 100, "g"),
            )
        await db_with_user.conn.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            await db_with_user.conn.execute(
                "INSERT INTO food_portions "
                "(user_id, food_id, name, name_key, base_amount) "
                "VALUES (?, ?, ?, ?, ?)",
                (other_user, food["id"], "medium", "medium", 182),
            )
        await db_with_user.conn.rollback()


class TestFoods:
    async def test_save_update_lookup_list_archive_and_recreate(
        self, db_with_user, user_id
    ):
        added = await db_with_user.save_food(
            user_id,
            "  Red   Apple  ",
            "G",
            100,
            calories=52,
            protein_g=0.3,
            carbs_g=14,
            fat_g=0.2,
        )
        assert added["status"] == "added"
        food = added["food"]
        assert food["name"] == "Red Apple"
        assert food["name_key"] == "red apple"
        assert food["base_unit"] == "g"
        assert food["basis_amount"] == pytest.approx(100)

        updated = await db_with_user.save_food(
            user_id, "RED APPLE", "g", 150, calories=80
        )
        assert updated["status"] == "updated"
        assert updated["food"]["id"] == food["id"]
        assert updated["food"]["basis_amount"] == pytest.approx(150)
        assert updated["food"]["protein_g"] is None

        found = await db_with_user.get_food_by_key(user_id, " red apple ")
        assert found == updated["food"]
        assert await db_with_user.list_foods(user_id) == [updated["food"]]

        archived = await db_with_user.archive_food(user_id, food["id"])
        assert archived == {
            "status": "updated",
            "action": "archived",
            "food_id": food["id"],
        }
        assert await db_with_user.get_food_by_key(user_id, "Red Apple") is None
        assert await db_with_user.list_foods(user_id) == []

        recreated = await db_with_user.save_food(user_id, "red apple", "g", 100)
        assert recreated["status"] == "added"
        assert recreated["food"]["id"] != food["id"]

    async def test_archive_food_deletes_orphaned_portions(self, db_with_user, user_id):
        """EDGE-5: Archiving a food deletes its portions, so re-creation is clean."""
        food = await _food(db_with_user, user_id)
        
        # Add a portion
        await db_with_user.save_food_portion(
            user_id, food["id"], " Medium ", 182, "g"
        )
        
        # Archive the food
        archived = await db_with_user.archive_food(user_id, food["id"])
        assert archived["status"] == "updated"
        
        # Ensure portions are gone
        portions = await db_with_user.get_food_portions(user_id, food["id"])
        assert len(portions) == 0
        
        # Ensure re-creating the food gives a clean slate
        recreated = await db_with_user.save_food(user_id, "Apple", "g", 100)
        assert recreated["status"] == "added"
        new_food_id = recreated["food"]["id"]
        
        new_portions = await db_with_user.get_food_portions(user_id, new_food_id)
        assert len(new_portions) == 0

    async def test_food_unit_is_immutable_and_users_are_isolated(
        self, db_with_user, user_id
    ):
        food = await _food(db_with_user, user_id)
        other_user = user_id + 1
        await db_with_user.ensure_user(other_user, "other", "Other")

        mismatch = await db_with_user.save_food(
            user_id, "apple", "piece", 1, calories=80
        )
        assert mismatch == {
            "status": "unit_mismatch",
            "food": None,
            "expected_unit": "g",
            "provided_unit": "piece",
        }
        unchanged = await db_with_user.get_food_by_key(user_id, "apple")
        assert unchanged["id"] == food["id"]
        assert unchanged["calories"] == pytest.approx(52)
        assert await db_with_user.get_food_by_key(other_user, "apple") is None
        assert await db_with_user.archive_food(other_user, food["id"]) == {
            "status": "not_found",
            "food_id": None,
        }

    @pytest.mark.parametrize("unit", ["g", "ml", "piece"])
    async def test_all_food_base_units(self, db_with_user, user_id, unit):
        result = await db_with_user.save_food(user_id, f"Food {unit}", unit, 1)
        assert result["status"] == "added"
        assert result["food"]["base_unit"] == unit

    @pytest.mark.parametrize("bad_value", [0, -1, float("nan"), float("inf"), True])
    async def test_amounts_must_be_positive_finite_and_bounded(
        self, db_with_user, user_id, bad_value
    ):
        with pytest.raises(ValueError):
            await db_with_user.save_food(user_id, "Bad", "g", bad_value)

    async def test_catalog_text_rejects_embedded_control_characters(
        self, db_with_user, user_id
    ):
        for invalid in ("apple\x00hidden", "apple\nadmin", "apple\tadmin"):
            with pytest.raises(ValueError, match="control"):
                await db_with_user.save_food(user_id, invalid, "g", 100)

    async def test_food_limit_status_does_not_block_updates(
        self, db_with_user, user_id, monkeypatch
    ):
        monkeypatch.setattr(database_module, "MAX_ACTIVE_FOODS", 1)
        await _food(db_with_user, user_id)

        limited = await db_with_user.save_food(user_id, "Banana", "g", 100)
        assert limited == {"status": "limit", "food": None, "limit": 1}
        updated = await db_with_user.save_food(user_id, "Apple", "g", 120)
        assert updated["status"] == "updated"


class TestFoodPortions:
    async def test_portion_add_update_remove_and_unit_mismatch(
        self, db_with_user, user_id
    ):
        food = await _food(db_with_user, user_id)
        added = await db_with_user.save_food_portion(
            user_id, food["id"], " Medium ", 182, "g"
        )
        assert added["status"] == "added"
        portion = added["portion"]
        assert portion["name"] == "Medium"
        assert portion["base_amount"] == pytest.approx(182)
        assert portion["food_base_unit"] == "g"

        updated = await db_with_user.save_food_portion(
            user_id, food["id"], "MEDIUM", 190, "g"
        )
        assert updated["status"] == "updated"
        assert updated["portion"]["id"] == portion["id"]
        assert updated["portion"]["base_amount"] == pytest.approx(190)
        assert await db_with_user.get_food_portions(user_id, food["id"]) == [
            updated["portion"]
        ]

        mismatch = await db_with_user.save_food_portion(
            user_id, food["id"], "cup", 1, "ml"
        )
        assert mismatch["status"] == "unit_mismatch"
        assert mismatch["expected_unit"] == "g"
        assert mismatch["provided_unit"] == "ml"

        with pytest.raises(ValueError, match="standard base unit"):
            await db_with_user.save_food_portion(
                user_id, food["id"], "kg", 1000, "g"
            )

        piece = await db_with_user.save_food_portion(
            user_id, food["id"], "pcs", 50, "g"
        )
        assert piece["status"] == "added"
        assert piece["portion"]["name_key"] == "piece"

        removed = await db_with_user.remove_food_portion(
            user_id, food["id"], portion["id"]
        )
        assert removed["status"] == "updated"
        assert removed["action"] == "removed"
        await db_with_user.remove_food_portion(
            user_id, food["id"], piece["portion"]["id"]
        )
        assert await db_with_user.get_food_portions(user_id, food["id"]) == []

    async def test_cross_dimension_alias_is_stored_per_canonical_unit(
        self, db_with_user, user_id
    ):
        soup = (
            await db_with_user.save_food(
                user_id, "Soup", "ml", 100, calories=50
            )
        )["food"]

        result = await db_with_user.save_food_portion(
            user_id, soup["id"], "kg", 1000, "ml"
        )

        assert result["status"] == "added"
        assert result["portion"]["name_key"] == "g"
        assert result["portion"]["base_amount"] == pytest.approx(1)

        with pytest.raises(ValueError, match="Canonical portion amount"):
            await db_with_user.save_food_portion(
                user_id, soup["id"], "kg", 5e-324, "ml"
            )

    async def test_portions_enforce_ownership_not_found_and_limit(
        self, db_with_user, user_id, monkeypatch
    ):
        food = await _food(db_with_user, user_id)
        other_user = user_id + 1
        await db_with_user.ensure_user(other_user, "other", "Other")

        assert await db_with_user.save_food_portion(
            other_user, food["id"], "medium", 182, "g"
        ) == {"status": "not_found", "portion": None}
        assert await db_with_user.get_food_portions(other_user, food["id"]) == []

        monkeypatch.setattr(database_module, "MAX_PORTIONS_PER_FOOD", 1)
        await db_with_user.save_food_portion(user_id, food["id"], "small", 150, "g")
        limited = await db_with_user.save_food_portion(
            user_id, food["id"], "large", 220, "g"
        )
        assert limited == {"status": "limit", "portion": None, "limit": 1}


class TestRecipes:
    @pytest.mark.parametrize("unit", ["g", "ml", "piece", "serving"])
    async def test_all_recipe_yield_units(self, db_with_user, user_id, unit):
        result = await db_with_user.save_recipe(user_id, f"Recipe {unit}", 4, unit)
        assert result["status"] == "added"
        assert result["recipe"]["yield_unit"] == unit

    async def test_recipe_update_lookup_archive_and_recreate(
        self, db_with_user, user_id
    ):
        recipe = await _recipe(db_with_user, user_id, " Fruit   Bowl ")
        assert recipe["name"] == "Fruit Bowl"
        assert recipe["name_key"] == "fruit bowl"

        updated = await db_with_user.save_recipe(
            user_id, "FRUIT BOWL", 800, "g"
        )
        assert updated["status"] == "updated"
        assert updated["recipe"]["id"] == recipe["id"]
        assert updated["recipe"]["yield_amount"] == pytest.approx(800)
        assert await db_with_user.get_recipe_by_key(user_id, "fruit bowl") == updated[
            "recipe"
        ]

        archived = await db_with_user.archive_recipe(user_id, recipe["id"])
        assert archived["status"] == "updated"
        assert archived["action"] == "archived"
        assert await db_with_user.list_recipes(user_id) == []

        recreated = await db_with_user.save_recipe(
            user_id, "fruit bowl", 2, "serving"
        )
        assert recreated["status"] == "added"
        assert recreated["recipe"]["id"] != recipe["id"]

    async def test_recipe_and_ingredient_limits(
        self, db_with_user, user_id, monkeypatch
    ):
        monkeypatch.setattr(database_module, "MAX_ACTIVE_RECIPES", 1)
        recipe = await _recipe(db_with_user, user_id)
        limited_recipe = await db_with_user.save_recipe(
            user_id, "Second recipe", 1, "serving"
        )
        assert limited_recipe == {
            "status": "limit",
            "recipe": None,
            "limit": 1,
        }

        monkeypatch.setattr(database_module, "MAX_INGREDIENTS_PER_RECIPE", 1)
        apple = await _food(db_with_user, user_id)
        banana = await _food(db_with_user, user_id, "Banana")
        first = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], apple["id"], 182, "g", 1, "medium"
        )
        assert first["status"] == "added"
        limited_ingredient = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], banana["id"], 100, "g", 100, "g"
        )
        assert limited_ingredient == {
            "status": "limit",
            "ingredient": None,
            "limit": 1,
        }


class TestRecipeIngredients:
    async def test_ingredient_add_update_join_and_remove(
        self, db_with_user, user_id
    ):
        food = await _food(db_with_user, user_id)
        recipe = await _recipe(db_with_user, user_id)
        added = await db_with_user.save_recipe_ingredient(
            user_id,
            recipe["id"],
            food["id"],
            182,
            "g",
            1,
            "medium",
        )
        assert added["status"] == "added"
        ingredient = added["ingredient"]
        assert ingredient["base_amount"] == pytest.approx(182)
        assert ingredient["display_amount"] == pytest.approx(1)
        assert ingredient["display_unit"] == "medium"
        assert ingredient["food_name"] == "Apple"
        assert ingredient["food_base_unit"] == "g"
        assert ingredient["food_basis_amount"] == pytest.approx(100)
        assert ingredient["food_calories"] == pytest.approx(52)
        assert ingredient["food_protein_g"] == pytest.approx(0.3)

        updated = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], food["id"], 220, "g", 220, "g"
        )
        assert updated["status"] == "updated"
        assert updated["ingredient"]["id"] == ingredient["id"]
        assert updated["ingredient"]["base_amount"] == pytest.approx(220)
        assert await db_with_user.get_recipe_ingredients(user_id, recipe["id"]) == [
            updated["ingredient"]
        ]

        removed = await db_with_user.remove_recipe_ingredient(
            user_id, recipe["id"], ingredient["id"]
        )
        assert removed["status"] == "updated"
        assert removed["action"] == "removed"
        assert await db_with_user.get_recipe_ingredients(user_id, recipe["id"]) == []

    async def test_ingredient_ownership_unit_mismatch_and_archived_food_snapshot(
        self, db_with_user, user_id
    ):
        food = await _food(db_with_user, user_id)
        recipe = await _recipe(db_with_user, user_id)
        other_user = user_id + 1
        await db_with_user.ensure_user(other_user, "other", "Other")
        other_recipe = await _recipe(db_with_user, other_user, "Other recipe")

        assert await db_with_user.save_recipe_ingredient(
            other_user, other_recipe["id"], food["id"], 100, "g", 100, "g"
        ) == {"status": "not_found", "ingredient": None}

        mismatch = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], food["id"], 1, "piece", 1, "piece"
        )
        assert mismatch["status"] == "unit_mismatch"
        assert mismatch["expected_unit"] == "g"

        added = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], food["id"], 100, "g", 100, "g"
        )
        await db_with_user.archive_food(user_id, food["id"])
        ingredients = await db_with_user.get_recipe_ingredients(
            user_id, recipe["id"]
        )
        assert ingredients[0]["id"] == added["ingredient"]["id"]
        assert ingredients[0]["food_is_active"] == 0
        assert await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], food["id"], 200, "g", 200, "g"
        ) == {"status": "not_found", "ingredient": None}

    async def test_recreated_food_replaces_same_named_archived_recipe_item(
        self, db_with_user, user_id
    ):
        old_food = await _food(db_with_user, user_id)
        recipe = await _recipe(db_with_user, user_id)
        original = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], old_food["id"], 182, "g", 1, "medium"
        )
        await db_with_user.archive_food(user_id, old_food["id"])
        new_food = (
            await db_with_user.save_food(
                user_id, "apple", "piece", 1, calories=80
            )
        )["food"]

        replaced = await db_with_user.save_recipe_ingredient(
            user_id, recipe["id"], new_food["id"], 1, "piece", 1, "piece"
        )

        assert replaced["status"] == "updated"
        assert replaced["ingredient"]["food_id"] == new_food["id"]
        assert replaced["ingredient"]["id"] != original["ingredient"]["id"]
        ingredients = await db_with_user.get_recipe_ingredients(
            user_id, recipe["id"]
        )
        assert [row["food_id"] for row in ingredients] == [new_food["id"]]

    async def test_failed_catalog_write_rolls_back_and_releases_lock(
        self, db_with_user, user_id
    ):
        await db_with_user.conn.executescript(
            """
            CREATE TRIGGER fail_food_insert
            BEFORE INSERT ON foods
            BEGIN
                SELECT RAISE(ABORT, 'forced catalog failure');
            END;
            """
        )
        await db_with_user.conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced catalog failure"):
            await db_with_user.save_food(user_id, "Apple", "g", 100)
        assert db_with_user.conn.in_transaction is False

        await db_with_user.conn.execute("DROP TRIGGER fail_food_insert")
        await db_with_user.conn.commit()
        result = await db_with_user.save_food(user_id, "Apple", "g", 100)
        assert result["status"] == "added"
