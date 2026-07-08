from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

try:
    import fcntl

    def _try_lock(fileno: int) -> None:
        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fileno: int) -> None:
        fcntl.flock(fileno, fcntl.LOCK_UN)

except ImportError:
    import msvcrt

    def _try_lock(fileno: int) -> None:
        msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)

    def _unlock(fileno: int) -> None:
        try:
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

class AlreadyRunning(RuntimeError):
    pass

class SingleInstance:

    def __init__(self, lock_path: str | os.PathLike[str]) -> None:
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> "SingleInstance":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            _try_lock(fh.fileno())
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(
                f"another daemon already holds {self.lock_path} "
                f"(holder pid: {self._read_holder()}); refusing to start a second poller"
            ) from exc
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
