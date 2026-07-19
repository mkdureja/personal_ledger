"""
Shared test fixtures for Ledger bot tests.

Uses in-memory SQLite for fast, isolated tests.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

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
