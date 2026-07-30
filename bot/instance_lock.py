"""An OS-level single-instance guard for the polling process.

The Definition of Done requires "exactly one supervised polling process". Nothing
enforced it: the ``post_init`` guard in :mod:`bot.main` (``if "db" in
application.bot_data``) is per-process state, so it only prevents duplicate
initialization *inside one* ``Application`` and says nothing across processes.

Two instances on one host is an easy accident — a Scheduled Task plus a manual
``python -m bot`` — with two concrete consequences:

* ``context.user_data`` is per-process in-memory, so a user's taps split across
  instances lose the guided draft. That is the same failure class the draft-loss
  fix was written to prevent.
* :mod:`bot.handlers.reminders` reads the delivered-chunk set once, then sends and
  records per chunk. Two instances both read "not delivered" and both send. The
  ``ON CONFLICT`` clause protects the *record* after the send; it cannot prevent
  the duplicate send.

The mechanism is an advisory byte-range lock on a file beside the database —
``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX. Both are held by the
*open file handle*, which means the operating system releases the lock when the
process dies for any reason, including a hard kill. There is deliberately no PID
file: ownership is the lock, not the file's existence, so there is no stale entry
to reap and no window where a recycled PID looks alive.

This module imports only the standard library — no configuration, no database, no
Telegram code — so the lock can be taken *before* anything else opens or migrates
the database.

Scope honestly: this prevents same-host concurrency. It does not address a
multi-host deployment, and it does not close the unavoidable send-then-record
crash window in reminder delivery.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType

#: Suffix appended to the resolved database path. ``.gitignore`` excludes it.
LOCK_SUFFIX = ".instance.lock"

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - platform-selected
    import msvcrt
else:  # pragma: no cover - platform-selected
    import fcntl


class InstanceLockError(RuntimeError):
    """The single-instance lock could not be acquired."""


class AlreadyRunningError(InstanceLockError):
    """Another process already holds the lock for this database."""


def lock_path_for(db_path: str | os.PathLike[str]) -> Path:
    """Return the lock file path for a database path.

    Derived from the *resolved* database path so two differently-spelled
    configurations pointing at one file still contend for the same lock. An
    in-memory database has no file to sit beside, so it gets a fixed name in the
    working directory; production always uses a real file.
    """
    raw = str(db_path)
    if raw == ":memory:":
        return Path.cwd() / f"ledger-memory{LOCK_SUFFIX}"
    resolved = Path(raw).expanduser().resolve()
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


class SingleInstanceLock:
    """Hold an exclusive OS lock for as long as this object holds its handle.

    Usable as a context manager, or acquired and released explicitly when the
    lifetime spans a long-running call such as ``run_polling()``.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    # -- acquisition ------------------------------------------------------
    def acquire(self) -> None:
        """Take the lock, or raise :class:`AlreadyRunningError`.

        The lock file is created if absent and never deleted on release: an
        unlocked leftover file is harmless, whereas deleting it would race a
        second process that has already opened it.
        """
        if self._fd is not None:
            raise InstanceLockError("this lock is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_exclusive_nonblocking(fd)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(self._contention_message()) from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Unlock and close the handle. Safe to call when not held."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _contention_message(self) -> str:
        return (
            f"Another Ledger process already holds {self.path}. Exactly one "
            "polling process may run per database: two would split guided drafts "
            "held in per-process memory and could send the same reminder twice. "
            "Stop the supervised service (systemd unit, NSSM service, or Scheduled "
            "Task) before starting a manual run, then try again. The lock is held "
            "by the running process's open handle, so the operating system frees "
            "it automatically if that process dies -- the file itself is not a "
            "stale marker to delete."
        )

    # -- context manager --------------------------------------------------
    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _lock_exclusive_nonblocking(fd: int) -> None:
    """Exclusive, non-blocking lock on the first byte; raises OSError if taken."""
    if _IS_WINDOWS:  # pragma: no cover - platform-selected
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - platform-selected
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if _IS_WINDOWS:  # pragma: no cover - platform-selected
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            # Closing the handle releases the lock regardless; a failed explicit
            # unlock must not mask the caller's own error during shutdown.
            pass
    else:  # pragma: no cover - platform-selected
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
