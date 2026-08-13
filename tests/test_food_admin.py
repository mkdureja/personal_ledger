"""Tests for the local foods-and-recipes admin tool.

The tool writes to a real ledger, so the things worth proving are that it
validates like the bot does, that it cannot be driven by a page the user did not
open, and that it refuses a database it does not match rather than migrating it.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import pytest_asyncio

from bot import nutrition
from bot.database import DatabaseManager
from bot.meal_models import DefaultQuantity
from ledger_schema import LATEST_SCHEMA_VERSION
from scripts import food_admin
from scripts.food_admin import (
    AdminError,
    AdminHandler,
    AdminServer,
    FoodSpec,
    IngredientSpec,
    LoopThread,
    RecipeSpec,
    apply_food,
    apply_recipe,
    check_schema,
    load_ledger,
    load_users,
    parse_food_payload,
    parse_portions,
    parse_recipe_payload,
    parse_user_ids,
)

MANOJ = 1554408692
RATIKA = 8908288417


def food_payload(**overrides):
    payload = {
        "name": "Rolled oats",
        "base_unit": "g",
        "basis_amount": 100,
        "calories": 389,
        "protein_g": 16.9,
        "carbs_g": 66.3,
        "fat_g": 6.9,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestFoodValidation:
    def test_accepts_a_complete_food(self):
        spec = parse_food_payload(food_payload())
        assert spec.name == "Rolled oats"
        assert spec.base_unit == "g"
        assert spec.basis_amount == 100
        assert spec.calories == 389
        assert spec.portions == ()
        assert spec.default is None

    @pytest.mark.parametrize("field", ["calories", "protein_g", "carbs_g", "fat_g"])
    def test_every_macro_is_required(self, field):
        """Mandatory nutrition is the rule everywhere; this door is not an exception."""
        payload = food_payload()
        del payload[field]
        with pytest.raises(AdminError, match="must be a number"):
            parse_food_payload(payload)

    @pytest.mark.parametrize("field", ["calories", "protein_g", "carbs_g", "fat_g"])
    def test_zero_is_a_value(self, field):
        spec = parse_food_payload(food_payload(**{field: 0}))
        assert getattr(spec, field) == 0

    def test_blank_is_not_a_value(self):
        with pytest.raises(AdminError):
            parse_food_payload(food_payload(protein_g=None))

    def test_rejects_a_negative_macro(self):
        with pytest.raises(AdminError, match="cannot be negative"):
            parse_food_payload(food_payload(fat_g=-1))

    def test_rejects_a_boolean_dressed_as_a_number(self):
        """True is an int in Python and would otherwise be stored as one calorie."""
        with pytest.raises(AdminError, match="must be a number"):
            parse_food_payload(food_payload(calories=True))

    def test_rejects_a_non_finite_number(self):
        with pytest.raises(AdminError, match="must be a number"):
            parse_food_payload(food_payload(calories=float("inf")))

    def test_basis_amount_must_be_positive(self):
        with pytest.raises(AdminError, match="greater than zero"):
            parse_food_payload(food_payload(basis_amount=0))

    def test_rejects_an_unknown_base_unit(self):
        with pytest.raises(AdminError, match="Base unit must be one of"):
            parse_food_payload(food_payload(base_unit="cup"))

    def test_rejects_a_serving_base_unit(self):
        """``serving`` is a recipe yield, never a food's storage dimension."""
        assert "serving" in nutrition.RECIPE_YIELD_UNITS
        assert "serving" not in nutrition.FOOD_BASE_UNITS
        with pytest.raises(AdminError):
            parse_food_payload(food_payload(base_unit="serving"))

    def test_name_is_required(self):
        with pytest.raises(AdminError, match="Name is required"):
            parse_food_payload(food_payload(name="   "))

    def test_name_length_is_bounded(self):
        with pytest.raises(AdminError, match="characters or fewer"):
            parse_food_payload(food_payload(name="x" * 101))

    def test_parses_a_usual_amount(self):
        spec = parse_food_payload(
            food_payload(default={"amount": 40, "unit": "g"})
        )
        assert spec.default == DefaultQuantity(amount=40.0, unit="g")

    def test_a_usual_amount_needs_a_unit(self):
        with pytest.raises(AdminError, match="needs a unit"):
            parse_food_payload(food_payload(default={"amount": 40, "unit": " "}))


