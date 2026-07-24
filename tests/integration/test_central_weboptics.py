"""Scraped optics end to end: the store round-trip, the target query's gates,
and the merge landing on real severity/badge state.

The unit tests pin the merge arithmetic. What matters here is that a scraped
Rx reaches the SAME machinery an SNMP-derived one does — severity, the OLT
badge, the paging transition — because the whole design bet is that the fold
happens BEFORE CentralOpticsMonitor and nothing downstream ever learns where a
reading came from. If that bet is wrong, C-Data ONUs get numbers on a screen
and no alarms behind them.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central.optics import CentralOpticsMonitor
from wisp.central.store import CentralStore
from wisp.central.weboptics import merge_scraped
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"
NOW = "2026-07-22T12:00:00+00:00"
RECENT = "2026-07-22T11:58:00+00:00"
OLD = "2026-07-20T12:00:00+00:00"
MAC = "8C:A3:99:17:D3:38"


def walked(onu_key, serial, state="online", rx=None):
    """A roster row as the SNMP fold produces it on this vendor: no Rx at all."""
    return {"onu_key": onu_key, "pon_port": f"EPON0/{onu_key.split('.')[0]}",
            "onu_id": int(onu_key.split(".")[1]), "name": "sub", "serial": serial,
            "state": state, "rx_dbm": rx, "tx_dbm": None, "olt_rx_dbm": None,
            "distance_m": 2764}


class WebOpticsStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          optical_warn_dbm=-24.0, optical_crit_dbm=-27.0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.olt = self.store.create_org_device(ORG, {
            "name": "PYLON-OLT", "ip_address": "172.168.107.242",
            "device_type": "OLT", "region": "Pylon", "parent_device_id": None})
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def _scraped(self):
        return self.store.list_web_optics(ORG, self.olt)

    def test_readings_round_trip(self):
        self.store.upsert_web_optics(ORG, self.olt, [{
            "onu_key": "3.8", "serial": MAC, "rx_dbm": -2.93, "tx_dbm": 2.4,
            "distance_m": 4531, "temp_c": 47.2, "voltage_v": 3.29,
            "tx_bias_ma": 12.0}], RECENT)
        row = self._scraped()[0]
        self.assertEqual(row["serial"], MAC)
        self.assertEqual(row["rx_dbm"], -2.93)
        self.assertEqual(row["distance_m"], 4531)
        self.assertEqual(row["scraped_at"], RECENT)

    def test_a_second_scrape_updates_in_place(self):
        for rx, ts in ((-21.0, OLD), (-23.5, RECENT)):
            self.store.upsert_web_optics(ORG, self.olt, [{
                "onu_key": "3.8", "serial": MAC, "rx_dbm": rx}], ts)
        rows = self._scraped()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rx_dbm"], -23.5)

    def test_a_partial_scrape_leaves_untouched_pons_alone(self):
        # The OLT holds one session slot, so a sweep can legitimately end early.
        # Storing must never be delete-then-insert or every partial scrape would
        # black out the PONs that simply weren't reached this time.
        self.store.upsert_web_optics(ORG, self.olt, [
            {"onu_key": "1.1", "serial": "AA:AA:AA:00:00:01", "rx_dbm": -18.0},
            {"onu_key": "3.8", "serial": MAC, "rx_dbm": -21.0}], OLD)
        self.store.upsert_web_optics(ORG, self.olt, [
            {"onu_key": "1.1", "serial": "AA:AA:AA:00:00:01", "rx_dbm": -18.4}],
            RECENT)
        rows = {r["onu_key"]: r for r in self._scraped()}
        self.assertEqual(rows["1.1"]["rx_dbm"], -18.4)
        self.assertEqual(rows["3.8"]["rx_dbm"], -21.0)     # kept, just older
        self.assertEqual(rows["3.8"]["scraped_at"], OLD)

    def test_rows_without_a_key_are_dropped_not_stored_blank(self):
        self.store.upsert_web_optics(ORG, self.olt, [
            {"onu_key": "", "serial": MAC, "rx_dbm": -21.0},
            {"onu_key": "3.8", "serial": MAC, "rx_dbm": -21.0}], RECENT)
        self.assertEqual([r["onu_key"] for r in self._scraped()], ["3.8"])


class WebOpticsTargetsTest(unittest.TestCase):
    """Who the sweeper is allowed to touch. Each gate keeps it off a box it has
    no business POSTing an admin login to."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own")
        self.store.set_org_web_proxy(ORG, True)

    def tearDown(self):
        self.tmp.cleanup()

    def _olt(self, name="PYLON-OLT", vendor="dbc", node="edge-1"):
        did = self.store.create_org_device(ORG, {
            "name": name, "ip_address": "172.168.107.242", "device_type": "OLT",
            "region": "Pylon", "parent_device_id": None,
            "gpon_vendor": vendor, "assigned_node_id": node})
        return did

    def _creds(self, device_id, username="admin", enc="enc:xxx"):
        self.store.set_device_webui_credentials(
            ORG, device_id, username=username, password_enc=enc,
            set_password=True, auth_mode="form", updated_by="tester")

    def _ids(self):
        return [t["id"] for t in self.store.web_optics_targets()]

    def _roster(self, device_id, *pon_ports):
        """The SNMP walk's roster — what a scraped reading merges ONTO."""
        for i, pon in enumerate(pon_ports or ("EPON0/1",)):
            self.store.upsert_onu_optics(
                ORG, device_id, f"{i}.1", pon_port=pon, onu_id=1, name="sub",
                serial=MAC, state="online", rx_dbm=None, tx_dbm=None,
                olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None,
                rx_ref_at=None, severity="ok", ts=RECENT)

    def _detected(self, device_id, profile="dbc",
                  sysoid="1.3.6.1.4.1.37950.1.1.5.10.14.1"):
        """What the EDGE reported about its own optics sweep of this OLT."""
        self.store.upsert_snmp_statuses(
            ORG, [(device_id, "optics",
                   {"state": "ok", "profile": profile, "sysobjectid": sysoid,
                    "count": 1})], RECENT)

    def test_a_configured_dbc_olt_is_a_target(self):
        did = self._olt()
        self._creds(did)
        self._roster(did)
        rows = self.store.web_optics_targets()
        self.assertEqual([r["id"] for r in rows], [did])
        self.assertEqual(rows[0]["username"], "admin")
        self.assertEqual(rows[0]["assigned_node_id"], "edge-1")

    def test_a_device_without_stored_credentials_is_not_a_target(self):
        did = self._olt()
        self._roster(did)
        self.assertEqual(self._ids(), [])

    def test_an_edge_detected_dbc_olt_is_a_target(self):
        # THE generalization. The vendor field is on automatic, but the edge's
        # own optics sweep matched the dbc profile off this box's sysObjectID
        # and said so on its report. That is the same evidence an operator
        # would have used to pick the dropdown value, straight from the box —
        # and requiring it is what took this subsystem off one hand-configured
        # OLT and onto every C-Data OLT in the fleet.
        did = self._olt(vendor=None)
        self._creds(did)
        self._roster(did)
        self._detected(did)
        self.assertEqual(self._ids(), [did])

    def test_a_detected_vendor_without_a_sysobjectid_does_not_qualify(self):
        # `profile` is also echoed back for an OVERRIDE — including a
        # fleet-wide WISP_GPON_VENDOR default, which is a config value, not the
        # box identifying itself. Only a real auto-detect stamps sysObjectID,
        # so that is the field that has to be there.
        did = self._olt(vendor=None)
        self._creds(did)
        self._roster(did)
        self._detected(did, sysoid=None)
        self.assertEqual(self._ids(), [])

    def test_another_vendors_detection_does_not_qualify(self):
        # The login form and page path are one vendor's recipe.
        did = self._olt(vendor=None)
        self._creds(did)
        self._roster(did)
        self._detected(did, profile="huawei")
        self.assertEqual(self._ids(), [])

    def test_an_undetected_olt_on_automatic_is_still_not_a_target(self):
        # Nothing has claimed this box. "Probably C-Data" is not enough to
        # start POSTing an admin login at it, and never was.
        for vendor in (None, "", "huawei"):
            did = self._olt(name=f"OLT-{vendor}", vendor=vendor)
            self._creds(did)
            self._roster(did)
        self.assertEqual(self._ids(), [])

    def test_an_olt_with_no_roster_is_not_a_target(self):
        # A reading merges onto a walked slot and can never create one, so
        # there is nothing here for a scrape to surface — the login and the
        # page fetch could only ever be discarded. The live example is a C-Data
        # GPON box that was logged into every 15 minutes for a day to be told
        # its firmware has no OPM Diag page.
        did = self._olt()
        self._creds(did)
        self.assertEqual(self._ids(), [])

    def test_the_target_carries_the_olts_own_pon_ports(self):
        # One POST per PON, so the sweeper has to know how many there are. The
        # roster is the only honest source; a constant is how half the fleet's
        # ONUs went unasked-about while the scrape reported success.
        did = self._olt()
        self._creds(did)
        self._roster(did, "EPON0/1", "EPON0/3", "EPON0/8")
        labels = self.store.web_optics_targets()[0]["pon_ports"]
        self.assertEqual(sorted(labels.split(",")),
                         ["EPON0/1", "EPON0/3", "EPON0/8"])

    def test_a_device_with_no_assigned_probe_is_not_a_target(self):
        did = self._olt(node=None)
        self._creds(did)
        self._roster(did)
        self.assertEqual(self._ids(), [])

    def test_an_org_without_the_web_proxy_grant_is_not_swept(self):
        # Without the grant the edge holds no long-poll, so every request would
        # only burn its timeout.
        did = self._olt()
        self._creds(did)
        self._roster(did)
        self.store.set_org_web_proxy(ORG, False)
        self.assertEqual(self._ids(), [])

    def test_one_device_can_be_singled_out_without_widening_the_gates(self):
        """The manual refresh narrows this query rather than looking a device up
        on its own — "may this box be scraped" must have exactly one answer, or
        a hand-triggered read could reach an OLT the sweep refuses."""
        ok, no_roster = self._olt(), self._olt(name="HILL-OLT")
        self._creds(ok)
        self._creds(no_roster)
        self._roster(ok)
        self.assertEqual([r["id"] for r in self.store.web_optics_targets(
            device_id=ok)], [ok])
        # ineligible for the sweep is ineligible for the button, identically
        self.assertEqual(self.store.web_optics_targets(device_id=no_roster), [])

    def test_a_device_in_maintenance_is_left_alone(self):
        did = self._olt()
        self._creds(did)
        self._roster(did)
        self.store.set_org_device_maintenance(ORG, did, True)
        self.assertEqual(self._ids(), [])


