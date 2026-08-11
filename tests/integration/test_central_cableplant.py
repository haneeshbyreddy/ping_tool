import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import auth, fiber, inventory
from wisp.central.server import make_server
from wisp.central.store import CentralStore


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "field", "fieldpassword", "worker")
        self.olt = self.store.create_org_device("ispA", {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        self.spl = self.store.create_org_device("ispA", {
            "name": "SPL-1", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt,
            "pon_port": "EPON0/1", "split_ratio": 8})
        self.now = datetime.now(timezone.utc)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, cookie=None):
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
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username, password):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    def _owner(self):
        return self._login("owner", "ownerpassword")

    def _onu(self, serial, *, onu_id=1, pon="EPON0/1", state="online"):
        self.store.upsert_onu_optics(
            "ispA", self.olt, f"{pon}.{onu_id}", pon_port=pon, onu_id=onu_id,
            name=None, serial=serial, state=state, rx_dbm=-21.0, tx_dbm=2.0,
            olt_rx_dbm=-20.0, distance_m=1200, rx_ref_dbm=None, rx_ref_at=None,
            severity="ok", ts=_iso(self.now))


class SplitInputsTest(_Base):
    def _save(self, **fields):
        body = {"id": self.spl, "name": "SPL-1", "ip_address": "",
                "device_type": "splitter", "region": None,
                "parent_device_id": self.olt, "pon_port": "EPON0/1"}
        body.update(fields)
        return self._req("POST", "/api/inventory/update", body, cookie=self._owner())

    def _row(self):
        return self.store.get_org_device("ispA", self.spl)

    def test_a_1_16_saves(self):
        self.assertEqual(self._save(split_ratio=16)[0], 200)
        self.assertEqual(self._row()["split_ratio"], 16)

    def test_a_2_16_saves_both_halves(self):
        self.assertEqual(self._save(split_ratio=16, split_inputs=2)[0], 200)
        row = self._row()
        self.assertEqual((row["split_ratio"], row["split_inputs"]), (16, 2))

    def test_one_input_is_stored_as_absence(self):
        self.assertEqual(self._save(split_ratio=8, split_inputs=1)[0], 200)
        self.assertIsNone(self._row()["split_inputs"])

    def test_a_second_input_needs_a_ratio(self):
        status, body, _ = self._save(split_ratio=None, split_inputs=2)
        self.assertEqual(status, 422, body)

    def test_clearing_the_ratio_clears_the_second_input(self):
        self._save(split_ratio=16, split_inputs=2)
        self.assertEqual(self._save(split_ratio=None, split_inputs=None)[0], 200)
        row = self._row()
        self.assertIsNone(row["split_ratio"])
        self.assertIsNone(row["split_inputs"])

    def test_an_absent_key_reads_as_not_recorded_so_every_writer_must_carry_it(self):
        self._save(split_ratio=16, split_inputs=2)
        self.assertEqual(self._save(split_ratio=16, name="SPL-1-RENAMED")[0], 200)
        self.assertIsNone(self._row()["split_inputs"])

    def test_a_passive_only_column_never_lands_on_gear(self):
        status, _, _ = self._req("POST", "/api/inventory/update", {
            "id": self.olt, "name": "HILL-OLT-1", "ip_address": "10.0.0.1",
            "device_type": "OLT", "region": None, "parent_device_id": None,
            "split_ratio": 16, "split_inputs": 2}, cookie=self._owner())
        self.assertEqual(status, 200)
        row = self.store.get_org_device("ispA", self.olt)
        self.assertIsNone(row["split_ratio"])
        self.assertIsNone(row["split_inputs"])