class TestPortionValidation:
    def test_accepts_a_named_portion(self):
        portions = parse_portions([{"name": "bowl", "amount": 40}], "g")
        assert portions[0].name == "bowl"
        assert portions[0].amount == 40

    def test_rejects_a_portion_named_after_the_foods_own_unit(self):
        """``g`` on a per-gram food would shadow the unit the parser knows."""
        with pytest.raises(AdminError, match="duplicates this food's own base unit"):
            parse_portions([{"name": "g", "amount": 40}], "g")

    def test_allows_a_cross_dimension_standard_portion(self):
        """``piece = 50 g`` on a mass food is the documented way to log 2 eggs."""
        portions = parse_portions([{"name": "piece", "amount": 50}], "g")
        assert portions[0].name == "piece"

    def test_rejects_a_duplicate_portion(self):
        with pytest.raises(AdminError, match="listed twice"):
            parse_portions(
                [{"name": "Bowl", "amount": 40}, {"name": "bowl", "amount": 50}], "g"
            )

    def test_portion_amount_must_be_positive(self):
        with pytest.raises(AdminError, match="greater than zero"):
            parse_portions([{"name": "bowl", "amount": 0}], "g")

    def test_a_non_standard_name_is_stored_normalized(self):
        portions = parse_portions([{"name": "  Big Bowl ", "amount": 60}], "g")
        assert portions[0].name == "big bowl"

    def test_empty_is_allowed(self):
        assert parse_portions(None, "g") == ()
        assert parse_portions([], "g") == ()


class TestRecipeValidation:
    def test_accepts_a_recipe(self):
        spec = parse_recipe_payload(
            {
                "name": "Overnight oats",
                "yield_amount": 1,
                "yield_unit": "serving",
                "ingredients": [{"food": "Rolled oats", "amount": 60, "unit": "g"}],
            }
        )
        assert spec.name == "Overnight oats"
        assert spec.ingredients[0] == IngredientSpec("Rolled oats", 60.0, "g")

    def test_a_recipe_needs_at_least_one_ingredient(self):
        """Nutrition is derived, so an empty recipe could only ever total zero."""
        with pytest.raises(AdminError, match="at least one ingredient"):
            parse_recipe_payload(
                {
                    "name": "Nothing",
                    "yield_amount": 1,
                    "yield_unit": "serving",
                    "ingredients": [],
                }
            )

    def test_recipes_may_yield_servings(self):
        spec = parse_recipe_payload(
            {
                "name": "Dal",
                "yield_amount": 4,
                "yield_unit": "serving",
                "ingredients": [{"food": "Lentils", "amount": 200, "unit": "g"}],
            }
        )
        assert spec.yield_unit == "serving"

    def test_rejects_an_unknown_yield_unit(self):
        with pytest.raises(AdminError, match="Yield unit must be one of"):
            parse_recipe_payload(
                {
                    "name": "Dal",
                    "yield_amount": 4,
                    "yield_unit": "bowl",
                    "ingredients": [{"food": "Lentils", "amount": 200, "unit": "g"}],
                }
            )


class TestUserSelection:
    def test_accepts_known_users(self):
        assert parse_user_ids([MANOJ, RATIKA], [MANOJ, RATIKA]) == (MANOJ, RATIKA)

    def test_deduplicates(self):
        assert parse_user_ids([MANOJ, MANOJ], [MANOJ]) == (MANOJ,)

    def test_refuses_a_stranger(self):
        """Creating a user is the bot's job, on a real message from that person."""
        with pytest.raises(AdminError, match="not a user of this ledger"):
            parse_user_ids([999], [MANOJ])

    def test_refuses_an_empty_choice(self):
        with pytest.raises(AdminError, match="at least one person"):
            parse_user_ids([], [MANOJ])


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_user_db():
    manager = DatabaseManager(":memory:")
    await manager.connect()
    await manager.init_db()
    await manager.ensure_user(MANOJ, "manoj", "Manoj")
    await manager.ensure_user(RATIKA, "ratika", "Ratika")
    yield manager
    await manager.close()


OATS = FoodSpec(
    name="Rolled oats",
    base_unit="g",
    basis_amount=100,
    calories=389,
    protein_g=16.9,
    carbs_g=66.3,
    fat_g=6.9,
)


