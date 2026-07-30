"""Real-process tests for the single-instance guard (plan §0.4).

A safety mechanism that is only ever exercised against a mock proves nothing
about the operating system primitive underneath it, so contention, normal
release, and crash release all run in genuine subprocesses. The child scripts
import :mod:`bot.instance_lock` alone, which is standard-library-only — no bot
token, database, or event loop involved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bot import main as main_module
from bot.instance_lock import (
    LOCK_SUFFIX,
    AlreadyRunningError,
    InstanceLockError,
    SingleInstanceLock,
    lock_path_for,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Child A: acquire, announce, then block until the parent closes stdin.
_HOLD_SCRIPT = """
import sys
from bot.instance_lock import SingleInstanceLock
lock = SingleInstanceLock(sys.argv[1])
lock.acquire()
print("ACQUIRED", flush=True)
sys.stdin.readline()
lock.release()
print("RELEASED", flush=True)
"""

# Child B: try to acquire once and report the outcome.
_TRY_SCRIPT = """
import sys
from bot.instance_lock import AlreadyRunningError, SingleInstanceLock
lock = SingleInstanceLock(sys.argv[1])
try:
    lock.acquire()
except AlreadyRunningError as exc:
    print(f"REFUSED {exc}", flush=True)
    raise SystemExit(3)
print("ACQUIRED", flush=True)
lock.release()
"""

# Child C: acquire, then die without unwinding — the crash case.
_CRASH_SCRIPT = """
import os, sys
from bot.instance_lock import SingleInstanceLock
lock = SingleInstanceLock(sys.argv[1])
lock.acquire()
print("ACQUIRED", flush=True)
sys.stdout.flush()
os._exit(1)
"""


def _spawn(script: str, lock_file: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, str(lock_file)],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_line(process: subprocess.Popen, expected: str, timeout: float = 20.0) -> str:
    """Read one stdout line, failing the test if the child never gets there."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line:
            assert expected in line, f"unexpected child output: {line!r}"
            return line
        if process.poll() is not None:
            pytest.fail(
                f"child exited early ({process.returncode}): {process.stderr.read()}"
            )
    pytest.fail(f"child never printed {expected!r}")


def _stop(process: subprocess.Popen, timeout: float = 30.0) -> int:
    """Let a holder child release its lock and exit; return its exit code."""
    if process.poll() is None:
        try:
            process.stdin.write("go\n")
            process.stdin.flush()
        except (OSError, ValueError):  # already gone
            pass
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - child wedged
        process.kill()
        return process.wait(timeout=timeout)