class CableRecordTest(_Base):


    def _cable(self, cookie, **body):
        body.setdefault("org_id", "ispA")
        return self._req("POST", "/api/inventory/cable", body, cookie)

    def _cables(self, cookie):
        st, data, _ = self._req("GET", "/api/inventory/cables?org=ispA", None, cookie)
        self.assertEqual(st, 200, data)
        return {c["id"]: c for c in data["cables"]}

    def test_a_cable_runs_between_two_points_and_says_so(self):
        cookie = self._owner()
        st, data, _ = self._cable(cookie, name="Main St", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self.assertEqual(st, 200, data)
        cable = self._cables(cookie)[data["id"]]
        self.assertEqual(cable["a"]["device_id"], self.olt)
        self.assertEqual(cable["a"]["name"], "HILL-OLT-1")
        self.assertEqual(cable["b"]["name"], "SPL-1")
        self.assertEqual(cable["cores"], 12)

    def test_a_cable_needs_BOTH_ends_on_create(self):
        cookie = self._owner()
        st, data, _ = self._cable(cookie, name="Half", cores=12,
                                  a_device_id=self.olt)
        self.assertEqual(st, 422, data)

    def test_a_cable_may_not_run_from_a_point_back_to_itself(self):
        cookie = self._owner()
        st, data, _ = self._cable(cookie, name="Loop", cores=12,
                                  a_device_id=self.olt, b_device_id=self.olt)
        self.assertEqual(st, 422, data)

    def test_a_RENAME_need_not_restate_the_ends(self):
        cookie = self._owner()
        _, made, _ = self._cable(cookie, name="Main St", cores=12,
                                 a_device_id=self.olt, b_device_id=self.spl)
        st, _, _ = self._cable(cookie, id=made["id"], name="Main Street", cores=12)
        self.assertEqual(st, 200)
        cable = self._cables(cookie)[made["id"]]
        self.assertEqual(cable["name"], "Main Street")
        self.assertEqual(cable["a"]["device_id"], self.olt)

    def test_a_CUSTOMER_is_a_valid_end(self):
        cookie = self._owner()
        self._onu("AA:BB:CC:00:00:01")
        self.store.set_onu_place("ispA", "AA:BB:CC:00:00:01", 17.0, 78.0,
                                 "RAMESH", None, witness=False)
        st, data, _ = self._cable(cookie, name="Lane 3", cores=4,
                                  a_device_id=self.spl, b_mac="AA:BB:CC:00:00:01")
        self.assertEqual(st, 200, data)
        cable = self._cables(cookie)[data["id"]]
        self.assertEqual(cable["b"]["kind"], "onu")
        self.assertEqual(cable["b"]["name"], "RAMESH")

    def test_an_end_may_not_be_a_box_AND_a_customer(self):
        cookie = self._owner()
        st, _, _ = self._cable(cookie, name="Both", cores=4,
                               a_device_id=self.olt, b_device_id=self.spl,
                               b_mac="AA:BB:CC:00:00:01")
        self.assertEqual(st, 422)

    def test_a_customer_nobody_has_a_record_of_is_a_404(self):
        cookie = self._owner()
        st, _, _ = self._cable(cookie, name="Ghost", cores=4,
                               a_device_id=self.spl, b_mac="DE:AD:BE:EF:00:01")
        self.assertEqual(st, 404)

    def test_an_end_in_ANOTHER_ORG_is_a_404(self):
        cookie = self._owner()
        auth.create_user(self.store, "ispB", "other", "otherpassword", "owner")
        theirs = self.store.create_org_device("ispB", {
            "name": "THEIRS", "ip_address": "10.9.9.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        st, _, _ = self._cable(cookie, name="Leak", cores=12,
                               a_device_id=self.olt, b_device_id=theirs)
        self.assertEqual(st, 404)

    def test_shrinking_the_count_under_a_core_IN_USE_is_refused(self):
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=24,
                                  a_device_id=self.olt, b_device_id=self.spl)
        st, _, _ = self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": trunk["id"], "a_core_no": 19},
            cookie)
        self.assertEqual(st, 200)
        st, data, _ = self._cable(cookie, id=trunk["id"], name="Trunk", cores=12)
        self.assertEqual(st, 422, data)
        self.assertIn("19", data.get("error", ""))

    def test_clearing_the_count_clears_the_cores_with_it(self):
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=24,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": trunk["id"], "a_core_no": 19},
            cookie)
        st, _, _ = self._cable(cookie, id=trunk["id"], name="Trunk", cores=None)
        self.assertEqual(st, 200)
        self.assertEqual(self._cables(cookie)[trunk["id"]]["plan"], {})

    def test_MOVING_an_end_discards_the_joints_made_at_the_old_one(self):
        cookie = self._owner()
        other = self.store.create_org_device("ispA", {
            "name": "SPL-2", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt})
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": trunk["id"], "a_core_no": 3},
            cookie)
        self.assertEqual(self._cables(cookie)[trunk["id"]]["cores_recorded"], 1)
        st, _, _ = self._cable(cookie, id=trunk["id"], name="Trunk", cores=12,
                               a_device_id=self.olt, b_device_id=other)
        self.assertEqual(st, 200)
        self.assertEqual(self._cables(cookie)[trunk["id"]]["plan"], {})

    def test_re_saving_a_cable_UNCHANGED_keeps_its_joints(self):
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": trunk["id"], "a_core_no": 3},
            cookie)
        self._cable(cookie, id=trunk["id"], name="Trunk", cores=12,
                    a_device_id=self.olt, b_device_id=self.spl)
        self.assertEqual(self._cables(cookie)[trunk["id"]]["cores_recorded"], 1)

    def test_deleting_a_cable_takes_its_joints_and_its_core_register(self):
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": trunk["id"], "a_core_no": 3},
            cookie)
        self._req("POST", "/api/inventory/cable/core", {
            "cable_id": trunk["id"], "core_no": 1, "label": "BSNL leased line"},
            cookie)
        st, data, _ = self._req("POST", "/api/inventory/cable/delete",
                                {"id": trunk["id"]}, cookie)
        self.assertEqual(st, 200, data)
        self.assertEqual(self._cables(cookie), {})
        st, tray, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.spl}", None, cookie)
        self.assertEqual(tray["joints"], [])

    def test_RECORDED_is_never_OCCUPIED(self):
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        cable = self._cables(cookie)[trunk["id"]]
        self.assertEqual(cable["cores_recorded"], 0)
        for key in ("cores_free", "spare", "available"):
            self.assertNotIn(key, cable)

    def test_a_LABEL_counts_as_recorded_too(self):
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self._req("POST", "/api/inventory/cable/core", {
            "cable_id": trunk["id"], "core_no": 5, "label": "village A tower"},
            cookie)
        cable = self._cables(cookie)[trunk["id"]]
        self.assertEqual(cable["cores_recorded"], 1)
        self.assertEqual(cable["labels"], {"5": "village A tower"})

    def test_a_worker_may_not_record_fibre(self):
        cookie = self._login("field", "fieldpassword")
        st, _, _ = self._cable(cookie, name="Nope", cores=12,
                               a_device_id=self.olt, b_device_id=self.spl)
        self.assertEqual(st, 403)

    def test_the_tray_is_owner_only_like_every_other_plant_READ(self):
        cookie = self._login("field", "fieldpassword")
        st, _, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.spl}", None, cookie)
        self.assertEqual(st, 403)

    def test_recording_fibre_NEVER_reaches_the_engine(self):
        cookie = self._owner()
        before = self.store.org_device_topology("ispA")
        parents = self.store.org_device_parent_map("ispA")
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": trunk["id"], "a_core_no": 3},
            cookie)
        self.assertEqual(self.store.org_device_topology("ispA"), before)
        self.assertEqual(self.store.org_device_parent_map("ispA"), parents)

    def test_a_span_may_no_longer_carry_a_cable_and_is_TOLD_so(self):
        cookie = self._owner()
        st, data, _ = self._req("POST", "/api/inventory/link-style", {
            "child_id": self.spl, "parent_id": self.olt, "cable_id": 1}, cookie)
        self.assertEqual(st, 422)
        self.assertIn("cable", data.get("error", ""))
        for gone in ("run", "tap", "splice"):
            st, data, _ = self._req("POST", f"/api/inventory/cable/{gone}",
                                    {"cable_id": 1}, cookie)
            self.assertEqual(st, 422, gone)
            self.assertIn("reload", data.get("error", ""))


class CableRouteTest(_Base):
    def _laid(self, cookie, path=None, cores=12):
        _, made, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Main St", "cores": cores,
            "a_device_id": self.olt, "b_device_id": self.spl}, cookie)
        if path is not None:
            st, data, _ = self._req("POST", "/api/inventory/cable/path", {
                "cable_id": made["id"], "path": path}, cookie)
            self.assertEqual(st, 200, data)
        return made["id"]

    def _cables(self, cookie):
        _, data, _ = self._req("GET", "/api/inventory/cables?org=ispA", None, cookie)
        return {c["id"]: c for c in data["cables"]}

    def test_a_traced_cable_reports_its_LENGTH_in_metres(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.001, 78.002]])
        self.assertAlmostEqual(self._cables(cookie)[cid]["length_m"], 323.7, places=0)

    def test_an_UNTRACED_cable_has_no_length_rather_than_zero(self):
        cookie = self._owner()
        cid = self._laid(cookie)
        self.assertIsNone(self._cables(cookie)[cid]["length_m"])

    def test_a_route_of_ONE_POINT_is_refused(self):
        cookie = self._owner()
        cid = self._laid(cookie)
        st, _, _ = self._req("POST", "/api/inventory/cable/path", {
            "cable_id": cid, "path": [[17.0, 78.0]]}, cookie)
        self.assertEqual(st, 422)

    def test_a_route_writes_GEOMETRY_AND_NOTHING_ELSE(self):
        cookie = self._owner()
        cid = self._laid(cookie)
        self._req("POST", "/api/inventory/cable/path", {
            "cable_id": cid, "path": [[17.0, 78.0], [17.0, 78.002]],
            "name": "Renamed", "cores": 48}, cookie)
        cable = self._cables(cookie)[cid]
        self.assertEqual(cable["name"], "Main St")
        self.assertEqual(cable["cores"], 12)

    def test_OPENING_A_COUPLER_splits_the_cable_and_splices_every_core_through(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]])
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["spliced"], 12)
        cables = self._cables(cookie)
        self.assertEqual(len(cables), 2)
        near, far = cables[out["cable_id"]], cables[out["new_cable_id"]]
        self.assertEqual(near["name"], far["name"], "Main St")
        self.assertEqual(near["a"]["device_id"], self.olt)
        self.assertEqual(near["b"]["device_id"], out["closure_id"])
        self.assertEqual(far["a"]["device_id"], out["closure_id"])
        self.assertEqual(far["b"]["device_id"], self.spl)
        st, trace, _ = self._req(
            "GET", f"/api/inventory/fibre/trace?org=ispA&cable={cid}&core=7",
            None, cookie)
        self.assertEqual(st, 200, trace)
        self.assertTrue(trace["ok"])
        self.assertEqual([p["name"] for p in trace["points"]],
                         ["HILL-OLT-1", "JC-1", "SPL-1"])

    def test_the_closure_it_makes_is_a_PASSIVE_and_reaches_no_engine(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]])
        before = self.store.org_device_topology("ispA")
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200)
        self.assertEqual(self.store.org_device_topology("ispA"), before)
        made = [d for d in self.store.list_org_devices("ispA")
                if d["id"] == out["closure_id"]][0]
        self.assertEqual(made["device_type"], "closure")
        self.assertIsNone(made["parent_device_id"])
        self.assertEqual((made["lat"], made["lng"]), (17.0, 78.002))

    def test_splitting_an_UNTRACED_cable_is_refused(self):
        cookie = self._owner()
        cid = self._laid(cookie)
        st, data, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 422)
        self.assertIn("trace", data.get("error", "").lower())

    def test_splitting_at_the_END_is_refused_rather_than_making_a_stub(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.004]])
        st, _, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 77.900}, cookie)
        self.assertEqual(st, 422)

    def test_a_split_cable_with_NO_count_splices_nothing_and_says_so(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]],
                         cores=None)
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["spliced"], 0)

    def test_a_split_carries_the_FAR_END_joints_onto_the_far_half(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]])
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.spl, "a_cable_id": cid, "a_core_no": 4}, cookie)
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200)
        _, tray, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.spl}", None, cookie)
        self.assertEqual([j["a_cable_id"] for j in tray["joints"]],
                         [out["new_cable_id"]])

    def test_a_worker_may_not_trace_or_split(self):
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.004]])
        worker = self._login("field", "fieldpassword")
        for path, body in (("path", {"cable_id": cid, "path": [[17.0, 78.0]]}),
                           ("split", {"cable_id": cid, "lat": 17.0, "lng": 78.002})):
            st, _, _ = self._req("POST", f"/api/inventory/cable/{path}", body, worker)
            self.assertEqual(st, 403, path)