class TestApplyFood:
    @pytest.mark.asyncio
    async def test_saves_into_both_ledgers_as_separate_rows(self, two_user_db):
        results = await apply_food(two_user_db, [MANOJ, RATIKA], OATS)
        assert [r["ok"] for r in results] == [True, True]

        mine = await two_user_db.list_foods(MANOJ)
        hers = await two_user_db.list_foods(RATIKA)
        assert [f["name"] for f in mine] == ["Rolled oats"]
        assert [f["name"] for f in hers] == ["Rolled oats"]
        # Separate rows, so one person editing theirs never moves the other's.
        assert mine[0]["id"] != hers[0]["id"]

    @pytest.mark.asyncio
    async def test_goes_through_save_food_so_nutrition_stays_mandatory(
        self, two_user_db
    ):
        """A hole here would spread to every meal logged from the food."""
        holed = FoodSpec(
            name="Mystery",
            base_unit="g",
            basis_amount=100,
            calories=100,
            protein_g=None,  # type: ignore[arg-type]
            carbs_g=1,
            fat_g=1,
        )
        results = await apply_food(two_user_db, [MANOJ], holed)
        assert results[0]["ok"] is False
        assert await two_user_db.list_foods(MANOJ) == []

    @pytest.mark.asyncio
    async def test_stores_portions(self, two_user_db):
        spec = FoodSpec(
            **{**OATS.__dict__, "portions": (food_admin.PortionSpec("bowl", 40),)}
        )
        results = await apply_food(two_user_db, [MANOJ], spec)
        assert results[0]["ok"] is True
        food = await two_user_db.get_food_by_key(MANOJ, "Rolled oats")
        portions = await two_user_db.get_food_portions(MANOJ, food["id"])
        assert [(p["name"], p["base_amount"]) for p in portions] == [("bowl", 40.0)]

    @pytest.mark.asyncio
    async def test_a_usual_amount_makes_the_food_tappable(self, two_user_db):
        spec = FoodSpec(**{**OATS.__dict__, "default": DefaultQuantity(40.0, "g")})
        results = await apply_food(two_user_db, [MANOJ], spec)
        assert results[0]["ok"] is True
        assert results[0]["notes"] == []
        food = await two_user_db.get_food_by_key(MANOJ, "Rolled oats")
        prefs = await two_user_db.get_food_preferences(MANOJ)
        stored = prefs.get(("food", food["id"]))
        assert stored is not None
        assert stored["default_amount"] == 40.0
        assert stored["default_unit"] == "g"

    @pytest.mark.asyncio
    async def test_an_unusable_usual_amount_downgrades_instead_of_failing(
        self, two_user_db
    ):
        """The food is already saved and useful; do not undo it over a default."""
        spec = FoodSpec(**{**OATS.__dict__, "default": DefaultQuantity(1.0, "scoop")})
        results = await apply_food(two_user_db, [MANOJ], spec)
        assert results[0]["ok"] is True
        assert any("usual amount" in note for note in results[0]["notes"])
        assert await two_user_db.get_food_by_key(MANOJ, "Rolled oats") is not None

    @pytest.mark.asyncio
    async def test_one_users_failure_does_not_block_the_other(self, two_user_db):
        """A user id with no row fails the foreign key, which must not discard
        the save that already succeeded for the person before them."""
        results = await apply_food(two_user_db, [MANOJ, 4242], OATS)
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False
        assert await two_user_db.get_food_by_key(MANOJ, "Rolled oats") is not None

    @pytest.mark.asyncio
    async def test_saving_the_same_name_twice_updates_rather_than_duplicates(
        self, two_user_db
    ):
        await apply_food(two_user_db, [MANOJ], OATS)
        revised = FoodSpec(**{**OATS.__dict__, "calories": 400})
        await apply_food(two_user_db, [MANOJ], revised)
        foods = await two_user_db.list_foods(MANOJ)
        assert len(foods) == 1
        assert foods[0]["calories"] == 400


