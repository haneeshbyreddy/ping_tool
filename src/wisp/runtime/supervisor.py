"""Edge supervisor — owns the agent binary's self-update (Phase 10 Part D).

The OS service runs the *supervisor*; the supervisor launches/monitors the *agent* (today's
daemon, frozen) and owns **download → verify → atomic-swap → restart → health-gate → rollback**.
This solves "how does a binary update itself while running" — the updater is not the thing being
updated, so it changes rarely while agent updates are the common path. The agent learns the
target from its heartbeat reply and drops an `update_request.json`; the supervisor consumes it.

Only safe transitions:
  * an unverified artifact (sha256 mismatch) is **never** swapped in;
  * the current binary is backed up to last-known-good *before* the swap;
  * after restart the new agent must prove healthy (its `preflight()` + a fresh heartbeat)
    within the deadline, or the supervisor **rolls back** to last-known-good and restarts.

The decision logic + the swap/rollback state machine live here behind injected IO (download /
restart / health-check / clock), so they unit-test with temp files and fakes — no real binary,
network, or systemd in the suite. The real wiring (httpx download, `systemctl restart`) lives in
the thin `apps/supervisor` entrypoint, which needs a real host to exercise.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("wisp.supervisor")

# apply() outcomes
UPDATED = "updated"
SKIPPED = "skipped"
VERIFY_FAILED = "verify_failed"
ROLLED_BACK = "rolled_back"


def needs_update(current: str | None, target: str | None) -> bool:
    """Central is the authority on the target; we pull on any difference (it never asks for a
    version we already run). A missing/empty target is a no-op."""
    return bool(target) and current != target


def verify_sha256(path: Path, expected: str) -> bool:
    """Constant-time check of the artifact digest. An unverified binary is never swapped in —
    this is the supply-chain gate (the published sha256 is signed alongside the artifact)."""
    if not expected:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return hmac.compare_digest(h.hexdigest(), expected.lower())


class Supervisor:
    def __init__(self, *, agent_path: Path, backup_path: Path, download, restart,
                 health_ok, current_version, clock=time.monotonic, sleep=time.sleep,
                 deadline_s: int = 300, poll_s: float = 5.0) -> None:
        self.agent_path = Path(agent_path)
        self.backup_path = Path(backup_path)
        self._download = download          # (url) -> Path of the fetched artifact
        self._restart = restart            # () -> None: restart the agent process
        self._health_ok = health_ok        # () -> bool: agent preflight + fresh heartbeat
        self._current_version = current_version  # () -> str
        self._clock = clock
        self._sleep = sleep
        self.deadline_s = deadline_s
        self.poll_s = poll_s

    def apply(self, directive: dict) -> str:
        """Run one update to `directive['target_version']` (url + sha256). Returns the outcome
        constant. Idempotent: a directive for the running version is SKIPPED."""
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

        # Back up last-known-good BEFORE the swap so rollback is always possible.
        if self.agent_path.exists():
            shutil.copy2(self.agent_path, self.backup_path)
        os.replace(artifact, self.agent_path)   # atomic on the same filesystem
        try:
            os.chmod(self.agent_path, 0o755)
        except OSError:
            pass
        log.info("swapped in agent %s; restarting + health-gating", target)
        self._restart()

        if self._await_health():
            return UPDATED

        # New agent never came back healthy in time -> roll back to last-known-good.
        log.error("update to %s failed health gate — rolling back", target)
        if self.backup_path.exists():
            os.replace(self.backup_path, self.agent_path)
        self._restart()
        return ROLLED_BACK

    def _await_health(self) -> bool:
        deadline = self._clock() + self.deadline_s
        while self._clock() < deadline:
            if self._health_ok():
                return True
            self._sleep(self.poll_s)
        return self._health_ok()  # one last check at the deadline

    def consume_request(self, request_path: Path) -> str | None:
        """Apply a pending update_request.json (written by the agent's shipper), then clear it.
        Returns the outcome, or None if there was no request. The request is removed on a
        terminal outcome so a poison directive isn't retried forever."""
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
        outcome = self.apply(directive)
        request_path.unlink(missing_ok=True)
        return outcome