class FibreJointTest(_Base):
    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        self.trunk = self._lay("Trunk", 12, self.olt, self.jc)
        self.branch = self._lay("Branch", 4, self.jc, self.spl)

    def _lay(self, name, cores, a, b):
        _, made, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": name, "cores": cores,
            "a_device_id": a, "b_device_id": b}, self.cookie)
        return made["id"]

    def _joint(self, **body):
        return self._req("POST", "/api/inventory/fibre/joint", body, self.cookie)

    def _tray(self, device_id=None, mac=None):
        q = f"device={device_id}" if device_id else f"onu={mac}"
        _, data, _ = self._req("GET", f"/api/inventory/fibre?org=ispA&{q}",
                               None, self.cookie)
        return data

    def test_a_splice_joins_two_cores_at_a_point(self):
        st, out, _ = self._joint(device_id=self.jc, a_cable_id=self.trunk,
                                 a_core_no=1, b_cable_id=self.branch, b_core_no=1)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"])
        tray = self._tray(self.jc)
        self.assertEqual(len(tray["cables"]), 2)
        self.assertEqual(len(tray["joints"]), 1)

    def test_a_TERMINATION_needs_no_second_cable(self):
        st, out, _ = self._joint(device_id=self.spl, a_cable_id=self.branch,
                                 a_core_no=2)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"])
        tray = self._tray(self.spl)
        self.assertEqual(tray["joints"][0]["b_cable_id"], None)

    def test_a_fibre_that_does_not_END_here_is_refused_BY_NAME(self):
        st, out, _ = self._joint(device_id=self.olt, a_cable_id=self.branch,
                                 a_core_no=1)
        self.assertEqual(st, 200)
        self.assertFalse(out["ok"])
        self.assertEqual(out["refused"], "absent")
        self.assertIn("opened", out["reason"])

    def test_ONE_fibre_joins_exactly_ONE_fibre(self):
        self._joint(device_id=self.jc, a_cable_id=self.trunk, a_core_no=1,
                    b_cable_id=self.branch, b_core_no=1)
        st, out, _ = self._joint(device_id=self.jc, a_cable_id=self.trunk,
                                 a_core_no=1, b_cable_id=self.branch, b_core_no=2)
        self.assertEqual(st, 200)
        self.assertEqual(out["refused"], "taken")

    def test_a_core_past_the_cable_is_refused(self):
        st, data, _ = self._joint(device_id=self.jc, a_cable_id=self.branch,
                                  a_core_no=9)
        self.assertEqual(st, 422)
        self.assertIn("1 and 4", data.get("error", ""))

    def test_SPLICE_STRAIGHT_THROUGH_does_the_whole_tray_at_once(self):
        st, out, _ = self._req("POST", "/api/inventory/fibre/through", {
            "device_id": self.jc, "a_cable_id": self.trunk,
            "b_cable_id": self.branch}, self.cookie)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["spliced"], 4)
        self.assertEqual(len(self._tray(self.jc)["joints"]), 4)

    def test_straight_through_SKIPS_what_is_already_joined_by_hand(self):
        self._joint(device_id=self.jc, a_cable_id=self.trunk, a_core_no=3,
                    b_cable_id=self.branch, b_core_no=1)
        st, out, _ = self._req("POST", "/api/inventory/fibre/through", {
            "device_id": self.jc, "a_cable_id": self.trunk,
            "b_cable_id": self.branch}, self.cookie)
        self.assertEqual((out["spliced"], out["skipped"]), (2, 2))
        again, _ = out["spliced"], None
        st, out, _ = self._req("POST", "/api/inventory/fibre/through", {
            "device_id": self.jc, "a_cable_id": self.trunk,
            "b_cable_id": self.branch}, self.cookie)
        self.assertEqual(out["spliced"], 0)

    def test_CLEARING_is_named_by_the_fibre_so_either_side_can_undo_it(self):
        self._joint(device_id=self.jc, a_cable_id=self.trunk, a_core_no=1,
                    b_cable_id=self.branch, b_core_no=1)
        st, out, _ = self._req("POST", "/api/inventory/fibre/clear", {
            "device_id": self.jc, "cable_id": self.branch, "core_no": 1},
            self.cookie)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"])
        self.assertEqual(self._tray(self.jc)["joints"], [])

    def test_a_CUSTOMER_POINT_has_a_tray_like_any_other(self):
        self._onu("AA:BB:CC:00:00:01")
        self._onu("AA:BB:CC:00:00:02", onu_id=2)
        self.store.set_onu_place("ispA", "AA:BB:CC:00:00:01", 17.0, 78.0,
                                 "RAMESH", None, witness=False)
        self.store.set_onu_place("ispA", "AA:BB:CC:00:00:02", 17.001, 78.001,
                                 "SITA", None, witness=False)
        drop = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Lane 3", "cores": 4,
            "a_device_id": self.spl, "b_mac": "AA:BB:CC:00:00:01"},
            self.cookie)[1]["id"]
        onward = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Lane 3", "cores": 4,
            "a_mac": "AA:BB:CC:00:00:01", "b_mac": "AA:BB:CC:00:00:02"},
            self.cookie)[1]["id"]
        st, out, _ = self._joint(mac="AA:BB:CC:00:00:01", a_cable_id=drop,
                                 a_core_no=2, b_cable_id=onward, b_core_no=2)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"])
        tray = self._tray(mac="AA:BB:CC:00:00:01")
        self.assertEqual(tray["point"]["name"], "RAMESH")
        self.assertEqual(len(tray["cables"]), 2)

    def test_a_joint_across_TWO_ORGS_is_a_404(self):
        auth.create_user(self.store, "ispB", "other", "otherpassword", "owner")
        theirs = self.store.create_org_device("ispB", {
            "name": "THEIRS", "ip_address": "10.9.9.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        their_cable = self.store.set_org_cable(
            "ispB", None, name="Theirs", cores=12, notes=None,
            a={"device_id": theirs, "mac": None},
            b={"device_id": theirs, "mac": None}, updated_by="other")
        st, _, _ = self._joint(device_id=self.jc, a_cable_id=self.trunk,
                               a_core_no=1, b_cable_id=their_cable, b_core_no=1)
        self.assertEqual(st, 404)

    def test_a_worker_may_not_splice(self):
        worker = self._login("field", "fieldpassword")
        st, _, _ = self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.jc, "a_cable_id": self.trunk, "a_core_no": 1},
            worker)
        self.assertEqual(st, 403)