class TestApplyRecipe:
    @pytest.mark.asyncio
    async def test_builds_a_recipe_from_each_users_own_foods(self, two_user_db):
        await apply_food(two_user_db, [MANOJ, RATIKA], OATS)
        spec = RecipeSpec(
            name="Overnight oats",
            yield_amount=1,
            yield_unit="serving",
            ingredients=(IngredientSpec("Rolled oats", 60, "g"),),
        )
        results = await apply_recipe(two_user_db, [MANOJ, RATIKA], spec)
        assert [r["ok"] for r in results] == [True, True]

        for user_id in (MANOJ, RATIKA):
            recipe = await two_user_db.get_recipe_by_key(user_id, "Overnight oats")
            rows = await two_user_db.get_recipe_ingredients(user_id, recipe["id"])
            # Each recipe points at that user's own food row, never the other's.
            own_food = await two_user_db.get_food_by_key(user_id, "Rolled oats")
            assert [r["food_id"] for r in rows] == [own_food["id"]]
            assert rows[0]["base_amount"] == 60.0

    @pytest.mark.asyncio
    async def test_names_the_missing_food_rather_than_failing_vaguely(
        self, two_user_db
    ):
        await apply_food(two_user_db, [MANOJ], OATS)
        spec = RecipeSpec(
            name="Overnight oats",
            yield_amount=1,
            yield_unit="serving",
            ingredients=(IngredientSpec("Rolled oats", 60, "g"),),
        )
        results = await apply_recipe(two_user_db, [MANOJ, RATIKA], spec)
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False
        assert "Rolled oats" in results[1]["error"]
        assert "add it as a food first" in results[1]["error"]

    @pytest.mark.asyncio
    async def test_a_named_portion_resolves_to_base_units(self, two_user_db):
        spec = FoodSpec(
            **{**OATS.__dict__, "portions": (food_admin.PortionSpec("bowl", 40),)}
        )
        await apply_food(two_user_db, [MANOJ], spec)
        recipe = RecipeSpec(
            name="Two bowls",
            yield_amount=1,
            yield_unit="serving",
            ingredients=(IngredientSpec("Rolled oats", 2, "bowl"),),
        )
        results = await apply_recipe(two_user_db, [MANOJ], recipe)
        assert results[0]["ok"] is True
        stored = await two_user_db.get_recipe_by_key(MANOJ, "Two bowls")
        rows = await two_user_db.get_recipe_ingredients(MANOJ, stored["id"])
        assert rows[0]["base_amount"] == 80.0

    @pytest.mark.asyncio
    async def test_a_missing_ingredient_leaves_no_empty_recipe_behind(
        self, two_user_db
    ):
        """The recipe row must not outlive the ingredient that failed.

        A recipe with no ingredients is not a harmless stub: nutrition is
        derived, so it totals zero and would log a meal of nothing.
        """
        await apply_food(two_user_db, [MANOJ], OATS)
        spec = RecipeSpec(
            name="Half known",
            yield_amount=1,
            yield_unit="serving",
            ingredients=(
                IngredientSpec("Rolled oats", 60, "g"),
                IngredientSpec("Salmon", 100, "g"),
            ),
        )
        results = await apply_recipe(two_user_db, [MANOJ], spec)
        assert results[0]["ok"] is False
        assert "Salmon" in results[0]["error"]
        assert await two_user_db.get_recipe_by_key(MANOJ, "Half known") is None
        assert await two_user_db.list_recipes(MANOJ) == []

    @pytest.mark.asyncio
    async def test_an_unusable_ingredient_unit_leaves_no_empty_recipe(
        self, two_user_db
    ):
        """Same rule when the food exists but the amount cannot be resolved."""
        await apply_food(two_user_db, [MANOJ], OATS)
        spec = RecipeSpec(
            name="Bad unit",
            yield_amount=1,
            yield_unit="serving",
            ingredients=(IngredientSpec("Rolled oats", 2, "scoop"),),
        )
        results = await apply_recipe(two_user_db, [MANOJ], spec)
        assert results[0]["ok"] is False
        assert await two_user_db.list_recipes(MANOJ) == []

    @pytest.mark.asyncio
    async def test_a_catalog_food_is_not_an_ingredient(self, two_user_db):
        """Recipe ingredients key to ``foods``; the shared catalog is elsewhere."""
        spec = RecipeSpec(
            name="Rice bowl",
            yield_amount=1,
            yield_unit="serving",
            ingredients=(IngredientSpec("White rice (cooked)", 100, "g"),),
        )
        results = await apply_recipe(two_user_db, [MANOJ], spec)
        assert results[0]["ok"] is False
        assert "saved foods" in results[0]["error"]


