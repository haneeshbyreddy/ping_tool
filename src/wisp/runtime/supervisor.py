from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import time
from pathlib import Path

from wisp.version import is_newer

log = logging.getLogger("wisp.supervisor")

UPDATED = "updated"
SKIPPED = "skipped"
VERIFY_FAILED = "verify_failed"
ROLLED_BACK = "rolled_back"
FAILED = "failed"

def needs_update(current: str | None, target: str | None) -> bool:
    return bool(target) and is_newer(target, current)

def verify_sha256(path: Path, expected: str) -> bool:
    if not expected:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return hmac.compare_digest(h.hexdigest(), expected.lower())

class Supervisor:
    def __init__(self, *, agent_path: Path, backup_path: Path, download, restart,
                 health_ok, current_version, stop=lambda: None,
                 clock=time.monotonic, sleep=time.sleep,
                 deadline_s: int = 300, poll_s: float = 5.0, stable_polls: int = 3) -> None:
        self.agent_path = Path(agent_path)
        self.backup_path = Path(backup_path)
        self._download = download
        self._restart = restart
        self._stop = stop
        self._health_ok = health_ok
        self._current_version = current_version
        self._clock = clock
        self._sleep = sleep
        self.deadline_s = deadline_s
        self.poll_s = poll_s
        self.stable_polls = stable_polls

    def apply(self, directive: dict) -> str:
        target = directive.get("target_version")
        if not needs_update(self._current_version(), target):
            return SKIPPED

        artifact = Path(self._download(directive["url"]))
        if not verify_sha256(artifact, directive.get("sha256", "")):
            log.error("update to %s: sha256 mismatch — refusing to swap", target)
            try:
                artifact.unlink()
            except OSError:
                pass
            return VERIFY_FAILED

        if self.agent_path.exists():
            shutil.copy2(self.agent_path, self.backup_path)
        self._stop()
        os.replace(artifact, self.agent_path)
        try:
            os.chmod(self.agent_path, 0o755)
        except OSError:
            pass
        log.info("swapped in agent %s; restarting + health-gating", target)
        self._restart()

        if self._await_health():
            return UPDATED

        log.error("update to %s failed health gate — rolling back", target)
        self._stop()
        if self.backup_path.exists():
            os.replace(self.backup_path, self.agent_path)
        self._restart()
        return ROLLED_BACK

    def _await_health(self) -> bool:
        deadline = self._clock() + self.deadline_s
        streak = 0
        while self._clock() < deadline:
            self._sleep(self.poll_s)
            if self._health_ok():
                streak += 1
                if streak >= self.stable_polls:
                    return True
            else:
                streak = 0
        return False

    def consume_request(self, request_path: Path) -> str | None:
        import json
        request_path = Path(request_path)
        if not request_path.is_file():
            return None
        try:
            directive = json.loads(request_path.read_text())
        except Exception:
            log.exception("bad update_request.json; discarding")
            request_path.unlink(missing_ok=True)
            return None
        try:
            outcome = self.apply(directive)
        except Exception:
            log.exception("update to %s failed mid-apply; discarding request "
                          "(the next heartbeat re-issues it)",
                          directive.get("target_version"))
            outcome = FAILED
        request_path.unlink(missing_ok=True)
        return outcome
