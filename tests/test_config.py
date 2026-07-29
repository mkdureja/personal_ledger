"""Tests for bot configuration."""

import importlib
import os
from pathlib import Path
from unittest import mock

import pytest

from bot.config import _parse_allowed_user_ids


@pytest.fixture(autouse=True)
def _restore_config():
    """Reload bot.config with the real (conftest) env after each test.

    Several tests reload bot.config under a mocked, sometimes-invalid environment
    and expect it to raise. Restoring afterwards guarantees a reload-that-raised
    never leaves bot.config broken for other test modules.
    """
    yield
    import bot.config

    importlib.reload(bot.config)


def test_db_path_default():
    """Test that DB_PATH defaults to a path relative to the project root."""
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1"}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        # Reload the config module to pick up the mocked environment
        import bot.config
        importlib.reload(bot.config)
        
        expected_path = Path(bot.config.__file__).resolve().parent.parent / "ledger.db"
        assert Path(bot.config.DB_PATH) == expected_path


def test_reminder_hour_valid():
    """Test that a valid REMINDER_HOUR is accepted."""
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1", "REMINDER_HOUR": "10"}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        import bot.config
        importlib.reload(bot.config)
        assert bot.config.REMINDER_HOUR == 10
        assert bot.config.REMINDER_TIME.hour == 10


@pytest.mark.parametrize("invalid_hour", ["24", "-1", "abc", ""])
def test_reminder_hour_invalid(invalid_hour):
    """Test that invalid REMINDER_HOUR raises RuntimeError."""
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1", "REMINDER_HOUR": invalid_hour}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        import bot.config
        with pytest.raises(RuntimeError):
            importlib.reload(bot.config)


def test_relative_paths_resolve_under_project_root():
    """Relative DB_PATH/ROUTINE_PATH values are anchored to the project root."""
    from bot.config import _PROJECT_ROOT, _resolve_under_root

    assert Path(_resolve_under_root("ledger.db")) == _PROJECT_ROOT / "ledger.db"
    assert Path(_resolve_under_root("data/x.yaml")) == _PROJECT_ROOT / "data" / "x.yaml"


def test_absolute_and_memory_paths_pass_through():
    from bot.config import _PROJECT_ROOT, _resolve_under_root

    absolute = str(_PROJECT_ROOT / "somewhere" / "ledger.db")
    assert _resolve_under_root(absolute) == absolute
    assert _resolve_under_root(":memory:") == ":memory:"


# ---------------------------------------------------------------------------
# Allowlist parsing hardening
# ---------------------------------------------------------------------------
def test_allowlist_parses_multiple_ids():
    assert _parse_allowed_user_ids("111, 222,333") == frozenset({111, 222, 333})


def test_allowlist_rejects_non_integer():
    with pytest.raises(RuntimeError, match="not an integer"):
        _parse_allowed_user_ids("111, abc")


@pytest.mark.parametrize("raw", ["0", "-5", "111, 0"])
def test_allowlist_rejects_non_positive(raw):
    with pytest.raises(RuntimeError, match="positive"):
        _parse_allowed_user_ids(raw)


def test_allowlist_rejects_duplicates():
    with pytest.raises(RuntimeError, match="duplicate"):
        _parse_allowed_user_ids("111, 222, 111")


@pytest.mark.parametrize("raw", ["", "  ", " , "])
def test_allowlist_rejects_empty(raw):
    with pytest.raises(RuntimeError, match="not set"):
        _parse_allowed_user_ids(raw)


def test_allowlist_duplicate_error_does_not_echo_id():
    """The duplicate diagnostic reports a count, never a real Telegram ID."""
    with pytest.raises(RuntimeError) as exc_info:
        _parse_allowed_user_ids("555000111, 555000111")
    assert "555000111" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Phase 1 rollout flags (plan §4.1)
# ---------------------------------------------------------------------------
def _reload_with(env: dict[str, str]):
    base = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1,2,3"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=True), mock.patch(
        "dotenv.load_dotenv"
    ):
        import bot.config

        importlib.reload(bot.config)
        return bot.config


