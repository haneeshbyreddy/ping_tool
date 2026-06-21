"""Channel test-send (services.test_channel).

Temp DB + a recording notifier injected in place of the real ntfy sender, so the
go-live "did my channel work?" check is exercised without network. Channels are
role-based now (owner / operator / tech ntfy topics) — there is no per-person
routing key — so the test targets a role and asserts its configured topic.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import migrate
from wisp.server import services
from wisp.egress import notifiers


class _Recording:
    """Stand-in for the real ntfy notifier: records the send, no network."""
    channel = "ntfy"

    def __init__(self):
        self.sent = []

    def send(self, recipient, title, body, priority):
        self.sent.append({"recipient": recipient, "title": title})
        return notifiers.NotifyResult(True)


class ChannelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        # Swap the real ntfy sender for a recorder so test_channel never hits the net.
        self._orig_notifier = notifiers.build_notifier
        notifiers.build_notifier = lambda cfg=None: _Recording()

    def tearDown(self):
        notifiers.build_notifier = self._orig_notifier
        self.tmp.cleanup()

    def test_each_role_targets_its_topic(self):
        for role, topic in (("owner", self.cfg.ntfy_topic_owner),
                            ("operator", self.cfg.ntfy_topic_operator),
                            ("tech", self.cfg.ntfy_topic_tech)):
            res = services.test_channel(role, self.cfg)
            self.assertTrue(res["ok"])
            self.assertEqual(res["channel"], "ntfy")
            self.assertEqual(res["recipient"], topic)
            self.assertEqual(res["role"], role)

    def test_invalid_channel_rejected(self):
        with self.assertRaises(services.WorkerError):
            services.test_channel("nobody", self.cfg)

    def test_send_failure_surfaced(self):
        class _Failing:
            channel = "ntfy"
            def send(self, *a, **k):
                return notifiers.NotifyResult(False, "send failed: network down")

        notifiers.build_notifier = lambda cfg=None: _Failing()
        res = services.test_channel("tech", self.cfg)
        self.assertFalse(res["ok"])
        self.assertIn("network down", res["detail"])


if __name__ == "__main__":
    unittest.main()