class FibreTraceTest(_Base):
    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        self.trunk = self._lay("Trunk", 12, self.olt, self.jc)
        self.branch = self._lay("Branch", 4, self.jc, self.spl)

    def _lay(self, name, cores, a, b):
        _, made, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": name, "cores": cores,
            "a_device_id": a, "b_device_id": b}, self.cookie)
        return made["id"]

    def _trace(self, cable_id, core_no):
        st, data, _ = self._req(
            "GET",
            f"/api/inventory/fibre/trace?org=ispA&cable={cable_id}&core={core_no}",
            None, self.cookie)
        self.assertEqual(st, 200, data)
        return data

    def test_it_crosses_the_sheath_at_the_closure(self):
        self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.jc, "a_cable_id": self.trunk, "a_core_no": 1,
            "b_cable_id": self.branch, "b_core_no": 3}, self.cookie)
        out = self._trace(self.trunk, 1)
        self.assertTrue(out["ok"])
        self.assertEqual([h["cable_name"] for h in out["hops"]],
                         ["Trunk", "Branch"])
        self.assertEqual([p["name"] for p in out["points"]],
                         ["HILL-OLT-1", "JC-A", "SPL-1"])

    def test_an_UNJOINED_core_stops_where_the_record_stops(self):
        out = self._trace(self.trunk, 2)
        self.assertEqual([p["name"] for p in out["points"]],
                         ["HILL-OLT-1", "JC-A"])

    def test_a_FORK_CANNOT_BE_RECORDED_in_the_first_place(self):
        second = self._lay("Branch 2", 4, self.jc, self.spl)
        st, out, _ = self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.jc, "a_cable_id": self.trunk, "a_core_no": 1,
            "b_cable_id": self.branch, "b_core_no": 1}, self.cookie)
        self.assertTrue(out["ok"], out)
        st, out, _ = self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.jc, "a_cable_id": second, "a_core_no": 1,
            "b_cable_id": self.branch, "b_core_no": 1}, self.cookie)
        self.assertEqual(st, 200)
        self.assertEqual(out["refused"], "taken")
        self.assertTrue(self._trace(self.trunk, 1)["ok"])

    def test_a_cable_that_is_not_there_is_reported_not_guessed(self):
        out = self._trace(9999, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["fault"], "missing")

    def test_the_PLANT_FEED_comes_from_the_glass_when_nothing_is_declared(self):
        feed = self.store.org_plant_feed_map("ispA")
        self.assertEqual(feed[self.jc], self.olt)
        self.assertEqual(feed[self.spl], self.olt)

    def test_a_feed_arriving_through_a_CUSTOMER_is_dropped_not_named(self):
        self._onu("AA:BB:CC:00:00:01")
        self.store.set_onu_place("ispA", "AA:BB:CC:00:00:01", 17.0, 78.0,
                                 "RAMESH", None, witness=False)
        far = self.store.create_org_device("ispA", {
            "name": "SPL-9", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": None})
        self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Drop", "cores": 4,
            "a_device_id": self.spl, "b_mac": "AA:BB:CC:00:00:01"}, self.cookie)
        self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Onward", "cores": 4,
            "a_mac": "AA:BB:CC:00:00:01", "b_device_id": far}, self.cookie)
        self.assertIsNone(self.store.org_plant_feed_map("ispA").get(far))


class DropRouteTest(_Base):
    MAC = "AA:BB:CC:00:00:01"

    def _attach(self, passive_id, mac=None):
        return self._req("POST", "/api/inventory/drops/set",
                         {"macs": [mac or self.MAC], "passive_id": passive_id,
                          "org_id": "ispA"}, cookie=self._owner())

    def _trace(self, waypoints, mac=None, cookie=None):
        return self._req("POST", "/api/inventory/drop-route",
                         {"mac": mac or self.MAC, "waypoints": waypoints,
                          "org_id": "ispA"},
                         cookie=cookie or self._owner())

    def _stored(self, mac=None):
        rows = {d["mac"]: d for d in self.store.list_onu_drops("ispA")}
        return rows.get((mac or self.MAC).upper())

    def test_traces_and_straightens(self):
        self._onu(self.MAC)
        self._attach(self.spl)
        status, body, _ = self._trace([[17.1, 79.1], [17.2, 79.2]])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["points"], 2)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1], [17.2, 79.2]])
        self.assertEqual(self._trace([])[0], 200)
        self.assertEqual(self._stored()["waypoints"], [])

    def test_a_drop_nobody_recorded_has_no_anchor_to_draw_from(self):
        self._onu(self.MAC)
        status, body, _ = self._trace([[17.1, 79.1]])
        self.assertEqual(status, 404, body)

    def test_re_homing_a_drop_discards_its_traced_route(self):
        spl2 = self.store.create_org_device("ispA", {
            "name": "SPL-2", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.olt,
            "pon_port": "EPON0/1", "split_ratio": 8})
        self._onu(self.MAC)
        self._attach(self.spl)
        self._trace([[17.1, 79.1]])
        self._attach(spl2)
        self.assertEqual(self._stored()["waypoints"], [])
        self.assertEqual(self._stored()["passive_id"], spl2)

    def test_re_saving_the_SAME_splitter_keeps_the_route(self):
        self._onu(self.MAC)
        self._attach(self.spl)
        self._trace([[17.1, 79.1]])
        self._attach(self.spl)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1]])

    def test_identity_is_norm_mac_case_insensitive_and_separator_EXACT(self):
        self._onu(self.MAC)
        self._attach(self.spl)
        self.assertEqual(self._trace([[17.1, 79.1]], mac=" aa:bb:cc:00:00:01 ")[0], 200)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1]])
        self.assertEqual(self._trace([[1.0, 2.0]], mac="AABBCC000001")[0], 404)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1]])

    def test_the_map_reply_carries_the_traced_path(self):
        self._onu(self.MAC)
        self._attach(self.spl)
        self.store.set_onu_place("ispA", self.MAC, 17.0, 79.0, None, None,
                                 witness=False)
        self._trace([[17.1, 79.1]])
        status, body, _ = self._req("GET", "/api/inventory/onu-places?org=ispA",
                                    cookie=self._owner())
        self.assertEqual(status, 200, body)
        place = body["places"][0]
        self.assertEqual(place["drop_passive_id"], self.spl)
        self.assertEqual(place["drop_waypoints"], [[17.1, 79.1]])

    def test_an_untraced_drop_ships_an_empty_list_not_a_null(self):
        self._onu(self.MAC)
        self._attach(self.spl)
        self.store.set_onu_place("ispA", self.MAC, 17.0, 79.0, None, None,
                                 witness=False)
        _, body, _ = self._req("GET", "/api/inventory/onu-places?org=ispA",
                               cookie=self._owner())
        self.assertEqual(body["places"][0]["drop_waypoints"], [])

    def test_a_worker_cannot_draw_plant(self):
        self._onu(self.MAC)
        self._attach(self.spl)
        status, _, _ = self._trace([[17.1, 79.1]],
                                   cookie=self._login("field", "fieldpassword"))
        self.assertEqual(status, 403)

    def test_org_isolation(self):
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self._onu(self.MAC)
        self._attach(self.spl)
        status, _, _ = self._req(
            "POST", "/api/inventory/drop-route",
            {"mac": self.MAC, "waypoints": [[17.1, 79.1]], "org_id": "ispA"},
            cookie=self._login("bowner", "bownerpassword"))
        self.assertIn(status, (403, 404))
        self.assertEqual(self._stored()["waypoints"], [])


