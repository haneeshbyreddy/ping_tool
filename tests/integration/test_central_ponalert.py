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

    def test_stale_walk_freezes_fault_state_never_clears(self):
        # A slow C-Data agent missing one walk made every open fault "recover"
        # and re-page when the walk landed (36 PON_FAULT pages in one hour,
        # 2026-07-14). A stale OLT freezes; recovery needs a fresh walk.
        self._mass_drop()
        self.alerter.sweep(_now())
        self.assertEqual(
            len([s for s in self.notifier.sent if "fiber cut" in s["title"]]), 1)
        # the OLT's walk goes stale: restamp every row 20 min into the past
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_optics SET updated_at=? WHERE org_id='ispA'",
                         (_recent(20.0),))
            conn.commit()
        self.alerter.sweep(_now())
        self.assertFalse(any("recovered" in s["title"] for s in self.notifier.sent))
        state = self.store.pon_fault_states("ispA")[(self.olt, "0/6")]
        self.assertEqual(state["active"], 1)
        # the walk lands again, fault unchanged: no re-page either
        self._mass_drop()
        self.alerter.sweep(_now())
        self.assertEqual(
            len([s for s in self.notifier.sent if "fiber cut" in s["title"]]), 1)

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


class _FakeHandler:
    """Minimal stand-in for the request handler the api modules ride on."""
    def __init__(self, store, org, cfg=None):
        self.store = store
        self._org = org
        self.cfg = cfg or Config()
        self.reply = None

    def _reader(self):
        return {"id": 1, "username": "u", "org_id": self._org,
                "role": "operator", "is_superadmin": False}

    def _scope_org(self, user, qs):
        return self._org

    def _reply(self, status, body):
        self.reply = (status, body)


class PonSummaryEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA", ntfy_topic_operator="ops-topic")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "OLT",
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-1"})

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
        # a fiber mass-drop on 0/6 …
        self._onu("survivor", "online", distance=700)
        for i, d in enumerate((1800, 1950, 2300)):
            self._onu(f"dark{i}", "los", distance=d)
        # … plus a live duplicate MAC (same serial, two ONLINE slots) on 0/7
        self._onu("loopA", "online", pon="0/7", serial="AA:BB:CC")
        self._onu("loopB", "online", pon="0/7", serial="AA:BB:CC")
        # one walk stamps every row identically; the roster view keys off that
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
        # the inventory list (Network device rows) stamps the same fiber-cut and
        # live-dup-MAC verdicts as the summary strip, mapped onto the OLT row.
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
        # every device carries the fields (default 0) so the row chips never
        # read undefined — a healthy OLT stamps 0/0, not absent.
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
        # cap this OLT at 2 ONUs/PON; three online on 0/6 → over cap
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
        # the stale OLT drops out entirely — no phantom dup off frozen rows
        self.assertEqual(body["olts"], 0)
        self.assertEqual(body["dup_macs_live"], 0)
        self.assertEqual(body["onus_total"], 0)


if __name__ == "__main__":
    unittest.main()
