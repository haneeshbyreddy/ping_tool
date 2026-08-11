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
        self.store.set_org("ispA", ntfy_topic_worker="ops-topic")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.alerter = PonFaultAlerter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _onu(self, key, state, distance=None, online_min_ago=2.0, serial=None):
        self.store.upsert_onu_optics(
            "ispA", self.olt, key, pon_port="0/6", onu_id=None, name=key,
            serial=serial, state=state, rx_dbm=None, tx_dbm=None, olt_rx_dbm=None,
            distance_m=distance, rx_ref_dbm=None, rx_ref_at=None, severity="ok",
            ts=_now())
        if state != "online":
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

    def _queued(self, needle=None):
        rows = self.store.pending_digest("ispA")
        return [r for r in rows if needle is None or needle in (r["title"] or "")]

    def test_fresh_fiber_fault_tracked_but_off(self):
        self._mass_drop()
        self.alerter.sweep(_now())
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._queued(), [])
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["active"], 1)
        self.assertEqual(state["kind"], "fiber")
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["active"], 1)

    def test_recovery_clears_state(self):
        self._mass_drop()
        self.alerter.sweep(_now())
        for i in range(3):
            self._onu(f"dark{i}", "online", distance=1800)
        self.alerter.sweep(_now())
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self._queued(), [])
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["active"], 0)

    def test_stale_walk_freezes_fault_state_never_clears(self):
        self._mass_drop()
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["active"], 1)
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_recent(20.0),))
            conn.commit()
        self.alerter.sweep(_now())
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["active"], 1)
        self._mass_drop()
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["active"], 1)
        self.assertEqual(self.notifier.sent, [])

    def _silent_drop(self):
        self._onu("survivor", "online", distance=700, serial="AA:00")
        for i, d in enumerate((1800, 1950, 2300)):
            self._onu(f"dark{i}", "offline", distance=d, serial=f"MAC:{i}")

    def test_a_surviving_reference_onu_turns_a_crew_roll_into_a_power_verdict(self):
        self._silent_drop()
        self._onu("ups", "online", distance=2600, serial="UPS:1")
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["kind"], "fiber")

        self.store.set_onu_place("ispA", "UPS:1", 15.85, 74.5, "Water tank", None, witness=True)
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["kind"], "power")

    def test_a_dark_reference_onu_keeps_the_fiber_verdict(self):
        self._silent_drop()
        self._onu("ups", "offline", distance=2600, serial="UPS:1")
        self.store.set_onu_place("ispA", "UPS:1", 15.85, 74.5, None, None, witness=True)
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["kind"], "fiber")

    def test_clearing_a_placement_restores_the_unwitnessed_verdict(self):
        self._silent_drop()
        self._onu("ups", "online", distance=2600, serial="UPS:1")
        self.store.set_onu_place("ispA", "UPS:1", 15.85, 74.5, None, None, witness=True)
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["kind"], "power")
        self.store.delete_onu_place("ispA", "UPS:1")
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["kind"], "fiber")

    def test_another_orgs_placement_is_not_a_witness_here(self):
        self._silent_drop()
        self._onu("ups", "online", distance=2600, serial="UPS:1")
        self.store.set_onu_place("ispB", "UPS:1", 15.85, 74.5, None, None, witness=True)
        self.alerter.sweep(_now())
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["kind"], "fiber")

    def test_power_pattern_writes_state_but_never_pages(self):
        self._onu("survivor", "online", distance=700)
        for i in range(3):
            self._onu(f"gasp{i}", "dying_gasp", distance=1500)
        self.alerter.sweep(_now())
        self.assertEqual(self._queued(), [])
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["kind"], "power")
        self.assertEqual(state["active"], 1)

    def test_gate_off_suppresses_but_still_tracks(self):
        cfg = Config(db_path=Path(self.tmp.name) / "wisp2.db", pon_fault_alerts=False)
        alerter = PonFaultAlerter(self.store, "ispA", self.notifier, cfg)
        self._mass_drop()
        alerter.sweep(_now())
        self.assertEqual(self._queued(), [])
        self.assertEqual(
            self.store.pon_fault_states("ispA")[(self.olt, "0/6")]["active"], 1)

    def test_fault_detected_with_plant_placed(self):
        self.store.set_org_device_location("ispA", self.olt, 17.000, 78.4)
        splitter = self.store.create_org_device("ispA", {
            "name": "FDB-14", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt, "pon_port": "0/6"})
        self.store.set_org_device_location("ispA", splitter, 17.0108, 78.4)
        self._mass_drop()
        self.alerter.sweep(_now())
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["active"], 1)


class _FakeHandler:
    def __init__(self, store, org, cfg=None):
        self.store = store
        self._org = org
        self.cfg = cfg or Config()
        self.reply = None

    def _reader(self):
        return {"id": 1, "username": "u", "org_id": self._org,
                "role": "owner", "is_superadmin": False}

    def _scope_org(self, user, qs):
        return self._org

    def _reply(self, status, body):
        self.reply = (status, body)


class PonSummaryEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA", ntfy_topic_worker="ops-topic")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})
        self.store.write_device_states(
            "ispA", [(self.olt, "UP", 5.0, 0.0, None)], _now())

    def tearDown(self):
        self.tmp.cleanup()

    def _onu(self, key, state, *, pon="0/6", serial=None, distance=None,
             online_min_ago=2.0):
        self.store.upsert_onu_optics(
            "ispA", self.olt, key, pon_port=pon, onu_id=None, name=key,
            serial=serial, state=state, rx_dbm=None, tx_dbm=None, olt_rx_dbm=None,
            distance_m=distance, rx_ref_dbm=None, rx_ref_at=None, severity="ok",
            ts=_now())
        if state != "online":
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE onu_optics SET last_online_at=? WHERE org_id='ispA'"
                    " AND device_id=? AND onu_key=?",
                    (_recent(online_min_ago), self.olt, key))
                conn.commit()

    def test_summary_counts_fiber_dups_and_online(self):
        from wisp.central.api import outages
        self._onu("survivor", "online", distance=700)
        for i, d in enumerate((1800, 1950, 2300)):
            self._onu(f"dark{i}", "los", distance=d)
        self._onu("loopA", "online", pon="0/7", serial="AA:BB:CC")
        self._onu("loopB", "online", pon="0/7", serial="AA:BB:CC")
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_now(),))
            conn.commit()

        h = _FakeHandler(self.store, "ispA")
        outages.pon_summary(h, {})
        status, body = h.reply
        self.assertEqual(status, 200)
        self.assertEqual(body["olts"], 1)
        self.assertEqual(body["fiber_cuts"], 1)
        self.assertEqual(body["dup_macs_live"], 1)
        self.assertEqual(body["dup_macs_total"], 1)
        self.assertEqual(body["onus_total"], 6)
        self.assertEqual(body["onus_online"], 3)
        self.assertEqual(body["onus_offline"], 3)
        self.assertEqual(body["pons_over_cap"], 0)
        self.assertEqual(body["pon_cap"], 64)

    def test_device_list_stamps_fiber_and_dup_chips(self):
        from wisp.central.api import devices
        self._onu("survivor", "online", distance=700)
        for i, d in enumerate((1800, 1950, 2300)):
            self._onu(f"dark{i}", "los", distance=d)
        self._onu("loopA", "online", pon="0/7", serial="AA:BB:CC")
        self._onu("loopB", "online", pon="0/7", serial="AA:BB:CC")
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_now(),))
            conn.commit()
        h = _FakeHandler(self.store, "ispA")
        rows = self.store.list_org_devices("ispA")
        devices._stamp_optical_faults(h, "ispA", rows)
        olt = next(d for d in rows if d["id"] == self.olt)
        self.assertEqual(olt["fiber_cuts"], 1)
        self.assertEqual(olt["dup_macs"], 1)

    def test_device_list_stamps_zero_when_clean(self):
        from wisp.central.api import devices
        self._onu("a", "online", distance=700)
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_now(),))
            conn.commit()
        h = _FakeHandler(self.store, "ispA")
        rows = self.store.list_org_devices("ispA")
        devices._stamp_optical_faults(h, "ispA", rows)
        olt = next(d for d in rows if d["id"] == self.olt)
        self.assertEqual(olt["fiber_cuts"], 0)
        self.assertEqual(olt["dup_macs"], 0)

    def test_summary_flags_pon_over_cap(self):
        from wisp.central.api import outages
        with self.store._connect() as conn:
            conn.execute("UPDATE org_devices SET onu_pon_limit=2 WHERE id=?",
                         (self.olt,))
            conn.commit()
        for k in ("a", "b", "c"):
            self._onu(k, "online", pon="0/6")
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_now(),))
            conn.commit()
        h = _FakeHandler(self.store, "ispA")
        outages.pon_summary(h, {})
        _, body = h.reply
        self.assertEqual(body["pons_over_cap"], 1)
        self.assertEqual(body["pon_cap_worst"], 3)
        self.assertEqual(body["over_cap_device_ids"], [self.olt])

    def test_summary_zeroes_down_olt(self):
        from wisp.central.api import outages
        self._onu("survivor", "online", distance=700)
        for i, d in enumerate((1800, 1950, 2300)):
            self._onu(f"dark{i}", "los", distance=d)
        self._onu("loopA", "online", pon="0/7", serial="AA:BB:CC")
        self._onu("loopB", "online", pon="0/7", serial="AA:BB:CC")
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_now(),))
            conn.commit()
        self.store.write_device_states(
            "ispA", [(self.olt, "DOWN", None, 100.0, None)], _now())
        h = _FakeHandler(self.store, "ispA")
        outages.pon_summary(h, {})
        status, body = h.reply
        self.assertEqual(status, 200)
        self.assertEqual(body["olts"], 1)
        self.assertEqual(body["onus_total"], 6)
        self.assertEqual(body["onus_online"], 0)
        self.assertEqual(body["onus_offline"], 6)
        self.assertEqual(body["fiber_cuts"], 0)
        self.assertEqual(body["dup_macs_live"], 0)
        self.assertEqual(body["dup_macs_total"], 0)

    def test_summary_drops_probe_silent_olt(self):
        from wisp.central.api import outages
        self._onu("a", "online")
        self._onu("b", "online")
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_now(),))
            conn.commit()
        self.store.write_device_states(
            "ispA", [(self.olt, "UP", 5.0, 0.0, None)], _recent(10.0))
        h = _FakeHandler(self.store, "ispA")
        outages.pon_summary(h, {})
        _, body = h.reply
        self.assertEqual(body["olts"], 0)
        self.assertEqual(body["onus_total"], 0)
        self.assertEqual(body["onus_online"], 0)

    def test_summary_skips_stale_olt(self):
        from wisp.central.api import outages
        self._onu("loopA", "online", serial="AA:BB:CC")
        self._onu("loopB", "online", serial="AA:BB:CC")
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_recent(20.0),))
            conn.commit()
        h = _FakeHandler(self.store, "ispA")
        outages.pon_summary(h, {})
        _, body = h.reply
        self.assertEqual(body["olts"], 0)
        self.assertEqual(body["dup_macs_live"], 0)
        self.assertEqual(body["onus_total"], 0)


if __name__ == "__main__":
    unittest.main()
