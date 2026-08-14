"""The user MAC: storage, eligibility, the sweep, and what the panel is told.

The point of the feature is the case the optics sweep cannot reach: a Syrotech
GPON OLT serves the address table and has NO optical page at all, and its ONUs
have no MAC of their own (their identity is a GPON serial), so the address
table is the ONLY place that customer's address exists.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import auth  # noqa: E402
from wisp.central.server import make_server  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.central.weboptics_sweep import WebOpticsSweeper  # noqa: E402
from wisp.config import Config  # noqa: E402

ORG = "ispA"
NOW = datetime.now(timezone.utc)
RECENT = NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00")
OLD = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

HEAD = ("<tr><td class='hd'>VLAN ID</td><td class='hd'>MAC Address</td>"
        "<td class='hd'>Type</td><td class='hd'>Port ID</td></tr>")


def page(rows, total=None):
    head = ""
    if total is not None:
        head = ("<script>var s=document.getElementById(\"macCount\"); "
                f"s.value = '{total}';</script>")
    body = "".join(
        f"<tr><td>{v}</td><td>{m}</td><td>Dynamic</td><td>{p}</td></tr>"
        for v, m, p in rows)
    return f"<html><body>{head}<table>{HEAD}{body}</table></body></html>"


class FakeHub:
    """Stands in for ProxyHub: serves canned replies, records what was asked."""

    def __init__(self, pages: dict[str, str], charset="gb2312") -> None:
        self.pages = pages
        self.charset = charset
        self.asked: list[str] = []
        self.logins = 0

    def polled_recently(self, org, node, hold):
        return True

    def active_sessions_for(self, org, node, idle_s=None):
        return []

    def reap_expired(self):
        return []

    def submit(self, session, *, method, path, headers, body, timeout, extra=None):
        if extra and extra.get("kind") == "preflight":
            return None
        self.asked.append(f"{method} {path}")
        if method == "POST":
            self.logins += 1
        base = path.split("?")[0]
        html = self.pages.get(base, "<html>nothing here</html>")
        import base64
        return {"status": 200 if base in self.pages else 404,
                "headers": [],
                "body_b64": base64.b64encode(html.encode(self.charset)).decode()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own")
        self.store.set_org_web_proxy(ORG, True)

    def tearDown(self):
        self.tmp.cleanup()

    def _olt(self, name="OLT", vendor="dbc", node="edge-1", org=ORG):
        return self.store.create_org_device(org, {
            "name": name, "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None,
            "gpon_vendor": vendor, "assigned_node_id": node})

    def _creds(self, device_id, org=ORG):
        self.store.set_device_webui_credentials(
            org, device_id, username="admin", password_enc="enc:x",
            set_password=True, auth_mode="form", updated_by="t")

    def _roster(self, device_id, onu_key="1.4", serial="AA:11:22:33:44:55",
                org=ORG, state="online"):
        self.store.upsert_onu_optics(
            org, device_id, onu_key, pon_port="EPON0/1", onu_id=4, name="sub",
            serial=serial, state=state, rx_dbm=-21.0, tx_dbm=None,
            olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=RECENT)


class UserMacStoreTest(Base):

    def test_an_address_is_stored_against_the_slot_and_read_back(self):
        did = self._olt()
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "D0:1E:1D:14:16:3A", "vlan": "1900",
             "kind": "Dynamic", "port_label": "EPON0/1:4"}], RECENT)
        got = self.store.user_macs_for_slot(ORG, did, "1.4")
        self.assertEqual([r["mac"] for r in got], ["D0:1E:1D:14:16:3A"])
        self.assertEqual(got[0]["vlan"], "1900")

    def test_ONE_SLOT_KEEPS_EVERY_ADDRESS_it_carries(self):
        did = self._olt()
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:01"},
            {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:02"}], RECENT)
        self.assertEqual(len(self.store.user_macs_for_slot(ORG, did, "1.4")), 2)

    def test_seeing_it_again_moves_last_seen_and_KEEPS_first_seen(self):
        did = self._olt()
        row = {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:01"}
        self.store.upsert_user_macs(ORG, did, [row], OLD)
        self.store.upsert_user_macs(ORG, did, [row], RECENT)
        got = self.store.user_macs_for_slot(ORG, did, "1.4")[0]
        self.assertEqual(got["first_seen_at"], OLD)
        self.assertEqual(got["last_seen_at"], RECENT)

    def test_AN_ADDRESS_THAT_AGES_OUT_IS_KEPT_not_deleted(self):
        # The address table is LEARNED and ages out, so an idle or offline
        # customer drops off the page while being the same customer with the
        # same router. Their last known address is exactly what the RADIUS
        # lookup needs — and it is wanted MOST when they are down.
        did = self._olt()
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:01"}], OLD)
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "9.9", "mac": "AA:BB:CC:DD:EE:99"}], RECENT)
        kept = self.store.user_macs_for_slot(ORG, did, "1.4")
        self.assertEqual([r["mac"] for r in kept], ["AA:BB:CC:DD:EE:01"])
        self.assertEqual(kept[0]["last_seen_at"], OLD)

    def test_a_second_router_is_a_second_row_not_a_replacement(self):
        did = self._olt()
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:01"}], OLD)
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "BB:BB:CC:DD:EE:02"}], RECENT)
        got = self.store.user_macs_for_slot(ORG, did, "1.4")
        self.assertEqual([r["mac"] for r in got],
                         ["BB:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:01"])

    def test_one_orgs_addresses_are_invisible_to_another(self):
        self.store.set_org("ispB", ntfy_topic_owner="b")
        did = self._olt()
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:01"}], RECENT)
        self.assertEqual(self.store.user_macs_for_slot("ispB", did, "1.4"), [])
        self.assertEqual(self.store.list_user_macs("ispB", did), [])

    def test_deleting_the_olt_takes_its_addresses_with_it(self):
        did = self._olt()
        self.store.upsert_user_macs(ORG, did, [
            {"onu_key": "1.4", "mac": "AA:BB:CC:DD:EE:01"}], RECENT)
        self.store.delete_org_device(ORG, did)
        self.assertEqual(self.store.list_user_macs(ORG, did), [])


class MacTargetTest(Base):

    def _ids(self, vendors=("dbc", "syrotech_gpon")):
        return [t["id"] for t in self.store.user_mac_targets(vendors)]

    def test_A_GPON_OLT_WITH_NO_OPTICS_PROFILE_IS_STILL_A_MAC_TARGET(self):
        # The whole reason these are separate: syrotech_gpon serves the address
        # table and has no optical page, so folding the recipe into the optics
        # profile would leave the fleet that needs it most unconfigurable.
        did = self._olt(vendor="syrotech_gpon")
        self._creds(did)
        self._roster(did)
        self.assertEqual(self._ids(), [did])
        self.assertEqual([t["id"] for t in self.store.web_optics_targets(
            ("dbc", "cdata_54824"))], [])

    def test_an_olt_with_no_stored_login_is_not_a_target(self):
        did = self._olt()
        self._roster(did)
        self.assertEqual(self._ids(), [])

    def test_an_olt_with_no_roster_is_not_a_target(self):
        # An address lands ON a roster slot; with no roster there is nothing for
        # it to attach to.
        did = self._olt()
        self._creds(did)
        self.assertEqual(self._ids(), [])

    def test_a_maintenance_olt_is_not_a_target(self):
        did = self._olt()
        self._creds(did)
        self._roster(did)
        self.store.set_org_device_maintenance(ORG, did, True)
        self.assertEqual(self._ids(), [])

    def test_an_org_without_the_web_proxy_grant_is_not_a_target(self):
        self.store.set_org_web_proxy(ORG, False)
        did = self._olt()
        self._creds(did)
        self._roster(did)
        self.assertEqual(self._ids(), [])


class MacSweepTest(Base):

    def _sweeper(self, hub):
        class Box:
            def decrypt(self, enc):
                return "secret"
        return WebOpticsSweeper(self.store, hub, Box(), self.cfg)

    def _run(self, hub, vendor="syrotech_gpon"):
        did = self._olt(vendor=vendor)
        self._creds(did)
        self._roster(did)
        self._sweeper(hub).sweep_once()
        return did

    def test_a_mac_only_olt_is_swept_and_its_addresses_stored(self):
        hub = FakeHub({
            "/action/login.html": "<html>login</html>",
            "/action/main.html": "<html>ok</html>",
            "/action/macinfo.html": page([
                ("100", "E4:47:B3:A4:83:12", "PON1:ONU4"),
                ("100", "44:FB:5A:9D:E4:4A", "GE1")]),
        })
        did = self._run(hub)
        got = self.store.user_macs_for_slot(ORG, did, "1.4")
        self.assertEqual([r["mac"] for r in got], ["E4:47:B3:A4:83:12"])
        status = self.store.get_web_mac_status(ORG, did)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["rows"], 1)

    def test_the_uplink_row_is_never_attributed_to_a_customer(self):
        hub = FakeHub({
            "/action/login.html": "<html>login</html>",
            "/action/main.html": "<html>ok</html>",
            "/action/macinfo.html": page([("1", "44:FB:5A:9D:E4:4A", "GE1")]),
        })
        did = self._run(hub)
        self.assertEqual(self.store.list_user_macs(ORG, did), [])

    def test_a_TRUNCATED_read_is_recorded_as_partial_not_as_success(self):
        # A short read makes a customer who HAS an address look exactly like one
        # who does not. The OLT's own declared total is what catches it.
        hub = FakeHub({
            "/action/login.html": "<html>login</html>",
            "/action/main.html": "<html>ok</html>",
            "/action/macinfo.html": page(
                [("1", "AA:BB:CC:DD:EE:01", "EPON0/1:4")], total=97),
        })
        did = self._run(hub, vendor="dbc")
        status = self.store.get_web_mac_status(ORG, did)
        self.assertEqual(status["state"], "partial")
        self.assertEqual(status["declared"], 97)
        detail = status["detail"] or ""
        self.assertIn("97", detail)
        self.assertIn("96", detail)   # names the shortfall, not just "partial"
        # the addresses it DID get are still kept
        self.assertEqual(len(self.store.list_user_macs(ORG, did)), 1)

    def test_a_build_with_no_declared_total_is_not_called_truncated(self):
        hub = FakeHub({
            "/action/login.html": "<html>login</html>",
            "/action/main.html": "<html>ok</html>",
            "/action/macinfo.html": page([("100", "E4:47:B3:A4:83:12",
                                           "PON1:ONU4")]),
        })
        did = self._run(hub)
        status = self.store.get_web_mac_status(ORG, did)
        self.assertEqual(status["state"], "ok")
        self.assertIsNone(status["declared"])

    def test_a_missing_page_is_reported_and_stores_nothing(self):
        hub = FakeHub({"/action/login.html": "<html>login</html>",
                       "/action/main.html": "<html>ok</html>"})
        did = self._run(hub)
        status = self.store.get_web_mac_status(ORG, did)
        self.assertEqual(status["state"], "unreachable")
        self.assertEqual(self.store.list_user_macs(ORG, did), [])

    def test_THE_CREDENTIAL_IS_NOT_SENT_when_the_login_page_does_not_answer(self):
        hub = FakeHub({"/action/macinfo.html": page([])})
        self._run(hub)
        self.assertEqual(hub.logins, 0)

    def test_a_browsed_probe_is_skipped_and_says_why(self):
        hub = FakeHub({"/action/login.html": "<html>login</html>",
                       "/action/main.html": "<html>ok</html>",
                       "/action/macinfo.html": page([])})
        hub.active_sessions_for = lambda org, node, idle_s=None: [{"sid": "x"}]
        did = self._run(hub)
        status = self.store.get_web_mac_status(ORG, did)
        self.assertEqual(status["state"], "skipped")
        self.assertIn("browsing", status["detail"])
        self.assertEqual(hub.asked, [])


class SubscriberApiTest(Base):

    def setUp(self):
        super().setUp()
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        self.olt = self._olt()
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        super().tearDown()

    def _cookie(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": "owner",
                                      "password": "ownerpassword"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        return cookie

    def _get(self, mac):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/inventory/subscriber?mac={mac}",
                     headers={"Cookie": self._cookie()})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw)

    def test_the_subscriber_reply_carries_the_user_mac_and_its_status(self):
        serial = "AA:11:22:33:44:55"
        self._roster(self.olt, serial=serial)
        self.store.upsert_user_macs(ORG, self.olt, [
            {"onu_key": "1.4", "mac": "D0:1E:1D:14:16:3A", "vlan": "1900",
             "kind": "Dynamic", "port_label": "EPON0/1:4"}], RECENT)
        self.store.set_web_mac_status(ORG, self.olt, "dbc", "ok", None, 1, 548)
        status, body = self._get(serial)
        self.assertEqual(status, 200, body)
        self.assertEqual([m["mac"] for m in body["user_macs"]],
                         ["D0:1E:1D:14:16:3A"])
        self.assertEqual(body["user_macs"][0]["vlan"], "1900")
        self.assertEqual(body["user_mac_status"]["state"], "ok")
        self.assertEqual(body["user_mac_status"]["declared"], 548)

    def test_a_subscriber_with_no_address_gets_an_empty_list_not_a_missing_key(self):
        serial = "AA:11:22:33:44:56"
        self._roster(self.olt, serial=serial)
        status, body = self._get(serial)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["user_macs"], [])
        self.assertIsNone(body["user_mac_status"])

    def test_ANOTHER_SLOTS_ADDRESS_IS_NOT_REPORTED_HERE(self):
        # A MAC pinned to the wrong drop sends a tech to the wrong house.
        serial = "AA:11:22:33:44:57"
        self._roster(self.olt, serial=serial)
        self.store.upsert_user_macs(ORG, self.olt, [
            {"onu_key": "9.9", "mac": "D0:1E:1D:14:16:3A"}], RECENT)
        _, body = self._get(serial)
        self.assertEqual(body["user_macs"], [])


if __name__ == "__main__":
    unittest.main()
