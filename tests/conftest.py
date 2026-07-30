"""
Shared test fixtures for Ledger bot tests.

Uses in-memory SQLite for fast, isolated tests.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

# Keep tests independent from a developer's real .env and usable in CI.
#
# bot.config calls load_dotenv() without override, so anything already in the
# environment wins but anything *absent* is read from .env. Every setting the
# config validates must therefore be pinned here — otherwise a real .env leaks
# in. That bit us once: enabling Phase 1 for two real user IDs locally made the
# whole suite fail collection, because those IDs are not a subset of the test
# ALLOWED_USER_IDS. Tests that need Phase 1 on monkeypatch bot.config directly.
os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN"
os.environ["ALLOWED_USER_IDS"] = "123456789"
os.environ["TZ"] = "Asia/Kolkata"
os.environ["PHASE1_ENABLED_USER_IDS"] = ""
os.environ["HOME_KEYBOARD_MODE"] = "off"
os.environ["HOME_KEYBOARD_PILOT_USER_IDS"] = ""

from bot.database import DatabaseManager


@pytest_asyncio.fixture
async def db():
    """Create an in-memory database for testing."""
    manager = DatabaseManager(":memory:")
    await manager.connect()
    await manager.init_db()
    yield manager
    await manager.close()


@pytest.fixture
def user_id():
    """Default test user ID."""
    return 123456789


@pytest_asyncio.fixture
async def db_with_user(db, user_id):
    """Database with a registered user."""
    await db.ensure_user(user_id, "testuser", "Test")
    return db