class TestReading:
    @pytest.mark.asyncio
    async def test_lists_users_with_a_readable_label(self, two_user_db):
        users = await load_users(two_user_db)
        assert {u["user_id"] for u in users} == {MANOJ, RATIKA}
        assert {u["label"] for u in users} == {"Manoj", "Ratika"}

    @pytest.mark.asyncio
    async def test_reports_an_empty_ledger_as_empty(self, two_user_db):
        assert await load_ledger(two_user_db, MANOJ) == {"foods": [], "recipes": []}

    @pytest.mark.asyncio
    async def test_reports_foods_with_their_portions(self, two_user_db):
        spec = FoodSpec(
            **{**OATS.__dict__, "portions": (food_admin.PortionSpec("bowl", 40),)}
        )
        await apply_food(two_user_db, [MANOJ], spec)
        ledger = await load_ledger(two_user_db, MANOJ)
        assert ledger["foods"][0]["name"] == "Rolled oats"
        assert ledger["foods"][0]["portions"] == [{"name": "bowl", "amount": 40.0}]


# ---------------------------------------------------------------------------
# Schema gate
# ---------------------------------------------------------------------------


class TestSchemaGate:
    def _db_at(self, tmp_path: Path, version: int) -> Path:
        path = tmp_path / "ledger.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        conn.close()
        return path

    def test_accepts_a_matching_database(self, tmp_path):
        path = self._db_at(tmp_path, LATEST_SCHEMA_VERSION)
        assert check_schema(path) == LATEST_SCHEMA_VERSION

    def test_refuses_an_older_database_instead_of_migrating_it(self, tmp_path):
        """Migration belongs to the bot's preflight, which backs up first."""
        path = self._db_at(tmp_path, LATEST_SCHEMA_VERSION - 1)
        with pytest.raises(SystemExit) as excinfo:
            check_schema(path)
        assert "never migrates" in str(excinfo.value)
        # Untouched: the gate must not be the thing that bumps the version.
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (
                LATEST_SCHEMA_VERSION - 1
            )
        finally:
            conn.close()

    def test_refuses_a_newer_database(self, tmp_path):
        path = self._db_at(tmp_path, LATEST_SCHEMA_VERSION + 1)
        with pytest.raises(SystemExit):
            check_schema(path)


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    """A real server on a real loopback port, driven over real HTTP."""
    loop = LoopThread()
    loop.start()
    db = DatabaseManager(":memory:")
    loop.run(db.connect())
    loop.run(db.init_db())
    loop.run(db.ensure_user(MANOJ, "manoj", "Manoj"))
    loop.run(db.ensure_user(RATIKA, "ratika", "Ratika"))
    users = loop.run(load_users(db))

    httpd = AdminServer(
        ("127.0.0.1", 0),
        AdminHandler,
        token="test-token",
        db=db,
        db_path=tmp_path / "ledger.db",
        users=users,
        schema_version=LATEST_SCHEMA_VERSION,
        loop=loop,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, db, loop
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        loop.run(db.close())
        loop.stop()


def request(base, path, *, token="test-token", body=None, origin=None, method=None):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        req.add_header("X-Admin-Token", token)
    if origin is not None:
        req.add_header("Origin", origin)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # HTTPError *is* the response object. Closing it matters: the suite runs
        # with warnings as errors, so a leaked handle surfaces later as an
        # unraisable-exception failure in whichever unrelated test triggers GC.
        with exc:
            return exc.code, json.loads(exc.read().decode())


class TestServing:
    def test_serves_the_page_with_a_token(self, server):
        base, _db, _loop = server
        req = urllib.request.Request(f"{base}/?token=test-token")
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode()
        assert response.status == 200
        assert "<title>Ledger" in body

    def test_refuses_the_page_without_the_token(self, server):
        """A loopback bind is not an authorization check on its own."""
        base, _db, _loop = server
        status, _body = request(base, "/", token=None)
        assert status == 403

    def test_refuses_a_wrong_token(self, server):
        base, _db, _loop = server
        status, _body = request(base, "/api/state", token="guessed")
        assert status == 403

    def test_reports_state(self, server):
        base, _db, _loop = server
        status, body = request(base, "/api/state")
        assert status == 200
        assert {u["user_id"] for u in body["users"]} == {MANOJ, RATIKA}
        assert body["schema_version"] == LATEST_SCHEMA_VERSION

    def test_saves_a_food_over_http(self, server):
        base, db, loop = server
        status, body = request(
            base,
            "/api/food",
            body={"user_ids": [MANOJ, RATIKA], "food": food_payload()},
        )
        assert status == 200
        assert [r["ok"] for r in body["results"]] == [True, True]
        # The reply carries fresh state, so the page never renders a stale list.
        assert [len(u["foods"]) for u in body["users"]] == [1, 1]
        assert len(loop.run(db.list_foods(MANOJ))) == 1

    def test_a_cross_site_post_is_refused(self, server):
        """Even with the token guessed, a page elsewhere cannot drive this."""
        base, db, loop = server
        status, _body = request(
            base,
            "/api/food",
            body={"user_ids": [MANOJ], "food": food_payload()},
            origin="https://example.com",
        )
        assert status == 403
        assert loop.run(db.list_foods(MANOJ)) == []

    def test_a_same_origin_post_is_allowed(self, server):
        base, _db, _loop = server
        status, _body = request(
            base,
            "/api/food",
            body={"user_ids": [MANOJ], "food": food_payload()},
            origin=base,
        )
        assert status == 200

    def test_a_bad_payload_answers_with_the_reason(self, server):
        base, db, loop = server
        status, body = request(
            base,
            "/api/food",
            body={"user_ids": [MANOJ], "food": food_payload(protein_g=None)},
        )
        assert status == 400
        assert "Protein g" in body["error"]
        assert loop.run(db.list_foods(MANOJ)) == []

    def test_an_unknown_route_is_not_found(self, server):
        base, _db, _loop = server
        status, _body = request(base, "/api/nope", body={"user_ids": [MANOJ]})
        assert status == 404

    def test_saves_a_recipe_over_http(self, server):
        base, db, loop = server
        request(
            base, "/api/food", body={"user_ids": [MANOJ], "food": food_payload()}
        )
        status, body = request(
            base,
            "/api/recipe",
            body={
                "user_ids": [MANOJ],
                "recipe": {
                    "name": "Overnight oats",
                    "yield_amount": 1,
                    "yield_unit": "serving",
                    "ingredients": [
                        {"food": "Rolled oats", "amount": 60, "unit": "g"}
                    ],
                },
            },
        )
        assert status == 200
        assert body["results"][0]["ok"] is True
        assert len(loop.run(db.list_recipes(MANOJ))) == 1


# ---------------------------------------------------------------------------
# The shared catalog
# ---------------------------------------------------------------------------


@pytest.fixture
def seed_file(tmp_path, monkeypatch):
    """A throwaway copy of the real seed file, so tests never edit the repo's."""
    copy = tmp_path / "catalog_seed.py"
    copy.write_text(
        food_admin.CATALOG_SEED_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(food_admin, "CATALOG_SEED_PATH", copy)
    return copy


def catalog_payload(**overrides):
    payload = {
        "name": "Amul dahi",
        "base_unit": "g",
        "basis_amount": 100,
        "calories": 60,
        "protein_g": 3.1,
        "carbs_g": 4.7,
        "fat_g": 3.0,
    }
    payload.update(overrides)
    return payload


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Amul dahi", "amul-dahi"),
            ("Roti / chapati", "roti-chapati"),
            ("  Protein Chef bread  ", "protein-chef-bread"),
            ("Café latte", "cafe-latte"),
            ("Egg", "egg"),
        ],
    )
    def test_it_matches_the_ids_already_in_the_file(self, name, expected):
        assert food_admin.slugify(name) == expected

    def test_a_name_with_no_usable_characters_is_caught(self):
        with pytest.raises(AdminError, match="no letters or numbers"):
            food_admin.parse_catalog_payload(catalog_payload(name="!!!"), [])


