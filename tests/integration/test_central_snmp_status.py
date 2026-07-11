"""SNMP guided troubleshooting, central side.

The edge diagnoses each SNMP subsystem per device (`snmp_status` on the full
/report); central stores the verdicts for the dashboard's "why is this panel
blank" flow, and operators can mark a subsystem as hardware-unsupported so the
admin coverage overview stops counting it as a problem.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class SnmpStatusStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.dev = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})

    def tearDown(self):
        self.tmp.cleanup()

    def _status(self):
        return {r["subsystem"]: r
                for r in self.store.device_snmp_status("ispA", self.dev)}

    def test_upsert_keeps_last_ok_and_coalesces_sysobjectid(self):
        self.store.upsert_snmp_statuses("ispA", [
            (self.dev, "health", {"state": "ok", "sysobjectid": "1.3.6.1.4.1.5651.3",
                                  "count": 3}),
        ], "2026-01-01T00:00:00+00:00")
        self.store.upsert_snmp_statuses("ispA", [
            (self.dev, "health", {"state": "timeout", "detail": "walk exceeded 20s"}),
        ], "2026-01-02T00:00:00+00:00")
        row = self._status()["health"]
        self.assertEqual(row["state"], "timeout")
        self.assertEqual(row["updated_at"], "2026-01-02T00:00:00+00:00")
        # last_ok_at survives the failure; sysobjectid holds across a silent walk.
        self.assertEqual(row["last_ok_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(row["sysobjectid"], "1.3.6.1.4.1.5651.3")

    def test_unknown_subsystem_or_state_is_dropped(self):
        self.store.upsert_snmp_statuses("ispA", [
            (self.dev, "quantum", {"state": "ok"}),
            (self.dev, "health", {"state": "on_fire"}),
        ], "2026-01-01T00:00:00+00:00")
        self.assertEqual(self._status(), {})

    def test_capability_supported_true_deletes_the_exception_row(self):
        self.assertTrue(self.store.set_device_capability(
            "ispA", self.dev, "optics", False, "EPON agent has no optical table",
            updated_by="alice"))
        caps = self.store.device_capabilities("ispA", self.dev)
        self.assertEqual(len(caps), 1)
        self.assertFalse(caps[0]["supported"])
        self.assertEqual(caps[0]["updated_by"], "alice")
        self.assertTrue(self.store.set_device_capability(
            "ispA", self.dev, "optics", True))
        self.assertEqual(self.store.device_capabilities("ispA", self.dev), [])

    def test_capability_rejects_unknown_subsystem_and_wrong_org(self):
        self.assertFalse(self.store.set_device_capability(
            "ispA", self.dev, "quantum", False))
        self.assertFalse(self.store.set_device_capability(
            "ispB", self.dev, "optics", False))

    def test_delete_device_purges_status_and_capability(self):
        self.store.upsert_snmp_statuses("ispA", [
            (self.dev, "health", {"state": "ok"}),
        ], "2026-01-01T00:00:00+00:00")
        self.store.set_device_capability("ispA", self.dev, "optics", False)
        self.assertTrue(self.store.delete_org_device("ispA", self.dev)["ok"])
        self.assertEqual(self.store.device_snmp_status("ispA", self.dev), [])
        self.assertEqual(self.store.device_capabilities("ispA", self.dev), [])


class OverviewSuppressionTest(unittest.TestCase):
    """An operator-confirmed "hardware can't do X" drops the gap from the
    superadmin coverage rollup — denominators and problem list both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA", ntfy_topic_owner="own")
        self.now = datetime.now(timezone.utc)
        self.fresh = (self.now - timedelta(seconds=60)).isoformat(timespec="seconds")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.store.set_org_device_snmp("ispA", self.olt, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161})
        # Fresh health so the device's SNMP counts as alive — the optics gap is
        # then the OLT's own problem, not suppressed under a dead-SNMP root cause.
        self.store.upsert_device_health(
            "ispA", self.olt, {"cpu_pct": 10.0}, self.fresh)

    def tearDown(self):
        self.tmp.cleanup()

    def _org(self):
        ov = self.store.admin_overview(now=self.now)
        return next(o for o in ov["orgs"] if o["org_id"] == "ispA")

    def test_unsupported_optics_leaves_both_counts_and_problems(self):
        before = self._org()
        self.assertEqual(before["optics"]["olts"], 1)
        self.assertEqual([p["area"] for p in before["problems"]], ["optics"])

        self.store.set_device_capability("ispA", self.olt, "optics", False,
                                         "no optical table on this EPON build")
        after = self._org()
        self.assertEqual(after["optics"]["olts"], 0)
        self.assertEqual(after["problems"], [])

        # Flipping back to supported restores the gap as a problem.
        self.store.set_device_capability("ispA", self.olt, "optics", True)
        self.assertEqual([p["area"] for p in self._org()["problems"]], ["optics"])


class SnmpStatusHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.dev = self.store.create_org_device("ispA", {
            "name": "OLT-1", "ip_address": "10.0.0.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, cookie=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username, password):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    def _report(self, snmp_status=None):
        body = {"v": 1, "org_id": "ispA", "node_id": "edge-1", "mode": "full",
                "pings": {"10.0.0.9": {"loss_pct": 0.0, "latency_ms": 5.0}}}
        if snmp_status is not None:
            body["snmp_status"] = snmp_status
        status, resp, _ = self._req("POST", "/report", body, token="tok")
        return status, resp

    def test_report_snmp_status_lands_and_reads_back(self):
        status, _ = self._report(snmp_status={str(self.dev): {
            "optics": {"state": "no_profile",
                       "detail": "no GPON vendor profile claims this OLT",
                       "sysobjectid": "1.3.6.1.4.1.9999.7"},
            "health": {"state": "ok", "count": 3,
                       "profile": "fiberhome"},
        }})
        self.assertEqual(status, 200)

        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-status?device_id={self.dev}", cookie=cookie)
        self.assertEqual(status, 200, body)
        rows = {r["subsystem"]: r for r in body["status"]}
        self.assertEqual(rows["optics"]["state"], "no_profile")
        self.assertEqual(rows["optics"]["sysobjectid"], "1.3.6.1.4.1.9999.7")
        self.assertEqual(rows["health"]["state"], "ok")
        self.assertEqual(rows["health"]["profile"], "fiberhome")
        self.assertEqual(rows["health"]["item_count"], 3)
        self.assertEqual(body["capability"], [])

    def test_status_for_a_device_outside_the_org_is_ignored(self):
        stranger = self.store.create_org_device("ispB", {
            "name": "B-SW", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        status, _ = self._report(snmp_status={str(stranger): {
            "health": {"state": "ok"}}})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.device_snmp_status("ispB", stranger), [])

    def test_capability_roundtrip_and_cross_org_forbidden(self):
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/capability", {
            "device_id": self.dev, "subsystem": "optics", "supported": False,
            "note": "EPON build exposes no optical table"}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req(
            "GET", f"/api/inventory/snmp-status?device_id={self.dev}", cookie=cookie)
        self.assertEqual(len(body["capability"]), 1)
        self.assertFalse(body["capability"][0]["supported"])
        self.assertEqual(body["capability"][0]["updated_by"], "owner")

        # supported=true clears the exception.
        status, _, _ = self._req("POST", "/api/inventory/capability", {
            "device_id": self.dev, "subsystem": "optics", "supported": True},
            cookie=cookie)
        self.assertEqual(status, 200)
        _, body, _ = self._req(
            "GET", f"/api/inventory/snmp-status?device_id={self.dev}", cookie=cookie)
        self.assertEqual(body["capability"], [])

        cookie_b = self._login("bowner", "bownerpassword")
        status, _, _ = self._req("POST", "/api/inventory/capability", {
            "device_id": self.dev, "subsystem": "optics", "supported": False},
            cookie=cookie_b)
        self.assertEqual(status, 403)
        status, _, _ = self._req(
            "GET", f"/api/inventory/snmp-status?device_id={self.dev}", cookie=cookie_b)
        self.assertEqual(status, 403)

    def test_capability_validates_subsystem(self):
        cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/capability", {
            "device_id": self.dev, "subsystem": "quantum", "supported": False},
            cookie=cookie)
        self.assertEqual(status, 422)
        self.assertIn("subsystem", body["error"])


if __name__ == "__main__":
    unittest.main()