class FibreTailTest(_Base):

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        self.far = self.store.create_org_device("ispA", {
            "name": "JC-B", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        _, made, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Trunk", "cores": 12,
            "a_device_id": self.jc, "b_device_id": self.far}, self.cookie)
        self.trunk = made["id"]

    def _tail(self, **body):
        return self._req("POST", "/api/inventory/fibre/tail", body, self.cookie)

    def _cables(self):
        _, data, _ = self._req("GET", "/api/inventory/cables?org=ispA",
                               None, self.cookie)
        return data["cables"]

    def test_a_core_reaches_a_box_its_own_cable_never_touches(self):
        st, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                a_core_no=7, to_device_id=self.olt)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)
        _, direct, _ = self._req("POST", "/api/inventory/fibre/joint", {
            "device_id": self.olt, "a_cable_id": self.trunk, "a_core_no": 8,
            "b_cable_id": None}, self.cookie)
        self.assertEqual(direct["refused"], "absent")

    def test_it_writes_a_1F_tail_and_lands_it_at_BOTH_ends(self):
        _, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                               a_core_no=7, to_device_id=self.olt)
        tail = next(c for c in self._cables() if c["id"] == out["cable_id"])
        self.assertEqual(tail["cores"], 1)
        self.assertEqual(tail["a"]["device_id"], self.jc)
        self.assertEqual(tail["b"]["device_id"], self.olt)
        self.assertEqual(tail["path"], [])
        self.assertIsNone(tail["length_m"])
        _, here, _ = self._req(
            f"GET", f"/api/inventory/fibre?org=ispA&device={self.jc}", None,
            self.cookie)
        _, there, _ = self._req(
            f"GET", f"/api/inventory/fibre?org=ispA&device={self.olt}", None,
            self.cookie)
        self.assertEqual(len(here["joints"]), 1)
        self.assertEqual(len(there["joints"]), 1)
        self.assertIsNone(there["joints"][0]["b_cable_id"])

    def test_the_trace_walks_through_it_end_to_end(self):
        self._tail(device_id=self.jc, a_cable_id=self.trunk, a_core_no=7,
                   to_device_id=self.olt)
        _, tr, _ = self._req(
            "GET", f"/api/inventory/fibre/trace?org=ispA&cable={self.trunk}&core=7",
            None, self.cookie)
        self.assertIsNone(tr["fault"])
        self.assertEqual(sorted(h["cable_name"] for h in tr["hops"]), ["", "Trunk"])
        self.assertIn("HILL-OLT-1", [e and e["point"]["name"] for e in tr["ends"]])

    def test_eight_tails_to_one_OLT_are_all_PLUMBING_and_none_is_named(self):
        for core in range(1, 9):
            _, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                   a_core_no=core, to_device_id=self.olt)
            self.assertTrue(out["ok"], out)
        tails = [c for c in self._cables() if c["cores"] == 1]
        self.assertEqual(len(tails), 8)
        self.assertEqual({c["name"] for c in tails}, {""})
        _, at, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.jc}", None,
            self.cookie)
        self.assertEqual([c["name"] for c in at["cables"] if not c["plumbing"]],
                         ["Trunk"])
        self.assertEqual(sum(1 for c in at["cables"] if c["plumbing"]), 8)

    def test_a_fibre_already_spoken_for_is_refused_and_lays_NOTHING(self):
        self._tail(device_id=self.jc, a_cable_id=self.trunk, a_core_no=7,
                   to_device_id=self.olt)
        before = len(self._cables())
        st, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                a_core_no=7, to_device_id=self.spl)
        self.assertEqual(st, 200)
        self.assertFalse(out["ok"])
        self.assertEqual(out["refused"], "taken")
        self.assertEqual(len(self._cables()), before)

    def test_a_core_that_is_not_open_here_is_refused(self):
        _, out, _ = self._tail(device_id=self.spl, a_cable_id=self.trunk,
                               a_core_no=3, to_device_id=self.olt)
        self.assertFalse(out["ok"])
        self.assertEqual(out["refused"], "absent")

    def test_a_tail_to_the_point_it_leaves_is_refused(self):
        st, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                a_core_no=3, to_device_id=self.jc)
        self.assertEqual(st, 422, out)

    def test_a_box_that_is_gone_is_refused_rather_than_cabled_to(self):
        ghost = self.store.create_org_device("ispA", {
            "name": "GONE", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        self.store.delete_org_device("ispA", ghost)
        before = len(self._cables())
        st, _, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                              a_core_no=3, to_device_id=ghost)
        self.assertEqual(st, 404)
        self.assertEqual(len(self._cables()), before)

    def test_another_orgs_box_is_never_reachable(self):
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        theirs = self.store.create_org_device("ispB", {
            "name": "THEIRS", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        st, _, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                              a_core_no=3, to_device_id=theirs)
        self.assertEqual(st, 404)

    def test_a_worker_may_not_run_one(self):
        st, _, _ = self._req(
            "POST", "/api/inventory/fibre/tail",
            {"device_id": self.jc, "a_cable_id": self.trunk, "a_core_no": 3,
             "to_device_id": self.olt},
            cookie=self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)

    def test_deleting_the_box_takes_its_tails_with_it(self):
        sw = self.store.create_org_device("ispA", {
            "name": "SW-1", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self._tail(device_id=self.jc, a_cable_id=self.trunk, a_core_no=7,
                   to_device_id=sw)
        self.assertEqual(self.store.delete_org_device("ispA", sw)["ok"], True)
        self.assertEqual([c["name"] for c in self._cables()], ["Trunk"])
        _, here, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.jc}", None,
            self.cookie)
        self.assertEqual(here["joints"], [])

    def test_undoing_it_is_the_ordinary_clear(self):
        self._tail(device_id=self.jc, a_cable_id=self.trunk, a_core_no=7,
                   to_device_id=self.olt)
        st, out, _ = self._req("POST", "/api/inventory/fibre/clear", {
            "device_id": self.jc, "cable_id": self.trunk, "core_no": 7},
            self.cookie)
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])

    def test_the_tail_lands_on_the_FAR_boxs_port(self):
        st, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                a_core_no=7, to_device_id=self.olt,
                                port_kind="pon", port_no=3)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)
        _, at_olt, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.olt}", None,
            self.cookie)
        self.assertEqual(
            [(j["port_kind"], j["port_no"]) for j in at_olt["joints"]],
            [("pon", 3)])
        _, at_jc, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.jc}", None,
            self.cookie)
        self.assertEqual([j["port_kind"] for j in at_jc["joints"]], [None])