def _run(script: str, lock_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(lock_file)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Lock path derivation
# ---------------------------------------------------------------------------
def test_lock_path_sits_beside_the_database(tmp_path):
    db = tmp_path / "ledger.db"
    assert lock_path_for(db) == tmp_path.resolve() / f"ledger.db{LOCK_SUFFIX}"


def test_lock_path_is_stable_across_equivalent_spellings(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    db.touch()
    monkeypatch.chdir(tmp_path)
    assert lock_path_for("ledger.db") == lock_path_for(db)
    assert lock_path_for("./ledger.db") == lock_path_for(db)


def test_in_memory_database_gets_a_named_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert lock_path_for(":memory:").name == f"ledger-memory{LOCK_SUFFIX}"


# ---------------------------------------------------------------------------
# Real contention between processes
# ---------------------------------------------------------------------------
def test_second_process_is_refused_while_the_first_holds_the_lock(tmp_path):
    lock_file = tmp_path / f"ledger.db{LOCK_SUFFIX}"
    # Popen's context manager closes the pipes and waits, so no descriptor leaks.
    with _spawn(_HOLD_SCRIPT, lock_file) as holder:
        try:
            _await_line(holder, "ACQUIRED")

            second = _run(_TRY_SCRIPT, lock_file)

            assert second.returncode == 3, second.stderr
            assert "REFUSED" in second.stdout
            # The message must name the lock file and the likely cause.
            assert str(lock_file) in second.stdout
            assert "one polling process" in second.stdout
        finally:
            _stop(holder)


def test_lock_is_available_again_after_a_normal_release(tmp_path):
    lock_file = tmp_path / f"ledger.db{LOCK_SUFFIX}"
    with _spawn(_HOLD_SCRIPT, lock_file) as holder:
        _await_line(holder, "ACQUIRED")
        assert _stop(holder) == 0

    second = _run(_TRY_SCRIPT, lock_file)
    assert second.returncode == 0, second.stderr
    assert "ACQUIRED" in second.stdout


def test_lock_is_available_again_after_a_crash(tmp_path):
    """A hard kill leaves no stale marker: the OS drops the lock with the handle."""
    lock_file = tmp_path / f"ledger.db{LOCK_SUFFIX}"
    crasher = _run(_CRASH_SCRIPT, lock_file)
    assert crasher.returncode == 1
    assert "ACQUIRED" in crasher.stdout
    assert lock_file.exists()  # the file survives, and that is not ownership

    second = _run(_TRY_SCRIPT, lock_file)
    assert second.returncode == 0, second.stderr
    assert "ACQUIRED" in second.stdout


# ---------------------------------------------------------------------------
# In-process behavior
# ---------------------------------------------------------------------------
def test_a_second_handle_in_this_process_is_also_refused(tmp_path):
    lock_file = tmp_path / f"ledger.db{LOCK_SUFFIX}"
    with SingleInstanceLock(lock_file) as first:
        assert first.held
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(lock_file).acquire()
    assert not first.held
    # Released: the same path can be taken again.
    with SingleInstanceLock(lock_file):
        pass


def test_double_acquire_on_one_object_is_a_programming_error(tmp_path):
    lock = SingleInstanceLock(tmp_path / f"ledger.db{LOCK_SUFFIX}")
    lock.acquire()
    try:
        with pytest.raises(InstanceLockError):
            lock.acquire()
    finally:
        lock.release()


def test_release_is_idempotent(tmp_path):
    lock = SingleInstanceLock(tmp_path / f"ledger.db{LOCK_SUFFIX}")
    lock.release()  # never acquired
    lock.acquire()
    lock.release()
    lock.release()
    assert not lock.held


def test_lock_directory_is_created_on_demand(tmp_path):
    nested = tmp_path / "deep" / "dir" / f"ledger.db{LOCK_SUFFIX}"
    with SingleInstanceLock(nested):
        assert nested.exists()


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------
def test_main_exits_non_zero_without_touching_the_database(tmp_path, monkeypatch):
    """A refused start must not build the Application or open the database."""
    db_path = tmp_path / "ledger.db"
    monkeypatch.setattr(main_module, "DB_PATH", str(db_path))

    def _must_not_run():  # pragma: no cover - asserts it is never called
        raise AssertionError("build_application ran despite a held instance lock")

    monkeypatch.setattr(main_module, "build_application", _must_not_run)

    with SingleInstanceLock(lock_path_for(db_path)):
        with pytest.raises(SystemExit) as excinfo:
            main_module.main()

    assert excinfo.value.code == 1
    assert not db_path.exists()


def test_main_releases_the_lock_when_polling_ends(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger.db"
    monkeypatch.setattr(main_module, "DB_PATH", str(db_path))

    class _App:
        def run_polling(self, **_kwargs):
            # While polling, the lock must be held: a second handle is refused.
            with pytest.raises(AlreadyRunningError):
                SingleInstanceLock(lock_path_for(db_path)).acquire()

    monkeypatch.setattr(main_module, "build_application", lambda: _App())
    main_module.main()

    # Released on the way out, so a restart can take it immediately.
    with SingleInstanceLock(lock_path_for(db_path)):
        pass


def test_main_releases_the_lock_when_polling_raises(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger.db"
    monkeypatch.setattr(main_module, "DB_PATH", str(db_path))

    class _App:
        def run_polling(self, **_kwargs):
            raise RuntimeError("network gone")

    monkeypatch.setattr(main_module, "build_application", lambda: _App())
    with pytest.raises(RuntimeError):
        main_module.main()

    with SingleInstanceLock(lock_path_for(db_path)):
        pass


def test_startup_lock_precedes_database_work(tmp_path, monkeypatch):
    """Ordering guard: the lock is taken before the Application is built.

    ``post_init`` — which connects, runs the migration preflight, and migrates —
    only runs inside ``run_polling()``, so building after acquisition is what
    keeps every database path behind the lock.
    """
    db_path = tmp_path / "ledger.db"
    monkeypatch.setattr(main_module, "DB_PATH", str(db_path))
    order: list[str] = []

    class _App:
        def run_polling(self, **_kwargs):
            order.append("poll")

    def _build():
        order.append("build")
        assert lock_path_for(db_path).exists()
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(lock_path_for(db_path)).acquire()
        return _App()

    monkeypatch.setattr(main_module, "build_application", _build)
    main_module.main()
    assert order == ["build", "poll"]
