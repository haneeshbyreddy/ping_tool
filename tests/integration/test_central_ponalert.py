import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import RecordingNotifier
from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central.ponalert import PonFaultAlerter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recent(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class PonFaultAlerterTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.notifier = RecordingNotifier()
        self.cfg = Config(db_path=Path(self.tmp.name) / "wisp.db")
        self.store.set_org("ispA", ntfy_topic_operator="ops-topic")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.alerter = PonFaultAlerter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _onu(self, key, state, distance=None, online_min_ago=2.0):
        self.store.upsert_onu_optics(
            "ispA", self.olt, key, pon_port="0/6", onu_id=None, name=key,
            serial=None, state=state, rx_dbm=None, tx_dbm=None, olt_rx_dbm=None,
            distance_m=distance, rx_ref_dbm=None, rx_ref_at=None, severity="ok",
            ts=_now())
        if state != "online":
            # simulate "was online until a moment ago": the upsert only stamps
            # last_online_at while online, so prime it directly
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE onu_optics SET last_online_at=? WHERE org_id='ispA'"
                    " AND device_id=? AND onu_key=?",
                    (_recent(online_min_ago), self.olt, key))
                conn.commit()

    def _mass_drop(self):
        self._onu("survivor", "online", distance=700)
        for i, d in enumerate((1800, 1950, 2300)):
            self._onu(f"dark{i}", "los", distance=d)

    def test_fresh_fiber_fault_pages_once(self):
        self._mass_drop()
        self.alerter.sweep(_now())
        cuts = [s for s in self.notifier.sent if "fiber cut" in s["title"]]
        self.assertEqual(len(cuts), 1)
        self.assertIn("OLT-1", cuts[0]["title"])
        self.assertIn("0/6", cuts[0]["title"])
        self.assertEqual(cuts[0]["recipient"], "ops-topic")
        # same fault on the next walk: state stands, no re-page
        self.alerter.sweep(_now())
        self.assertEqual(
            len([s for s in self.notifier.sent if "fiber cut" in s["title"]]), 1)

    def test_recovery_pages_and_clears_state(self):
        self._mass_drop()
        self.alerter.sweep(_now())
        for i in range(3):
            self._onu(f"dark{i}", "online", distance=1800)
        self.alerter.sweep(_now())
        self.assertTrue(any("recovered" in s["title"] for s in self.notifier.sent))
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["active"], 0)

    def test_power_pattern_writes_state_but_never_pages(self):
        self._onu("survivor", "online", distance=700)
        for i in range(3):
            self._onu(f"gasp{i}", "dying_gasp", distance=1500)
        self.alerter.sweep(_now())
        self.assertEqual(self.notifier.sent, [])
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["kind"], "power")
        self.assertEqual(state["active"], 1)

    def test_gate_off_suppresses_but_still_tracks(self):
        cfg = Config(db_path=Path(self.tmp.name) / "wisp2.db", pon_fault_alerts=False)
        alerter = PonFaultAlerter(self.store, "ispA", self.notifier, cfg)
        self._mass_drop()
        alerter.sweep(_now())
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["active"], 1)

    def test_suspect_named_when_plant_sits_in_the_interval(self):
        # splitter placed ~1.2 km down the same PON, inside (0.7, 1.8] km
        self.store.set_org_device_location("ispA", self.olt, 17.000, 78.4)
        splitter = self.store.create_org_device("ispA", {
            "name": "FDB-14", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt, "pon_port": "0/6"})
        self.store.set_org_device_location("ispA", splitter, 17.0108, 78.4)
        self._mass_drop()
        self.alerter.sweep(_now())
        cuts = [s for s in self.notifier.sent if "fiber cut" in s["title"]]
        self.assertEqual(len(cuts), 1)
        self.assertIn("FDB-14", cuts[0]["body"])


if __name__ == "__main__":
    unittest.main()
