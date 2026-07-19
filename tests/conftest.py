"""
Shared test fixtures for Ledger bot tests.

Uses in-memory SQLite for fast, isolated tests.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

# Keep tests independent from a developer's real .env and usable in CI.
os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN"
os.environ["ALLOWED_USER_IDS"] = "123456789"
os.environ["TZ"] = "Asia/Kolkata"

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
