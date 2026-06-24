"""Single-instance guard: refuse to start a second daemon against the same DB.

Why this exists
---------------
Each daemon keeps its **own** in-memory `MonitorEngine`/FSM and only shares the
SQLite file. Run two of them and both independently count a host down and both
emit `OutageOpened` for the same real outage — so you get duplicate `outages`
rows (a few seconds apart, as their poll phases differ) and double pages. The
engine is correct; the invariant it assumes is "one logical poller per DB".

We enforce that invariant with an **OS advisory lock** on a file next to the DB.
A second process can't take the lock and exits. The lock is held by the open
file handle, so the kernel releases it automatically when the process dies —
even on a crash or `kill -9` — meaning there is no stale pidfile to reap.

Cross-platform: `fcntl.flock` on POSIX, `msvcrt.locking` on Windows.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

# --- platform-specific non-blocking exclusive lock on an open file ----------
try:
    import fcntl

    def _try_lock(fileno: int) -> None:
        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fileno: int) -> None:
        fcntl.flock(fileno, fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - Windows fallback
    import msvcrt

    def _try_lock(fileno: int) -> None:
        msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)

    def _unlock(fileno: int) -> None:
        try:
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


class AlreadyRunning(RuntimeError):
    """Raised when another process already holds the instance lock."""


class SingleInstance:
    """Hold an exclusive advisory lock for the lifetime of the process.

    Use as a context manager (releases on exit) or call `acquire()`/`release()`
    directly. `acquire()` raises `AlreadyRunning` if another live process holds
    the lock; the held handle is kept open until `release()` so the lock stays.
    """

    def __init__(self, lock_path: str | os.PathLike[str]) -> None:
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> "SingleInstance":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Open (not truncating) so a crash leaves the previous pid readable until
        # we overwrite it; the lock — not the file contents — is the source of truth.
        fh = open(self.lock_path, "a+")
        try:
            _try_lock(fh.fileno())
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(
                f"another daemon already holds {self.lock_path} "
                f"(holder pid: {self._read_holder()}); refusing to start a second poller"
            ) from exc
        # We own it: record our pid for humans (`cat wisp.db.lock`); the lock persists.
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            try:
                _unlock(self._fh.fileno())
            finally:
                self._fh.close()
                self._fh = None

    def _read_holder(self) -> str:
        try:
            return self.lock_path.read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def __enter__(self) -> "SingleInstance":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