class TestCatalogValidation:
    def test_it_accepts_a_complete_food(self, seed_file):
        spec = food_admin.parse_catalog_payload(catalog_payload(), [])
        assert spec.food_id == "amul-dahi"
        assert spec.name == "Amul dahi"
        assert spec.calories == 60

    @pytest.mark.parametrize("field", ["calories", "protein_g", "carbs_g", "fat_g"])
    def test_all_four_nutrients_are_required(self, field):
        """A catalog row is a definition everyone inherits — no blanks, ever."""
        payload = catalog_payload()
        del payload[field]
        with pytest.raises(AdminError, match="must be a number"):
            food_admin.parse_catalog_payload(payload, [])

    def test_a_duplicate_id_is_refused(self):
        """The id is the row's identity across revisions; seed_catalog keys on it."""
        with pytest.raises(AdminError, match="already has an entry"):
            food_admin.parse_catalog_payload(catalog_payload(), ["amul-dahi"])

    def test_aliases_arrive_as_a_comma_separated_string(self):
        spec = food_admin.parse_catalog_payload(
            catalog_payload(aliases="dahi, curd , dahi"), []
        )
        assert spec.aliases == ("dahi", "curd")

    def test_portions_use_the_same_rules_as_a_private_food(self):
        with pytest.raises(AdminError, match="duplicates this food's own base unit"):
            food_admin.parse_catalog_payload(
                catalog_payload(portions=[{"name": "g", "amount": 50}]), []
            )

    def test_an_explicit_id_is_slugified_too(self):
        spec = food_admin.parse_catalog_payload(
            catalog_payload(food_id="Amul Dahi Plain"), []
        )
        assert spec.food_id == "amul-dahi-plain"


