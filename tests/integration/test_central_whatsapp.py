import os
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import engine as central_engine
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.notify_policy import AlertRouter, flush_digests
from wisp.central.store import CentralStore
from wisp.core.state_machine import DOWN, OutageOpened, OutageResolved
from support import RecordingNotifier

T0 = "2026-01-01T00:00:00+00:00"


class OrgRoleWhatsappTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA")

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbers_by_role_active_only(self):
        o1 = self.store.add_user("ispA", "own1", "h", "s", "owner")
        o2 = self.store.add_user("ispA", "own2", "h", "s", "owner")
        w1 = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        no_num = self.store.add_user("ispA", "own3", "h", "s", "owner")  # noqa: F841
        off = self.store.add_user("ispA", "own4", "h", "s", "owner")
        self.store.set_user_whatsapp(o1, "919000000001")
        self.store.set_user_whatsapp(o2, "919000000002")
        self.store.set_user_whatsapp(w1, "919000000009")
        self.store.set_user_whatsapp(off, "919000000000")
        self.store.set_user_active(off, False)              # deactivated → excluded

        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"),
                         ["919000000001", "919000000002"])
        self.assertEqual(self.store.org_role_whatsapp("ispA", "worker"),
                         ["919000000009"])

    def test_empty_when_none_set(self):
        self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"), [])

    def test_clearing_a_number_removes_it(self):
        u = self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.store.set_user_whatsapp(u, "919000000001")
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"),
                         ["919000000001"])
        self.store.set_user_whatsapp(u, None)
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"), [])

    def test_scoped_to_org(self):
        self.store.set_org("ispB")
        a = self.store.add_user("ispA", "a1", "h", "s", "owner")
        b = self.store.add_user("ispB", "b1", "h", "s", "owner")
        self.store.set_user_whatsapp(a, "919000000001")
        self.store.set_user_whatsapp(b, "919000000002")
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"),
                         ["919000000001"])


class WhatsappSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_only_whatsapp_prefixed_keys_stripped(self):
        self.store.set_setting("whatsapp_enabled", "1")
        self.store.set_setting("whatsapp_token", "TOK")
        self.store.set_setting("google_maps_key", "AIza")   # must not leak in
        self.assertEqual(self.store.whatsapp_settings(),
                         {"enabled": "1", "token": "TOK"})


class DispatchFanoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.dev = self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.0.1", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner",
                           ntfy_topic_worker="a-worker")
        o = self.store.add_user("ispA", "own1", "h", "s", "owner")
        w = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(o, "919000000001")     # owner number
        self.store.set_user_whatsapp(w, "919000000009")     # worker number
        self.engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        self.notifier = RecordingNotifier()
        self.disp = CentralAlertDispatcher(self.store, "ispA", self.engine,
                                           self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _by_topic(self):
        return {s["recipient"]: s for s in self.notifier.sent}

    def test_device_down_fans_owner_and_worker_numbers(self):
        self.store.open_outage_if_absent("ispA", self.dev, T0, DOWN)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        sent = self._by_topic()
        # owner leg carries owner numbers, worker leg carries worker numbers —
        # each role's page reaches its own accounts, mirroring the ntfy topics
        self.assertEqual(sent["a-owner"]["whatsapp"], ["919000000001"])
        self.assertEqual(sent["a-worker"]["whatsapp"], ["919000000009"])
        # structured facts ride the ICMP page
        self.assertEqual(sent["a-owner"]["facts"].status, "DOWN")

    def test_resolve_broadcast_fans_both_roles(self):
        self.store.open_outage_if_absent("ispA", self.dev, T0, DOWN)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.store.resolve_outage("ispA", self.dev, T0)
        self.notifier.sent.clear()
        self.disp.dispatch([OutageResolved(self.dev)], T0)
        sent = self._by_topic()
        self.assertEqual(sent["a-owner"]["whatsapp"], ["919000000001"])
        self.assertEqual(sent["a-worker"]["whatsapp"], ["919000000009"])
        self.assertEqual(sent["a-owner"]["facts"].status, "UP")

    def test_no_numbers_still_pages_ntfy(self):
        # numbers are additive — an org with topics but no numbers is unchanged
        self.store.set_user_whatsapp(
            self.store.get_user_by_username("own1")["id"], None)
        self.store.set_user_whatsapp(
            self.store.get_user_by_username("wkr1")["id"], None)
        self.store.open_outage_if_absent("ispA", self.dev, T0, DOWN)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        sent = self._by_topic()
        self.assertEqual(sent["a-owner"]["whatsapp"], [])
        self.assertEqual({r for r in sent}, {"a-owner", "a-worker"})


class RouterPushFanoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA", ntfy_topic_worker="a-worker")
        w = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(w, "919000000009")
        self.notifier = RecordingNotifier()
        self.router = AlertRouter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_resolves_worker_numbers(self):
        # every governor caller pages the worker channel; emit resolves those
        # numbers itself on the PUSH send path
        self.router.emit("PORT_DOWN", topic="a-worker", title="port down",
                         body="if 3", priority=3, ts=T0, device_id=7, cooldown_min=0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000009"])

    def test_digest_flush_fans_worker_numbers(self):
        # a DIGEST-tier alert queues (no immediate WhatsApp), then the hourly
        # summary carries the worker numbers
        self.router.emit("PON_FAULT", topic="a-worker", title="cut", body="x",
                         priority=3, ts="2026-01-01T00:00:00+00:00", device_id=7)
        self.assertEqual(self.notifier.sent, [])
        flush_digests(self.store, "ispA", self.notifier, self.cfg,
                      "2026-01-01T02:00:00+00:00")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000009"])


if __name__ == "__main__":
    unittest.main()