@pytest.mark.parametrize("mode", ["off", "pilot", "on", "remove"])
def test_home_keyboard_mode_accepts_valid(mode):
    # pilot mode needs a non-empty pilot list to be meaningful, but empty is
    # still a valid (if inert) configuration.
    cfg = _reload_with({"HOME_KEYBOARD_MODE": mode, "PHASE1_ENABLED_USER_IDS": "1"})
    assert cfg.HOME_KEYBOARD_MODE == mode


@pytest.mark.parametrize("mode", ["legacy", "prod", "ON", "", "enabled"])
def test_home_keyboard_mode_rejects_unknown(mode):
    with pytest.raises(RuntimeError, match="HOME_KEYBOARD_MODE"):
        _reload_with({"HOME_KEYBOARD_MODE": mode})


def test_phase1_must_be_subset_of_allowed():
    with pytest.raises(RuntimeError, match="subset of ALLOWED_USER_IDS"):
        _reload_with({"PHASE1_ENABLED_USER_IDS": "1,9"})


def test_phase1_defaults_empty():
    cfg = _reload_with({})
    assert cfg.PHASE1_ENABLED_USER_IDS == frozenset()
    assert cfg.HOME_KEYBOARD_MODE == "off"
    assert cfg.phase1_enabled_for(1) is False


def test_pilot_must_be_subset_of_phase1():
    with pytest.raises(RuntimeError, match="subset of\\s+PHASE1_ENABLED_USER_IDS"):
        _reload_with(
            {
                "HOME_KEYBOARD_MODE": "pilot",
                "PHASE1_ENABLED_USER_IDS": "1",
                "HOME_KEYBOARD_PILOT_USER_IDS": "2",
            }
        )


def test_phase1_enabled_for():
    cfg = _reload_with({"PHASE1_ENABLED_USER_IDS": "1,2"})
    assert cfg.phase1_enabled_for(1) is True
    assert cfg.phase1_enabled_for(2) is True
    assert cfg.phase1_enabled_for(3) is False


def test_keyboard_action_off_and_remove_always_remove():
    for mode in ("off", "remove"):
        cfg = _reload_with(
            {"HOME_KEYBOARD_MODE": mode, "PHASE1_ENABLED_USER_IDS": "1,2"}
        )
        assert cfg.home_keyboard_action_for(1) == "remove"
        assert cfg.home_keyboard_action_for(2) == "remove"


def test_keyboard_action_pilot_sends_only_to_pilot_users():
    cfg = _reload_with(
        {
            "HOME_KEYBOARD_MODE": "pilot",
            "PHASE1_ENABLED_USER_IDS": "1,2",
            "HOME_KEYBOARD_PILOT_USER_IDS": "1",
        }
    )
    assert cfg.home_keyboard_action_for(1) == "send"
    assert cfg.home_keyboard_action_for(2) == "remove"
    assert cfg.home_keyboard_action_for(3) == "remove"


def test_keyboard_action_on_sends_to_all_phase1_users():
    cfg = _reload_with(
        {"HOME_KEYBOARD_MODE": "on", "PHASE1_ENABLED_USER_IDS": "1,2"}
    )
    assert cfg.home_keyboard_action_for(1) == "send"
    assert cfg.home_keyboard_action_for(2) == "send"
    assert cfg.home_keyboard_action_for(3) == "remove"


# ---------------------------------------------------------------------------
# Timezone validation
# ---------------------------------------------------------------------------
def test_invalid_tz_raises_actionable_error():
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1", "TZ": "Not/AZone"}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        import bot.config

        with pytest.raises(RuntimeError, match="valid IANA timezone"):
            importlib.reload(bot.config)


def test_valid_tz_is_accepted():
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1", "TZ": "America/New_York"}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        import bot.config

        importlib.reload(bot.config)
        assert str(bot.config.LOCAL_TZ) == "America/New_York"