class PortTest(_Base):

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        _, made, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Feeder", "cores": 12,
            "a_device_id": self.olt, "b_device_id": self.spl}, self.cookie)
        self.cable = made["id"]

    def _join(self, **body):
        return self._req("POST", "/api/inventory/fibre/joint", body, self.cookie)

    def _at(self, device_id):
        _, out, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={device_id}", None,
            self.cookie)
        return out

    def test_a_fibre_lands_on_a_NAMED_port_and_the_box_reports_it(self):
        st, out, _ = self._join(device_id=self.olt, a_cable_id=self.cable,
                                a_core_no=1, port_kind="pon", port_no=3)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(
            [(j["port_kind"], j["port_no"]) for j in self._at(self.olt)["joints"]],
            [("pon", 3)])

    def test_an_OLT_lists_the_PONs_it_reports_and_a_recorded_one_never_vanishes(self):
        self._onu("AAAA", pon="EPON0/1")
        self._onu("BBBB", onu_id=2, pon="EPON0/2")
        self._join(device_id=self.olt, a_cable_id=self.cable, a_core_no=1,
                   port_kind="pon", port_no=7)
        self.assertEqual([p["label"] for p in self._at(self.olt)["ports"]],
                         ["PON 1", "PON 2", "PON 7"])

    def test_an_SNMP_SILENT_OLT_can_still_have_a_port_NAMED(self):
        out = self._at(self.olt)
        self.assertEqual(out["ports"], [])
        self.assertEqual(out["port_add"], "pon")
        st, made, _ = self._join(device_id=self.olt, a_cable_id=self.cable,
                                 a_core_no=1, port_kind="pon", port_no=6)
        self.assertEqual(st, 200, made)
        self.assertEqual([p["label"] for p in self._at(self.olt)["ports"]],
                         ["PON 6"])

    def test_a_box_whose_ports_are_ALREADY_ENUMERATED_offers_none_to_add(self):
        self.assertIsNone(self._at(self.spl)["port_add"])
        jc = self.store.create_org_device("ispA", {
            "name": "JC-8", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        self.assertIsNone(self._at(jc)["port_add"])

    def test_a_splitter_NOBODY_MEASURED_is_unbounded_and_does_get_the_row(self):
        bare = self.store.create_org_device("ispA", {
            "name": "SPL-?", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": None})
        self.assertEqual(self._at(bare)["port_add"], "leg")

    def test_a_splitter_lists_its_input_then_its_legs(self):
        self.assertEqual([p["label"] for p in self._at(self.spl)["ports"]],
                         ["input"] + [f"leg {n}" for n in range(1, 9)])

    def test_AN_ENCLOSURE_HAS_NO_PORTS_and_keeps_its_splice_schedule(self):
        jc = self.store.create_org_device("ispA", {
            "name": "JC-9", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        self.assertEqual(self._at(jc)["ports"], [])

    def test_ONE_port_takes_exactly_ONE_fibre(self):
        self._join(device_id=self.olt, a_cable_id=self.cable, a_core_no=1,
                   port_kind="pon", port_no=3)
        _, out, _ = self._join(device_id=self.olt, a_cable_id=self.cable,
                               a_core_no=2, port_kind="pon", port_no=3)
        self.assertEqual(out["refused"], "port_taken")
        self.assertIn("one fibre", out["reason"])

    def test_a_port_on_a_SPLICE_is_refused_by_name(self):
        _, other, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Onward", "cores": 12,
            "a_device_id": self.olt, "b_device_id": self.spl}, self.cookie)
        _, out, _ = self._join(device_id=self.olt, a_cable_id=self.cable,
                               a_core_no=1, b_cable_id=other["id"], b_core_no=1,
                               port_kind="pon", port_no=3)
        self.assertEqual(out["refused"], "port_splice")

    def test_a_leg_past_the_split_is_refused_but_a_PON_is_never_bounded(self):
        st, out, _ = self._join(device_id=self.spl, a_cable_id=self.cable,
                                a_core_no=1, port_kind="leg", port_no=9)
        self.assertEqual(st, 422, out)
        st, out, _ = self._join(device_id=self.olt, a_cable_id=self.cable,
                                a_core_no=2, port_kind="pon", port_no=9)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)

    def test_a_termination_with_NO_port_stays_ordinary(self):
        st, out, _ = self._join(device_id=self.olt, a_cable_id=self.cable,
                                a_core_no=1)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self._at(self.olt)["joints"][0]["port_kind"], None)

    def test_a_DROP_is_reported_on_its_LEG_and_an_unplaced_one_is_SAID(self):
        self.store.set_onu_drops(
            "ispA", ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"], self.spl)
        with self.store._connect() as conn:
            conn.execute("UPDATE onu_drops SET leg_no=2 WHERE mac=?",
                         ("aa:bb:cc:dd:ee:01",))
            conn.commit()
        out = self._at(self.spl)
        legs = {p["label"]: [d["mac"] for d in p["drops"]] for p in out["ports"]}
        self.assertEqual(legs["leg 2"], ["aa:bb:cc:dd:ee:01"])
        self.assertEqual(legs["leg 1"], [])
        self.assertEqual([d["mac"] for d in out["unplaced_drops"]],
                         ["aa:bb:cc:dd:ee:02"])

    def test_the_PON_a_splitter_is_on_comes_from_the_GLASS(self):
        self._join(device_id=self.olt, a_cable_id=self.cable, a_core_no=1,
                   port_kind="pon", port_no=3)
        self._join(device_id=self.spl, a_cable_id=self.cable, a_core_no=1,
                   port_kind="in", port_no=1)
        rows = {d["id"]: d for d in self.store.list_org_devices("ispA")}
        self.assertEqual(rows[self.spl]["fibre_pon"],
                         {"olt_id": self.olt, "pon_no": 3, "source": "fibre",
                          "ambiguous": False})
        self.assertEqual(rows[self.spl]["pon_port"], "EPON0/1")

    def test_the_PON_is_INHERITED_down_the_plant_chain(self):
        deep = self.store.create_org_device("ispA", {
            "name": "SPL-2", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": self.spl, "split_ratio": 4})
        self._join(device_id=self.olt, a_cable_id=self.cable, a_core_no=1,
                   port_kind="pon", port_no=5)
        self._join(device_id=self.spl, a_cable_id=self.cable, a_core_no=1,
                   port_kind="in", port_no=1)
        rows = {d["id"]: d for d in self.store.list_org_devices("ispA")}
        self.assertEqual(rows[deep]["fibre_pon"]["pon_no"], 5)
        self.assertEqual(rows[deep]["fibre_pon"]["source"], "inherited")


    def _connect(self, **body):
        return self._req("POST", "/api/inventory/fibre/connect",
                         {"org_id": "ispA", **body}, self.cookie)

    def test_ONE_call_lays_the_fibre_and_lands_it_at_BOTH_ends(self):
        st, out, _ = self._connect(device_id=self.olt, port_kind="pon", port_no=2,
                                   to_device_id=self.spl)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(
            [(j["port_kind"], j["port_no"]) for j in self._at(self.olt)["joints"]],
            [("pon", 2)])
        self.assertEqual(
            [(j["port_kind"], j["port_no"]) for j in self._at(self.spl)["joints"]],
            [("in", 1)])
        self.assertEqual(out["far_port"], "input")

    def test_the_cable_it_writes_is_NEVER_NAMED(self):
        self._connect(device_id=self.olt, port_kind="pon", port_no=2,
                      to_device_id=self.spl)
        self._connect(device_id=self.olt, port_kind="pon", port_no=3,
                      to_device_id=self.spl)
        _, out, _ = self._req("GET", "/api/inventory/cables?org=ispA", None, self.cookie)
        made = [c["name"] for c in out["cables"] if c["cores"] == 1]
        self.assertEqual(made, ["", ""])

    def test_a_TWO_INPUT_splitter_is_ambiguous_so_nothing_is_claimed(self):
        two = self.store.create_org_device("ispA", {
            "name": "SPL-2IN", "ip_address": "", "device_type": "splitter",
            "region": None, "parent_device_id": None,
            "split_ratio": 16, "split_inputs": 2})
        st, out, _ = self._connect(device_id=self.olt, port_kind="pon", port_no=2,
                                   to_device_id=two)
        self.assertEqual(st, 200, out)
        self.assertIsNone(out["far_port"])
        self.assertEqual([j["port_kind"] for j in self._at(two)["joints"]], [None])

    def test_it_goes_through_the_SAME_refusals_as_the_long_way(self):
        self._connect(device_id=self.olt, port_kind="pon", port_no=2,
                      to_device_id=self.spl)
        _, out, _ = self._connect(device_id=self.olt, port_kind="pon", port_no=2,
                                  to_device_id=self.spl)
        self.assertEqual(out["refused"], "port_taken")
        st, _, _ = self._connect(device_id=self.olt, port_kind="pon", port_no=4,
                                 to_device_id=self.olt)
        self.assertEqual(st, 422)

    def test_it_writes_ONLY_rows_the_long_way_could_have_written(self):
        self._connect(device_id=self.olt, port_kind="pon", port_no=2,
                      to_device_id=self.spl)
        _, cables, _ = self._req("GET", "/api/inventory/cables?org=ispA", None, self.cookie)
        cid = next(c["id"] for c in cables["cables"] if c["cores"] == 1)
        _, tr, _ = self._req("GET", f"/api/inventory/fibre/trace?org=ispA&cable={cid}&core=1",
                             None, self.cookie)
        self.assertTrue(tr["ok"], tr)
        self.assertEqual([p["name"] for p in tr["points"]], ["HILL-OLT-1", "SPL-1"])
        st, out, _ = self._req("POST", "/api/inventory/fibre/clear", {
            "device_id": self.olt, "cable_id": cid, "core_no": 1}, self.cookie)
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])

    def test_a_customer_can_be_recorded_ON_A_LEG_without_wiping_the_others(self):
        st, out, _ = self._req("POST", "/api/inventory/drops/set", {
            "org_id": "ispA", "macs": ["aa:bb:cc:dd:ee:01"],
            "passive_id": self.spl, "leg_no": 3}, self.cookie)
        self.assertEqual(st, 200, out)
        legs = {p["label"]: [d["mac"] for d in p["drops"]]
                for p in self._at(self.spl)["ports"]}
        self.assertEqual(legs["leg 3"], ["AA:BB:CC:DD:EE:01"])
        self._req("POST", "/api/inventory/drops/set", {
            "org_id": "ispA", "macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            "passive_id": self.spl}, self.cookie)
        legs = {p["label"]: [d["mac"] for d in p["drops"]]
                for p in self._at(self.spl)["ports"]}
        self.assertEqual(legs["leg 3"], ["AA:BB:CC:DD:EE:01"],
                         "the bulk write wiped a leg it was never asked about")

    def test_a_leg_past_the_split_is_refused_on_the_DROP_path_too(self):
        st, _, _ = self._req("POST", "/api/inventory/drops/set", {
            "org_id": "ispA", "macs": ["aa:bb:cc:dd:ee:01"],
            "passive_id": self.spl, "leg_no": 99}, self.cookie)
        self.assertEqual(st, 422)

    def test_recording_a_PORT_NEVER_reaches_the_engine(self):
        before = self.store.org_device_topology("ispA")
        self._join(device_id=self.olt, a_cable_id=self.cable, a_core_no=1,
                   port_kind="pon", port_no=3)
        self.assertEqual(self.store.org_device_topology("ispA"), before)


class TheRecordStartsFullTest(_Base):

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()

    def _at(self, device_id):
        _, out, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={device_id}", None,
            self.cookie)
        return out

    def test_the_panel_OPENS_with_the_connection_already_declared(self):
        out = self._at(self.olt)
        self.assertEqual([u["far"]["name"] for u in out["undrawn"]], ["SPL-1"])
        self.assertEqual(out["undrawn"][0]["relation"], "feeds")

    def test_the_box_BELOW_sees_the_same_edge_as_its_feed(self):
        out = self._at(self.spl)
        self.assertEqual([(u["far"]["name"], u["relation"]) for u in out["undrawn"]],
                         [("HILL-OLT-1", "fed by")])

    def test_ONE_CALL_records_it_and_the_draft_row_goes(self):
        st, out, _ = self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.spl,
            "port_kind": "pon", "port_no": 2}, self.cookie)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self._at(self.olt)["undrawn"], [])

    def test_the_cable_it_writes_is_PLUMBING_and_says_so(self):
        self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.spl,
            "port_kind": "pon", "port_no": 2}, self.cookie)
        [c] = self._at(self.olt)["cables"]
        self.assertEqual(c["name"], "")
        self.assertTrue(c["plumbing"])

    def test_BOTH_PORTS_are_named_in_ONE_call(self):
        st, out, _ = self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.spl,
            "port_kind": "pon", "port_no": 2,
            "to_port_kind": "leg", "to_port_no": 5}, self.cookie)
        self.assertEqual(st, 200, out)
        ports = {j["port_kind"]: j["port_no"] for j in self._at(self.spl)["joints"]}
        self.assertEqual(ports, {"leg": 5})

    def test_a_far_port_PAST_THE_SPLIT_is_refused_like_a_near_one(self):
        st, _, _ = self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.spl,
            "port_kind": "pon", "port_no": 2,
            "to_port_kind": "leg", "to_port_no": 99}, self.cookie)
        self.assertEqual(st, 422)

    def test_a_far_port_is_OPTIONAL_and_falls_back_to_the_sole_input(self):
        _, out, _ = self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.spl,
            "port_kind": "pon", "port_no": 2}, self.cookie)
        self.assertEqual(out["far_port"], "input")

    def test_the_DRAFT_IS_NEVER_A_CLAIM(self):
        before = self.store.org_device_topology("ispA")
        self._at(self.olt)
        self._at(self.spl)
        self.assertEqual(self.store.list_org_cables("ispA"), [])
        self.assertEqual(self.store.org_device_topology("ispA"), before)

    def test_CONFIRMING_one_never_reaches_the_engine_either(self):
        before = self.store.org_device_topology("ispA")
        self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.spl,
            "port_kind": "pon", "port_no": 2}, self.cookie)
        self.assertEqual(self.store.org_device_topology("ispA"), before)