class TestRendering:
    def test_a_minimal_entry_is_valid_python(self):
        spec = food_admin.parse_catalog_payload(catalog_payload(), [])
        rendered = food_admin.render_catalog_entry(spec)
        # Parses as a call inside a list, which is how it lands in the file.
        ast.parse(f"x = [\n{rendered}]")

    def test_whole_numbers_lose_their_trailing_zero(self):
        spec = food_admin.parse_catalog_payload(catalog_payload(), [])
        rendered = food_admin.render_catalog_entry(spec)
        assert "100," in rendered and "100.0," not in rendered

    def test_portions_and_aliases_render(self):
        spec = food_admin.parse_catalog_payload(
            catalog_payload(
                category="dairy",
                aliases="dahi",
                portions=[{"name": "katori", "amount": 150}],
            ),
            [],
        )
        rendered = food_admin.render_catalog_entry(spec)
        ast.parse(f"x = [\n{rendered}]")
        assert '"katori"' in rendered and "150" in rendered
        assert '"dahi"' in rendered and '"dairy"' in rendered
        # Double quotes throughout, so a generated line does not stand out in
        # the diff from every hand-written line around it.
        assert "'" not in rendered


class TestRevisionBump:
    @pytest.mark.parametrize(
        ("current", "expected"),
        [("2026.3", "2026.4"), ("2026.9", "2026.10"), ("2027.0", "2027.1")],
    )
    def test_it_advances_the_minor(self, current, expected):
        assert food_admin.bump_revision(current) == expected

    def test_an_unexpected_shape_still_advances(self):
        assert food_admin.bump_revision("draft") == "draft.1"