class ScrapedReadingDrivesAlarmsTest(unittest.TestCase):
    """The point of merging before the monitor: a scraped Rx must behave in
    every way like a walked one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          optical_warn_dbm=-24.0, optical_crit_dbm=-27.0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own", ntfy_topic_worker="op")
        self.olt = self.store.create_org_device(ORG, {
            "name": "PYLON-OLT", "ip_address": "172.168.107.242",
            "device_type": "OLT", "region": "Pylon", "parent_device_id": None})
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def _sync(self, roster, max_age_s=3600):
        merged, n = merge_scraped(
            roster, self.store.list_web_optics(ORG, self.olt), NOW, max_age_s)
        CentralOpticsMonitor(self.store, ORG, self.notifier,
                             self.cfg).sync_device(self.olt, merged, NOW)
        return n

    def _rows(self):
        return {r["onu_key"]: r for r in self.store.list_onu_optics(ORG, self.olt)}

    def test_a_scraped_rx_sets_severity_and_pages_like_any_other(self):
        self.store.upsert_web_optics(ORG, self.olt, [
            {"onu_key": "3.8", "serial": MAC, "rx_dbm": -29.4,
             "distance_m": 4531}], RECENT)
        merged = self._sync([walked("3.8", MAC)])

        self.assertEqual(merged, 1)
        row = self._rows()["3.8"]
        self.assertEqual(row["rx_dbm"], -29.4)
        self.assertEqual(row["severity"], "crit")
        # Distance stays the walk's: the page's real metres are stored but not
        # merged, or ponfault would bracket a cut across two units. See
        # weboptics._MERGED_FIELDS.
        self.assertEqual(row["distance_m"], 2764)
        badge = self.store.get_olt_optics(ORG, self.olt)
        self.assertEqual(badge["crit_count"], 1)
        self.assertTrue(badge["alarm"])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_without_a_scrape_the_vendor_stays_honestly_blank(self):
        # This is the pre-existing DBC behaviour and it must survive untouched:
        # no reading is better than a fabricated one.
        self._sync([walked("3.8", MAC)])
        row = self._rows()["3.8"]
        self.assertIsNone(row["rx_dbm"])
        self.assertEqual(row["severity"], "ok")
        self.assertEqual(self.notifier.sent, [])

    def test_a_stale_scrape_stops_driving_the_badge(self):
        # A scrape that quietly stopped working must not hold an alarm open on
        # evidence that is two days old.
        self.store.upsert_web_optics(ORG, self.olt, [
            {"onu_key": "3.8", "serial": MAC, "rx_dbm": -29.4}], OLD)
        merged = self._sync([walked("3.8", MAC)])
        self.assertEqual(merged, 0)
        self.assertIsNone(self._rows()["3.8"]["rx_dbm"])
        self.assertFalse(self.store.get_olt_optics(ORG, self.olt)["alarm"])

    def test_an_offline_onu_keeps_its_last_walked_state(self):
        # The page only lists ONUs it just queried over the fibre, so a dark
        # ONU has no reading by construction — and its zombie slot must not
        # inherit the live one's.
        self.store.upsert_web_optics(ORG, self.olt, [
            {"onu_key": "3.8", "serial": MAC, "rx_dbm": -21.0}], RECENT)
        self._sync([walked("3.8", MAC), walked("1.4", MAC, state="offline")])
        rows = self._rows()
        self.assertEqual(rows["3.8"]["rx_dbm"], -21.0)
        self.assertIsNone(rows["1.4"]["rx_dbm"])
        # An offline ONU is never judged, whatever its last reading was.
        self.assertEqual(rows["1.4"]["severity"], "ok")


if __name__ == "__main__":
    unittest.main()