class EveryBoxHasPortsTest(_Base):

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.sw = self.store.create_org_device("ispA", {
            "name": "LAN-SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        ts = self.now.isoformat()
        for idx, name in ((49153, "gigabitEthernet 1/0/1"),
                          (49157, "gigabitEthernet 1/0/5"),
                          (1, "Vlan-interface1")):
            self.store.upsert_switch_port(
                "ispA", self.sw, idx, name, None, "up", "up", None, 0, False,
                None, ts)

    def _at(self, device_id):
        _, out, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={device_id}", None,
            self.cookie)
        return out

    def test_a_switch_offers_the_ports_it_WALKS(self):
        out = self._at(self.sw)
        self.assertEqual([p["label"] for p in out["ports"]], ["port 1", "port 5"],
                         "a switch still has nowhere to land a fibre")

    def test_the_number_is_the_one_on_the_METAL_not_the_ifIndex(self):
        out = self._at(self.sw)
        self.assertEqual([p["no"] for p in out["ports"]], [1, 5])

    def test_the_box_s_OWN_NAME_rides_along_for_display(self):
        out = self._at(self.sw)
        self.assertEqual(out["ports"][1]["device_label"], "gigabitEthernet 1/0/5")

    def test_a_VLAN_is_not_somewhere_to_land_a_fibre(self):
        self.assertNotIn("Vlan-interface1",
                         [p.get("device_label") for p in self._at(self.sw)["ports"]])

    def test_a_port_can_be_NAMED_on_a_box_that_walks_nothing(self):
        self.assertEqual(self._at(self.sw)["port_add"], "port")

    def test_an_ENCLOSURE_still_has_none(self):
        jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        out = self._at(jc)
        self.assertEqual(out["ports"], [])
        self.assertIsNone(out["port_add"])

    def test_a_fibre_LANDS_on_a_switch_port_and_the_switch_reports_it(self):
        st, out, _ = self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.sw,
            "port_kind": "pon", "port_no": 1,
            "to_port_kind": "port", "to_port_no": 5}, self.cookie)
        self.assertEqual(st, 200, out)
        ports = {(j["port_kind"], j["port_no"]) for j in self._at(self.sw)["joints"]}
        self.assertEqual(ports, {("port", 5)})

    def test_a_port_NOBODY_WALKED_survives_on_the_list_that_offered_it(self):
        self._req("POST", "/api/inventory/fibre/connect", {
            "org_id": "ispA", "device_id": self.olt, "to_device_id": self.sw,
            "port_kind": "pon", "port_no": 1,
            "to_port_kind": "port", "to_port_no": 48}, self.cookie)
        self.assertIn(48, [p["no"] for p in self._at(self.sw)["ports"]])