class TestAppending:
    def test_the_food_lands_in_the_file(self, seed_file):
        before_revision, before = food_admin.load_catalog_seed()
        spec = food_admin.parse_catalog_payload(catalog_payload(), [])

        new_revision = food_admin.append_catalog_food(spec)

        revision, after = food_admin.load_catalog_seed()
        assert revision == new_revision != before_revision
        assert len(after) == len(before) + 1
        added = next(f for f in after if f["provider_food_id"] == "amul-dahi")
        assert added["display_name"] == "Amul dahi"
        assert added["calories"] == 60

    def test_existing_foods_and_their_comments_survive(self, seed_file):
        _revision, before = food_admin.load_catalog_seed()
        spec = food_admin.parse_catalog_payload(catalog_payload(), [])

        food_admin.append_catalog_food(spec)

        text = seed_file.read_text(encoding="utf-8")
        _revision, after = food_admin.load_catalog_seed()
        assert [f["provider_food_id"] for f in before] == [
            f["provider_food_id"] for f in after[:-1]
        ]
        # The comments explaining the odd rows are the ones most likely to be
        # misread later, so regenerating the file wholesale is not an option.
        assert "only measure the tub gives" in text

    def test_portions_round_trip_through_the_file(self, seed_file):
        spec = food_admin.parse_catalog_payload(
            catalog_payload(portions=[{"name": "katori", "amount": 150}]), []
        )
        food_admin.append_catalog_food(spec)

        _revision, foods = food_admin.load_catalog_seed()
        added = next(f for f in foods if f["provider_food_id"] == "amul-dahi")
        assert added["portions"] == [{"name": "katori", "base_amount": 150}]

    def test_two_foods_can_be_added_in_a_row(self, seed_file):
        # Measured from wherever the bundled catalog happens to stand: pinning
        # the literal made every real catalog addition fail this test, which
        # says nothing about whether two appends in a row work.
        start, _foods = food_admin.load_catalog_seed()
        expected = food_admin.bump_revision(food_admin.bump_revision(start))
        food_admin.append_catalog_food(
            food_admin.parse_catalog_payload(catalog_payload(), [])
        )
        _revision, foods = food_admin.load_catalog_seed()
        food_admin.append_catalog_food(
            food_admin.parse_catalog_payload(
                catalog_payload(name="Amul butter", calories=717,
                                protein_g=0.9, carbs_g=0.5, fat_g=81),
                [str(f["provider_food_id"]) for f in foods],
            )
        )
        revision, foods = food_admin.load_catalog_seed()
        assert revision == expected
        assert {"amul-dahi", "amul-butter"} <= {
            str(f["provider_food_id"]) for f in foods
        }

    def test_a_broken_edit_is_rolled_back(self, seed_file, monkeypatch):
        """A seed file that does not parse would stop the bot from starting."""
        original = seed_file.read_text(encoding="utf-8")
        monkeypatch.setattr(
            food_admin, "render_catalog_entry", lambda spec: "    _food(((,\n"
        )
        spec = food_admin.parse_catalog_payload(catalog_payload(), [])

        with pytest.raises(AdminError, match="rolled back"):
            food_admin.append_catalog_food(spec)

        assert seed_file.read_text(encoding="utf-8") == original
        food_admin.load_catalog_seed()  # still importable

    def test_the_real_seed_file_is_readable(self):
        """No monkeypatch: the shipped file must parse and be non-empty."""
        revision, foods = food_admin.load_catalog_seed()
        assert revision
        assert len(foods) >= 19
        assert all(f["provider_food_id"] for f in foods)


class TestCatalogOverHttp:
    def test_the_state_lists_the_catalog(self, server, seed_file):
        base, _db, _loop = server
        status, body = request(base, "/api/state")
        assert status == 200
        assert body["catalog_revision"]
        names = {f["name"] for f in body["catalog"]}
        assert "Egg" in names

    def test_adding_a_catalog_food_writes_the_file(self, server, seed_file):
        base, _db, _loop = server
        status, body = request(
            base, "/api/catalog", body={"catalog": catalog_payload()}
        )
        assert status == 200
        assert body["results"][0]["ok"] is True
        assert "amul-dahi" in seed_file.read_text(encoding="utf-8")
        # The reply carries fresh state, so the table updates without a reload.
        assert "Amul dahi" in {f["name"] for f in body["catalog"]}

    def test_the_reply_says_a_restart_is_needed(self, server, seed_file):
        """It is in the source file, not the database, until the bot restarts."""
        base, _db, _loop = server
        _status, body = request(
            base, "/api/catalog", body={"catalog": catalog_payload()}
        )
        notes = " ".join(body["results"][0]["notes"])
        assert "restart" in notes.lower()
        assert "revision" in notes.lower()

    def test_an_unseeded_food_is_marked_as_such(self, server, seed_file):
        base, _db, _loop = server
        _status, body = request(
            base, "/api/catalog", body={"catalog": catalog_payload()}
        )
        added = next(f for f in body["catalog"] if f["food_id"] == "amul-dahi")
        assert added["live"] is False

    def test_a_bad_catalog_payload_writes_nothing(self, server, seed_file):
        base, _db, _loop = server
        original = seed_file.read_text(encoding="utf-8")
        status, body = request(
            base,
            "/api/catalog",
            body={"catalog": catalog_payload(protein_g=None)},
        )
        assert status == 400
        assert "Protein g" in body["error"]
        assert seed_file.read_text(encoding="utf-8") == original


class TestPageAsset:
    def test_the_page_exists_next_to_the_module(self):
        assert food_admin.PAGE_PATH.is_file()

    def test_the_page_fetches_nothing_from_anywhere_else(self):
        """The server sends default-src 'none'; an external asset would break it."""
        page = food_admin.PAGE_PATH.read_text(encoding="utf-8")
        for marker in ("http://", "https://", "//cdn", "<link"):
            assert marker not in page.replace('xmlns="http://', ""), marker
