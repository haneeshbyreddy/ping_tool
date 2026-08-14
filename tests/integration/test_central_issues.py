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

from wisp.central import auth, issues
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config
from support import RecordingNotifier


def _iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


class IssueListTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "field", "fieldpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.store.issue_node_token("ispA", "probe1")
        self.now = datetime.now(timezone.utc)
        self.store.touch_node("ispA", "probe1", _iso(self.now))
        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()


    def _device(self, name, ip, *, org="ispA", node="probe1", **extra):
        return self.store.create_org_device(org, {
            "name": name, "ip_address": ip, "device_type": "switch",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": node, **extra})

    def _state(self, did, state, *, org="ispA", ts=None, loss=None):
        self.store.write_device_states(
            org, [(did, state, None, loss, None)], _iso(ts or self.now))

    def _port(self, did, if_index, *, org="ispA", alarm=True, monitored=True,
              name="Gi1/0/1", alias=None):
        self.store.upsert_switch_port(
            org, did, if_index, name, alias, "up", "down" if alarm else "up",
            None, 3 if alarm else 0, alarm,
            _iso(self.now - timedelta(minutes=10)) if alarm else None,
            _iso(self.now))
        pid = next(p["id"] for p in self.store.list_switch_ports(org, did)
                   if p["if_index"] == if_index)
        self.store.set_port_monitored(org, pid, monitored)
        return pid

    def _req(self, method, path, body=None, cookie=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        head = dict(resp.getheaders())
        conn.close()
        if raw:
            return resp.status, data, head
        return resp.status, (json.loads(data) if data else {}), head

    def _login(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _collect(self, org="ispA"):
        return issues.collect(self.store, self.cfg, org, now=self.now)


    def test_a_switch_with_two_dark_ports_is_two_rows(self):
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1, name="Gi1/0/1")
        self._port(sw, 2, name="Gi1/0/2", alias="uplink to OLT")
        rows = [r for r in self._collect() if r["kind"] == "port_down"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["subject"] for r in rows},
                         {"Gi1/0/1", "Gi1/0/2 (uplink to OLT)"})
        self.assertTrue(all(r["device_name"] == "CH-SW" for r in rows))

    def test_a_down_device_is_one_critical_row(self):
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN", loss=100.0)
        rows = [r for r in self._collect() if r["kind"] == "device_down"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], "critical")
        self.assertIn("DOWN", rows[0]["detail"])

    def test_a_degraded_device_is_a_warning_not_a_critical(self):
        d = self._device("EDGE", "10.0.0.5")
        self._state(d, "DEGRADED", loss=40.0)
        row = next(r for r in self._collect() if r["kind"] == "device_down")
        self.assertEqual(row["severity"], "warning")

    def test_maintenance_and_unmonitored_devices_are_not_issues(self):
        maint = self._device("MAINT-SW", "10.0.0.7")
        self._state(maint, "DOWN")
        self.store.set_org_device_maintenance("ispA", maint, True)
        loose = self.store.create_org_device("ispA", {
            "name": "NO-PROBE", "ip_address": "10.0.0.8", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self._state(loose, "DOWN")
        names = {r["device_name"] for r in self._collect()}
        self.assertNotIn("MAINT-SW", names)
        self.assertNotIn("NO-PROBE", names)

    def test_a_device_behind_an_unregistered_probe_is_not_counted(self):
        d = self._device("ORPHAN", "10.0.0.9", node="ghost")
        self._state(d, "DOWN")
        self.assertNotIn("ORPHAN", {r["device_name"] for r in self._collect()})

    def test_an_admin_down_port_is_silent_but_an_alarmed_one_is_not(self):
        sw = self._device("SW", "10.0.0.3")
        self._state(sw, "UP")
        self._port(sw, 1, alarm=False)
        self._port(sw, 2, monitored=False)
        self.assertEqual([r for r in self._collect() if r["kind"] == "port_down"], [])

    def test_a_port_on_an_unreachable_switch_is_kept_but_marked_frozen(self):
        sw = self._device("DARK-SW", "10.0.0.4")
        self._state(sw, "UNREACHABLE")
        self._port(sw, 1)
        row = next(r for r in self._collect() if r["kind"] == "port_down")
        self.assertEqual(row["severity"], "info")
        self.assertIn("frozen", row["detail"])

    def test_a_silent_probe_is_its_own_row_with_no_device(self):
        self.store.issue_node_token("ispA", "probe2")
        self.store.touch_node("ispA", "probe2",
                              _iso(self.now - timedelta(hours=2)))
        row = next(r for r in self._collect() if r["kind"] == "probe_stale")
        self.assertEqual(row["subject"], "probe2")
        self.assertIsNone(row["device_id"])
        self.assertEqual(row["severity"], "critical")

    def test_a_revoked_probe_never_pages_as_an_issue(self):
        self.store.issue_node_token("ispA", "gone")
        self.store.touch_node("ispA", "gone", _iso(self.now - timedelta(hours=2)))
        self.store.revoke_node_token("ispA", "gone")
        self.assertNotIn("gone", {r["subject"] for r in self._collect()})

    def test_critical_issues_sort_above_warnings(self):
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN")
        soft = self._device("SOFT", "10.0.0.6")
        self._state(soft, "DEGRADED")
        rows = self._collect()
        self.assertEqual(rows[0]["severity"], "critical")
        self.assertEqual([r["severity"] for r in rows],
                         sorted((r["severity"] for r in rows),
                                key=lambda s: issues.SEVERITY_RANK[s]))


    def test_endpoint_carries_rows_counts_and_the_kind_vocabulary(self):
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1)
        status, body, head = self._req("GET", "/api/issues?org=ispA",
                                       cookie=self._login())
        self.assertEqual(status, 200)
        self.assertEqual(body["counts"]["port_down"], 1)
        self.assertEqual(body["total"], len(body["issues"]))
        self.assertEqual(list(body["kinds"]), list(issues.KINDS))
        self.assertEqual(head.get("Cache-Control"), "no-store")

    def test_kind_filter_narrows_rows_but_never_the_counts(self):
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1)
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN")
        _, body, _ = self._req("GET", "/api/issues?org=ispA&kind=port_down",
                               cookie=self._login())
        self.assertEqual({r["kind"] for r in body["issues"]}, {"port_down"})
        self.assertEqual(body["counts"]["device_down"], 1)

    def test_an_unknown_kind_shows_the_whole_list_rather_than_erroring(self):
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN")
        status, body, _ = self._req("GET", "/api/issues?org=ispA&kind=nonsense",
                                    cookie=self._login())
        self.assertEqual(status, 200)
        self.assertEqual(len(body["issues"]), 1)

    def test_an_org_owner_is_pinned_to_its_own_org_whatever_it_asks_for(self):
        other = self._device("B-SW", "10.9.9.9", org="ispB", node="bprobe")
        self._state(other, "DOWN", org="ispB")
        mine = self._device("A-SW", "10.0.0.1")
        self._state(mine, "DOWN")
        _, body, _ = self._req("GET", "/api/issues?org=ispB", cookie=self._login())
        names = {r["device_name"] for r in body["issues"]}
        self.assertEqual(names, {"A-SW"})

    def test_signed_out_callers_get_401(self):
        status, _, _ = self._req("GET", "/api/issues?org=ispA")
        self.assertEqual(status, 401)

    def test_a_worker_can_read_the_issue_list(self):
        status, _, _ = self._req("GET", "/api/issues?org=ispA",
                                 cookie=self._login("field", "fieldpassword"))
        self.assertEqual(status, 200)

    def test_a_workers_issue_list_is_still_pinned_to_its_own_org(self):
        other = self._device("B-SW", "10.9.9.9", org="ispB", node="bprobe")
        self._state(other, "DOWN", org="ispB")
        mine = self._device("A-SW", "10.0.0.1")
        self._state(mine, "DOWN")
        self.store.set_device_assignees(
            "ispA", mine,
            [next(u["id"] for u in self.store.list_users("ispA")
                  if u["username"] == "field")], "owner")
        _, body, _ = self._req("GET", "/api/issues?org=ispB",
                               cookie=self._login("field", "fieldpassword"))
        self.assertEqual({r["device_name"] for r in body["issues"]}, {"A-SW"})


    def test_pdf_export_is_a_pdf_attachment(self):
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1, alias="uplink (main)")
        status, blob, head = self._req("GET", "/api/issues/pdf?org=ispA",
                                       cookie=self._login(), raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(head["Content-Type"], "application/pdf")
        self.assertIn("attachment; filename=\"issues-ispA-", head["Content-Disposition"])
        self.assertTrue(blob.startswith(b"%PDF"))
        self.assertIn(b"CH-SW", blob)
        self.assertIn(rb"uplink \(main\)", blob)

    def test_pdf_export_respects_the_kind_filter(self):
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1)
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN")
        _, blob, _ = self._req("GET", "/api/issues/pdf?org=ispA&kind=device_down",
                               cookie=self._login(), raw=True)
        self.assertIn(b"CORE", blob)
        self.assertNotIn(b"CH-SW", blob)

    def test_pdf_times_are_rendered_in_the_operators_zone(self):
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1)
        _, blob, _ = self._req("GET", "/api/issues/pdf?org=ispA",
                               cookie=self._login(), raw=True)
        self.assertIn(b"IST", blob)
        self.assertNotIn(b"+00:00", blob)

    def test_pdf_export_of_a_healthy_org_is_still_a_valid_file(self):
        status, blob, _ = self._req("GET", "/api/issues/pdf?org=ispA",
                                    cookie=self._login(), raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(blob.startswith(b"%PDF"))
        self.assertIn(rb"0 issue\(s\)", blob)


    def test_xlsx_export_is_a_real_workbook(self):
        import io, zipfile
        from xml.etree import ElementTree
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1, alias="uplink (main)")
        status, blob, head = self._req("GET", "/api/issues/xlsx?org=ispA",
                                       cookie=self._login(), raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(
            head["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn('filename="issues-ispA-', head["Content-Disposition"])
        self.assertTrue(head["Content-Disposition"].endswith('.xlsx"'))
        zf = zipfile.ZipFile(io.BytesIO(blob))
        self.assertIsNone(zf.testzip())
        sheet = zf.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("CH-SW", sheet)
        self.assertIn("uplink (main)", sheet)
        ElementTree.fromstring(sheet)

    def test_xlsx_since_is_a_date_cell_not_text(self):
        import io, zipfile
        from xml.etree import ElementTree
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN")
        _, blob, _ = self._req("GET", "/api/issues/xlsx?org=ispA",
                               cookie=self._login(), raw=True)
        sheet = ElementTree.fromstring(
            zipfile.ZipFile(io.BytesIO(blob)).read("xl/worksheets/sheet1.xml").decode())
        row = sheet.findall(".//m:row", ns)[1]
        since = row[-1]
        self.assertIsNone(since.get("t"))
        self.assertGreater(float(since.find("m:v", ns).text), 40000)

    def test_xlsx_export_respects_the_kind_filter(self):
        import io, zipfile
        sw = self._device("CH-SW", "10.0.0.2")
        self._state(sw, "UP")
        self._port(sw, 1)
        core = self._device("CORE", "10.0.0.1")
        self._state(core, "DOWN")
        _, blob, _ = self._req("GET", "/api/issues/xlsx?org=ispA&kind=device_down",
                               cookie=self._login(), raw=True)
        sheet = zipfile.ZipFile(io.BytesIO(blob)).read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("CORE", sheet)
        self.assertNotIn("CH-SW", sheet)

    def test_xlsx_export_needs_a_session_and_is_org_pinned(self):
        import io, zipfile
        status, body, _ = self._req("GET", "/api/issues/xlsx?org=ispA")
        self.assertEqual(status, 401)
        other = self._device("B-SW", "10.9.9.9", org="ispB", node="bprobe")
        self._state(other, "DOWN", org="ispB")
        mine = self._device("A-SW", "10.0.0.1")
        self._state(mine, "DOWN")
        _, blob, _ = self._req("GET", "/api/issues/xlsx?org=ispB",
                               cookie=self._login(), raw=True)
        sheet = zipfile.ZipFile(io.BytesIO(blob)).read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("A-SW", sheet)
        self.assertNotIn("B-SW", sheet)

    def test_a_worker_can_download_either_export(self):
        cookie = self._login("field", "fieldpassword")
        for path in ("/api/issues/pdf?org=ispA", "/api/issues/xlsx?org=ispA"):
            self.assertEqual(self._req("GET", path, cookie=cookie, raw=True)[0], 200, path)

    def test_pdf_export_needs_a_session(self):
        status, body, _ = self._req("GET", "/api/issues/pdf?org=ispA")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_pdf_export_is_pinned_to_the_sessions_own_org(self):
        other = self._device("B-SW", "10.9.9.9", org="ispB", node="bprobe")
        self._state(other, "DOWN", org="ispB")
        mine = self._device("A-SW", "10.0.0.1")
        self._state(mine, "DOWN")
        _, blob, _ = self._req("GET", "/api/issues/pdf?org=ispB",
                               cookie=self._login(), raw=True)
        self.assertIn(b"A-SW", blob)
        self.assertNotIn(b"B-SW", blob)


if __name__ == "__main__":
    unittest.main()