class EveryBoxsPortsAreReadableTest(_Base):

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.sw = self.store.create_org_device("ispA", {
            "name": "LAN-SW", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        ts = self.now.isoformat()
        for did, rows in ((self.sw, ((49153, "gigabitEthernet 1/0/1"),
                                     (49157, "gigabitEthernet 1/0/5"),
                                     (1, "Vlan-interface1"))),
                          (self.olt, ((1, "EPON0/1"), (2, "EPON0/2"),
                                      (3, "GE0/9")))):
            for idx, name in rows:
                self.store.upsert_switch_port("ispA", did, idx, name, None, "up",
                                              "up", None, 0, False, None, ts)

    def _ports(self):
        _, out, _ = self._req("GET", "/api/inventory/fibre/ports?org=ispA", None,
                              self.cookie)
        return out["ports"]

    def test_it_answers_for_every_box_at_once(self):
        p = self._ports()
        self.assertEqual([x["label"] for x in p[str(self.olt)]], ["PON 1", "PON 2"])
        self.assertEqual([x["label"] for x in p[str(self.sw)]], ["port 1", "port 5"])
        self.assertEqual(p[str(self.spl)][0]["label"], "input")

    def test_it_carries_the_box_s_OWN_NAME_for_a_numbered_port(self):
        self.assertEqual(self._ports()[str(self.sw)][1]["device_label"],
                         "gigabitEthernet 1/0/5")

    def test_an_ENCLOSURE_is_absent_rather_than_empty(self):
        self.assertNotIn(str(self.jc), self._ports())

    def test_it_agrees_EXACTLY_with_the_panel_it_will_be_offered_beside(self):
        for did in (self.olt, self.sw, self.spl):
            _, panel, _ = self._req(
                f"GET", f"/api/inventory/fibre?org=ispA&device={did}", None,
                self.cookie)
            self.assertEqual(
                [(p["kind"], p["no"]) for p in panel["ports"]],
                [(p["kind"], p["no"]) for p in self._ports().get(str(did), [])],
                f"device {did}")

    def test_a_VIRTUAL_interface_is_not_offered_here_either(self):
        self.assertNotIn("Vlan-interface1",
                         [x.get("device_label") for x in self._ports()[str(self.sw)]])

    def test_it_is_ORG_SCOPED(self):
        other = self.store.create_org_device("ispB", {
            "name": "OTHER", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.assertNotIn(str(other), self._ports())


class CouplersAreClosuresTest(_Base):
    def test_an_existing_coupler_row_is_renamed_on_open(self):
        did = self.store.create_org_device("ispA", {
            "name": "JC-legacy", "ip_address": "", "device_type": "closure",
            "region": None, "parent_device_id": None})
        with self.store._connect() as conn:
            conn.execute("UPDATE org_devices SET device_type='coupler' WHERE id=?",
                         (did,))
            conn.commit()
        reopened = CentralStore(self.cfg.central_db)
        self.assertEqual(reopened.get_org_device("ispA", did)["device_type"],
                         "closure")

    def test_the_TYPE_stays_valid_so_a_straggler_can_never_become_gear(self):
        self.assertIn("coupler", inventory.PASSIVE_TYPES)
        self.assertIn("coupler", fiber.ENCLOSURE_TYPES)
        self.assertEqual(fiber.port_slots("coupler", ports=[1, 2]), [])

    def test_opening_a_cable_mid_span_stands_a_CLOSURE(self):
        cookie = self._owner()
        self.store.set_org_device_location("ispA", self.olt, 1.0, 1.0)
        self.store.set_org_device_location("ispA", self.spl, 1.0, 1.4)
        _, made, _ = self._req("POST", "/api/inventory/cable", {
            "org_id": "ispA", "name": "Trunk", "cores": 12,
            "a_device_id": self.olt, "b_device_id": self.spl,
            "path": [[1.0, 1.0], [1.0, 1.2], [1.0, 1.4]]}, cookie)
        _, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "org_id": "ispA", "cable_id": made["id"], "lat": 1.0, "lng": 1.2},
            cookie)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(
            self.store.get_org_device("ispA", out["closure_id"])["device_type"],
            "closure")


if __name__ == "__main__":
    unittest.main()
