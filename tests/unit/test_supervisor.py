"""Edge supervisor tests (Phase 10 Part D): the verify gate and the
swap→restart→health-gate→rollback state machine, driven with temp files + injected IO
(download / restart / health / clock). No real binaries, network, or systemd."""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.runtime.supervisor import (
    Supervisor, needs_update, verify_sha256, UPDATED, SKIPPED, VERIFY_FAILED, ROLLED_BACK,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SupervisorPureTest(unittest.TestCase):
    def test_needs_update(self):
        self.assertTrue(needs_update("v1", "v2"))
        self.assertFalse(needs_update("v2", "v2"))
        self.assertFalse(needs_update("v1", None))
        self.assertFalse(needs_update("v1", ""))

    def test_verify_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"binary-bytes"); path = Path(f.name)
        try:
            self.assertTrue(verify_sha256(path, _sha(b"binary-bytes")))
            self.assertFalse(verify_sha256(path, _sha(b"other")))
            self.assertFalse(verify_sha256(path, ""))
        finally:
            path.unlink()


class SupervisorApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.agent = self.d / "agent"
        self.backup = self.d / "agent.bak"
        self.agent.write_bytes(b"OLD-v1")
        self.restarts = []
        self.t = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def _clock(self):
        return self.t

    def _sleep(self, _):
        self.t += 5.0   # advance virtual time instead of really sleeping

    def _make(self, *, new_bytes, current="v1", health_seq):
        """Build a Supervisor whose download writes new_bytes, with a scripted health sequence."""
        artifact = self.d / "download.bin"

        def download(url):
            artifact.write_bytes(new_bytes)
            return artifact

        seq = list(health_seq)

        def health_ok():
            return seq.pop(0) if seq else False

        return Supervisor(agent_path=self.agent, backup_path=self.backup, download=download,
                          restart=lambda: self.restarts.append(self._cur()),
                          health_ok=health_ok, current_version=lambda: current,
                          clock=self._clock, sleep=self._sleep, deadline_s=30, poll_s=5)

    def _cur(self):
        return self.agent.read_bytes()

    def test_happy_path_swaps_and_keeps(self):
        sup = self._make(new_bytes=b"NEW-v2", health_seq=[True])
        out = sup.apply({"target_version": "v2", "url": "u", "sha256": _sha(b"NEW-v2")})
        self.assertEqual(out, UPDATED)
        self.assertEqual(self.agent.read_bytes(), b"NEW-v2")
        self.assertEqual(self.restarts, [b"NEW-v2"])          # restarted once, on the new binary

    def test_verify_failure_never_swaps(self):
        sup = self._make(new_bytes=b"NEW-v2", health_seq=[True])
        out = sup.apply({"target_version": "v2", "url": "u", "sha256": _sha(b"WRONG")})
        self.assertEqual(out, VERIFY_FAILED)
        self.assertEqual(self.agent.read_bytes(), b"OLD-v1")  # untouched
        self.assertEqual(self.restarts, [])

    def test_unhealthy_rolls_back(self):
        sup = self._make(new_bytes=b"NEW-v2", health_seq=[False, False, False, False, False, False, False])
        out = sup.apply({"target_version": "v2", "url": "u", "sha256": _sha(b"NEW-v2")})
        self.assertEqual(out, ROLLED_BACK)
        self.assertEqual(self.agent.read_bytes(), b"OLD-v1")  # restored last-known-good
        # restarted twice: once onto the new binary, once back onto the old after rollback
        self.assertEqual(self.restarts, [b"NEW-v2", b"OLD-v1"])

    def test_skips_when_already_on_target(self):
        sup = self._make(new_bytes=b"NEW-v2", current="v2", health_seq=[True])
        out = sup.apply({"target_version": "v2", "url": "u", "sha256": _sha(b"NEW-v2")})
        self.assertEqual(out, SKIPPED)
        self.assertEqual(self.agent.read_bytes(), b"OLD-v1")  # nothing swapped

    def test_consume_request_file(self):
        sup = self._make(new_bytes=b"NEW-v2", health_seq=[True])
        req = self.d / "update_request.json"
        import json
        req.write_text(json.dumps({"target_version": "v2", "url": "u", "sha256": _sha(b"NEW-v2")}))
        out = sup.consume_request(req)
        self.assertEqual(out, UPDATED)
        self.assertFalse(req.exists())                         # cleared so it isn't retried
        self.assertIsNone(sup.consume_request(req))            # no request -> None


if __name__ == "__main__":
    unittest.main()
