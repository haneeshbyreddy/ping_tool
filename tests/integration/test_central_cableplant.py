"""The plant record: splitters, the fibre graph, and a subscriber's drop route.

  * splitters up to 1:16, and the 2:16 protection-input form;
  * a multi-core cable on a link — fibre count and which strand a run uses;
  * a traced route for a subscriber's drop.

Grouped in one file because they are one session's answer to one question — "the
map knows where our plant IS, but not what it is" — and because the sharpest
rules cut across them: a strand may not outlive the cable it was bounded by, a
route may not outlive the anchor it was drawn from, and a second input is a fact
about hardware rather than about wiring.
"""
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
from wisp.central import auth
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


# ---------------------------------------------------------------------------
# 1. Splitters: 1:16, and the 2:16 protection-input form
# ---------------------------------------------------------------------------

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
        # The sparse-storage rule this schema follows everywhere: 1 is the
        # default form of the object, so recording it explicitly must not write
        # a row that differs from every splitter placed before the column
        # existed.
        self.assertEqual(self._save(split_ratio=8, split_inputs=1)[0], 200)
        self.assertIsNone(self._row()["split_inputs"])

    def test_a_second_input_needs_a_ratio(self):
        # "2:?" names no product anybody stocks. Refused rather than stored as
        # half a fact — the same shape as a strand with no cable to be a strand
        # of, one feature over.
        status, body, _ = self._save(split_ratio=None, split_inputs=2)
        self.assertEqual(status, 422, body)

    def test_clearing_the_ratio_clears_the_second_input(self):
        self._save(split_ratio=16, split_inputs=2)
        self.assertEqual(self._save(split_ratio=None, split_inputs=None)[0], 200)
        row = self._row()
        self.assertIsNone(row["split_ratio"])
        self.assertIsNone(row["split_inputs"])

    def test_an_absent_key_reads_as_not_recorded_so_every_writer_must_carry_it(self):
        # The documented trap this column inherits from `split_ratio` and
        # `onu_pon_limit`: a caller that forgets the key silently downgrades the
        # box on the next rename. Pinned so a NEW caller of update_org_device is
        # a failing test rather than a support ticket about a 2:16 that keeps
        # turning back into a 1:16.
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


# ---------------------------------------------------------------------------
# 2. The cable: a sheath segment between two fibre points
# ---------------------------------------------------------------------------

class CableRecordTest(_Base):
    """A cable knows its own two ends, and everything else follows from that.

    Rewritten three times, each time by the operators. First (2026-08-08) the
    fibre count moved off the span onto a shared `org_cables` object, because a
    count on a section cannot say four runs are the same drum. Then (2026-08-09)
    membership moved off the topology link onto a `run`, so glass could be
    recorded between boxes with no link at all. And now the run is gone too: the
    ISPs described their plant as *fibre between two couplers, joined cable to
    cable or taken out to a device on a single fibre*, so a cable has ENDS and
    core N of it runs between them by definition.

    The deletions are the win. There is no run to double-book, no tap to project
    and no implicit continuity rule, which is why this file is shorter than the
    one it replaces while covering more.
    """

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
        # One end is not a weaker version of a cable, it is an unusable one.
        cookie = self._owner()
        st, data, _ = self._cable(cookie, name="Half", cores=12,
                                  a_device_id=self.olt)
        self.assertEqual(st, 422, data)

    def test_a_cable_may_not_run_from_a_point_back_to_itself(self):
        # Both ends would land in one tray, so every core would offer to be
        # spliced to itself and the feed walk would be asked which of two
        # identical points feeds the other.
        cookie = self._owner()
        st, data, _ = self._cable(cookie, name="Loop", cores=12,
                                  a_device_id=self.olt, b_device_id=self.olt)
        self.assertEqual(st, 422, data)

    def test_a_RENAME_need_not_restate_the_ends(self):
        # Editing the name is the commonest write this form takes; making it
        # resend the geometry is how an end gets silently moved by a stale form.
        cookie = self._owner()
        _, made, _ = self._cable(cookie, name="Main St", cores=12,
                                 a_device_id=self.olt, b_device_id=self.spl)
        st, _, _ = self._cable(cookie, id=made["id"], name="Main Street", cores=12)
        self.assertEqual(st, 200)
        cable = self._cables(cookie)[made["id"]]
        self.assertEqual(cable["name"], "Main Street")
        self.assertEqual(cable["a"]["device_id"], self.olt)

    def test_a_CUSTOMER_is_a_valid_end(self):
        # The case the ISPs added: a core may carry anything, so the customer
        # point is a coupler in its own right and a cable may end there.
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
        # A scrape can never add a subscriber and neither can this. A cable
        # landing on a typo'd sticker would draw to a point with nothing behind
        # it.
        cookie = self._owner()
        st, _, _ = self._cable(cookie, name="Ghost", cores=4,
                               a_device_id=self.spl, b_mac="DE:AD:BE:EF:00:01")
        self.assertEqual(st, 404)

    def test_an_end_in_ANOTHER_ORG_is_a_404(self):
        # The one cross-org leak a body naming two ids could produce.
        cookie = self._owner()
        auth.create_user(self.store, "ispB", "other", "otherpassword", "owner")
        theirs = self.store.create_org_device("ispB", {
            "name": "THEIRS", "ip_address": "10.9.9.9", "device_type": "OLT",
            "region": None, "parent_device_id": None})
        st, _, _ = self._cable(cookie, name="Leak", cores=12,
                               a_device_id=self.olt, b_device_id=theirs)
        self.assertEqual(st, 404)

    def test_shrinking_the_count_under_a_core_IN_USE_is_refused(self):
        # A joint naming core 19 of a cable that is now a 12F would render with a
        # tube and a colour, in full confidence, for a fibre that does not exist.
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
        # A different statement from shrinking: "we no longer know what this
        # sheath is" cannot leave strand numbers standing.
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
        # A splice is a fact about a particular closure. Carrying it across would
        # invent a splice nobody made — the same rule re-homing a drop keeps by
        # discarding its traced route.
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
        # The guard is on the end actually changing, so the form can re-save
        # idempotently — the shape `set_onu_drops` keeps.
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
        # A joint names two fibres and one of them has just stopped existing.
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
        # Three of twelve cores written down does not leave nine spare — nobody
        # wrote them down. The reply carries no free/spare key at all, so no
        # screen can start making that claim.
        cookie = self._owner()
        _, trunk, _ = self._cable(cookie, name="Trunk", cores=12,
                                  a_device_id=self.olt, b_device_id=self.spl)
        cable = self._cables(cookie)[trunk["id"]]
        self.assertEqual(cable["cores_recorded"], 0)
        for key in ("cores_free", "spare", "available"):
            self.assertNotIn(key, cable)

    def test_a_LABEL_counts_as_recorded_too(self):
        # Counting only joints printed "0 of 12 cores recorded" directly above a
        # core plainly carrying a note — the count-agreement rule broken inside
        # one card.
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
        # Matching `/api/inventory/cables` and `/api/inventory/drops`: a plant
        # record is not one of the monitoring surfaces the worker shell renders.
        # Widening it is a decision about the field role, not a side effect.
        cookie = self._login("field", "fieldpassword")
        st, _, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.spl}", None, cookie)
        self.assertEqual(st, 403)

    def test_recording_fibre_NEVER_reaches_the_engine(self):
        # The standing that makes this whole surface safe to hand to an operator
        # mid-survey. A cable is not a device: it cannot re-parent anything, and
        # it cannot rebuild an engine or re-page a fleet.
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
        # An SPA older than this central is a routine pairing — the bundle
        # deploys live and central needs a restart — and a silent 200 would leave
        # an operator watching a cable they think they recorded fail to appear.
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
    """The cable's own drawn route, and the split that opens a coupler on it."""

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
        # Crews order drum by the metre, walked segment by segment — not the
        # chord, which Mercator stretches with latitude.
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.001, 78.002]])
        self.assertAlmostEqual(self._cables(cookie)[cid]["length_m"], 323.7, places=0)

    def test_an_UNTRACED_cable_has_no_length_rather_than_zero(self):
        cookie = self._owner()
        cid = self._laid(cookie)
        self.assertIsNone(self._cables(cookie)[cid]["length_m"])

    def test_a_route_of_ONE_POINT_is_refused(self):
        # A place, not a run. Storing it would make every reader fall back to a
        # chord, which reads as "the trace did not save".
        cookie = self._owner()
        cid = self._laid(cookie)
        st, _, _ = self._req("POST", "/api/inventory/cable/path", {
            "cable_id": cid, "path": [[17.0, 78.0]]}, cookie)
        self.assertEqual(st, 422)

    def test_a_route_writes_GEOMETRY_AND_NOTHING_ELSE(self):
        # A survey must not be able to quietly restate the record it surveys.
        cookie = self._owner()
        cid = self._laid(cookie)
        self._req("POST", "/api/inventory/cable/path", {
            "cable_id": cid, "path": [[17.0, 78.0], [17.0, 78.002]],
            "name": "Renamed", "cores": 48}, cookie)
        cable = self._cables(cookie)[cid]
        self.assertEqual(cable["name"], "Main St")
        self.assertEqual(cable["cores"], 12)

    def test_OPENING_A_COUPLER_splits_the_cable_and_splices_every_core_through(self):
        # The gesture the segment model is built around: what the crew does, in
        # one click, without disturbing anything already recorded at either end.
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]])
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["spliced"], 12)
        cables = self._cables(cookie)
        self.assertEqual(len(cables), 2)
        near, far = cables[out["cable_id"]], cables[out["new_cable_id"]]
        # Both halves keep the drum's name; the ends tell them apart.
        self.assertEqual(near["name"], far["name"], "Main St")
        self.assertEqual(near["a"]["device_id"], self.olt)
        self.assertEqual(near["b"]["device_id"], out["coupler_id"])
        self.assertEqual(far["a"]["device_id"], out["coupler_id"])
        self.assertEqual(far["b"]["device_id"], self.spl)
        # …and the glass still reaches end to end.
        st, trace, _ = self._req(
            "GET", f"/api/inventory/fibre/trace?org=ispA&cable={cid}&core=7",
            None, cookie)
        self.assertEqual(st, 200, trace)
        self.assertTrue(trace["ok"])
        self.assertEqual([p["name"] for p in trace["points"]],
                         ["HILL-OLT-1", "JC-1", "SPL-1"])

    def test_the_coupler_it_makes_is_a_PASSIVE_and_reaches_no_engine(self):
        # It is the one write in the fibre half that touches org_devices, and it
        # is safe for exactly one reason: passives are excluded from
        # org_device_topology, and it is created with no parent.
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]])
        before = self.store.org_device_topology("ispA")
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200)
        self.assertEqual(self.store.org_device_topology("ispA"), before)
        made = [d for d in self.store.list_org_devices("ispA")
                if d["id"] == out["coupler_id"]][0]
        self.assertEqual(made["device_type"], "coupler")
        self.assertIsNone(made["parent_device_id"])
        self.assertEqual((made["lat"], made["lng"]), (17.0, 78.002))

    def test_splitting_an_UNTRACED_cable_is_refused(self):
        # There is no route to cut and no coordinate to stand the coupler on.
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
        # Enumerating the cores of a sheath nobody has measured would invent the
        # very fact this schema refuses to invent.
        cookie = self._owner()
        cid = self._laid(cookie, [[17.0, 78.0], [17.0, 78.002], [17.0, 78.004]],
                         cores=None)
        st, out, _ = self._req("POST", "/api/inventory/cable/split", {
            "cable_id": cid, "lat": 17.0, "lng": 78.002}, cookie)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["spliced"], 0)

    def test_a_split_carries_the_FAR_END_joints_onto_the_far_half(self):
        # Those splices were made at that closure and belong to the half that
        # still reaches it.
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
    """The tray: what is joined to what, and everything that may not be."""

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "coupler",
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
        # The only way a core is attached to equipment, which is why connecting
        # a device needs no route and no table of its own.
        st, out, _ = self._joint(device_id=self.spl, a_cable_id=self.branch,
                                 a_core_no=2)
        self.assertEqual(st, 200, out)
        self.assertTrue(out["ok"])
        tray = self._tray(self.spl)
        self.assertEqual(tray["joints"][0]["b_cable_id"], None)

    def test_a_fibre_that_does_not_END_here_is_refused_BY_NAME(self):
        # A bare 400 on a splice tray is indistinguishable from a broken button.
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
        # Nine closures in ten are exactly this, and doing it as N gestures is
        # the difference between a record that gets written and one that does
        # not. 1:1 runs to the SMALLER count — there is no honest core 13 of a 4F.
        st, out, _ = self._req("POST", "/api/inventory/fibre/through", {
            "device_id": self.jc, "a_cable_id": self.trunk,
            "b_cable_id": self.branch}, self.cookie)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["spliced"], 4)
        self.assertEqual(len(self._tray(self.jc)["joints"]), 4)

    def test_straight_through_SKIPS_what_is_already_joined_by_hand(self):
        # Pressing it twice is safe, and pressing it after hand-work leaves the
        # hand-work: the operator who deliberately crossed 3 to 1 keeps it.
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
        # A lane of houses daisy-chained down one 4F: core 1 into this one, the
        # rest passing onward. This is the case the ISPs added.
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
    """End to end, across sheaths — the question a light source is asking."""

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "coupler",
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
        # `fiber.trace` stops at a fork and names it — pinned in unit/test_fiber
        # against a hand-built graph. Through the API the fork is unreachable,
        # and that is the stronger statement: one fibre joins exactly one fibre,
        # enforced on the WRITE so an operator finds out while looking at the
        # tray rather than as a fault chip discovered later.
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
        # A box can be recorded with no parent at all — placing one stopped
        # asking what feeds it — so the chain has to be able to come from fibre.
        # DECLARED still wins: SPL-1 keeps the parent somebody typed.
        feed = self.store.org_plant_feed_map("ispA")
        self.assertEqual(feed[self.jc], self.olt)
        self.assertEqual(feed[self.spl], self.olt)

    def test_a_feed_arriving_through_a_CUSTOMER_is_dropped_not_named(self):
        # The walk follows a daisy chain correctly, but this map is device to
        # device and there is no id to name a subscriber with. No feed is the
        # honest answer, and it is what the map already says for anything
        # unreached.
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


# ---------------------------------------------------------------------------
# 3. The drop route: tracing the last hop
# ---------------------------------------------------------------------------

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
        # Straightening is a real answer, not a failure: the drop goes back to
        # the dotted chord and stops claiming to be surveyed.
        self.assertEqual(self._trace([])[0], 200)
        self.assertEqual(self._stored()["waypoints"], [])

    def test_a_drop_nobody_recorded_has_no_anchor_to_draw_from(self):
        # 404 rather than a row invented on the spot. Without a recorded
        # splitter the map draws to the OLT instead, and that line is an ADMITTED
        # GUESS ("we only know the PON") — tracing it would promote the guess
        # into surveyed geometry a crew orders drum against.
        self._onu(self.MAC)
        status, body, _ = self._trace([[17.1, 79.1]])
        self.assertEqual(status, 404, body)

    def test_re_homing_a_drop_discards_its_traced_route(self):
        # THE SHARPEST RULE HERE. That path was walked to the box the customer
        # no longer hangs off, so keeping it would leave a SOLID line — this
        # map's word for "surveyed" — running to the wrong splitter.
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
        # The other half, and the one a naive "clear on write" would break: the
        # bulk dialog re-saves its whole set every time it is used, so an
        # idempotent write must never destroy a traced drop.
        self._onu(self.MAC)
        self._attach(self.spl)
        self._trace([[17.1, 79.1]])
        self._attach(self.spl)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1]])

    def test_identity_is_norm_mac_case_insensitive_and_separator_EXACT(self):
        # The route keys on `onuroster._norm_mac`, so case and surrounding space
        # collapse...
        self._onu(self.MAC)
        self._attach(self.spl)
        self.assertEqual(self._trace([[17.1, 79.1]], mac=" aa:bb:cc:00:00:01 ")[0], 200)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1]])
        # ...and SEPARATORS DO NOT. That is deliberate and load-bearing: identity
        # here stays separator-exact because two OLTs reporting differently
        # punctuated strings really are two different values, and collapsing them
        # fabricates duplicate-MAC pages. `search_key` is the punctuation-blind
        # form and is for SEARCH only — a route must never be keyed on it.
        self.assertEqual(self._trace([[1.0, 2.0]], mac="AABBCC000001")[0], 404)
        self.assertEqual(self._stored()["waypoints"], [[17.1, 79.1]])

    def test_the_map_reply_carries_the_traced_path(self):
        # The geometry and the anchor come off ONE read of the table: fetching
        # them apart is how a line gets drawn to a splitter its waypoints no
        # longer lead to.
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
        # The map reads `drop_waypoints.length` to decide dotted vs solid; a
        # null there would be a runtime error on the commonest row there is.
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
        # ispB may not reach into ispA, and its own scope holds no such drop
        self.assertIn(status, (403, 404))
        self.assertEqual(self._stored()["waypoints"], [])



class FibreTailTest(_Base):
    """Taking ONE core out to a box that stands somewhere else.

    The half of "take a core out to a device" that had no route through this
    record at all. A strand may only be joined where its own sheath is opened —
    correct physics — so a trunk core could never reach the OLT beside the
    closure; and the single fibre that physically does reach it could not be laid
    either, because 1 was not a fibre count. Between the two, the commonest tail
    in an access network was unsayable.
    """

    def setUp(self):
        super().setUp()
        self.cookie = self._owner()
        self.jc = self.store.create_org_device("ispA", {
            "name": "JC-A", "ip_address": "", "device_type": "coupler",
            "region": None, "parent_device_id": None})
        self.far = self.store.create_org_device("ispA", {
            "name": "JC-B", "ip_address": "", "device_type": "coupler",
            "region": None, "parent_device_id": None})
        # A trunk that PASSES the closure: neither of its ends is the OLT, which
        # is the whole situation this gesture exists for.
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
        # …and the plain termination it replaces is still refused, which is why
        # this route has to exist rather than the rule being loosened.
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
        # UNTRACED on purpose: nobody surveys the metres from a closure to the
        # rack beside it, and an empty path draws the dashed chord — this map's
        # own word for "recorded, not walked". A length would be a measurement.
        self.assertEqual(tail["path"], [])
        self.assertIsNone(tail["length_m"])
        # spliced here, landed there
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
        self.assertEqual({h["cable_name"] for h in tr["hops"]},
                         {"Trunk", "Trunk core 7 → HILL-OLT-1"})
        self.assertIn("HILL-OLT-1", [e and e["point"]["name"] for e in tr["ends"]])

    def test_eight_tails_to_one_OLT_are_told_apart_by_their_core(self):
        # An 8-PON OLT fed off one closure. Named for the two POINTS alone all
        # eight are the same string, and the OLT's own picker offers eight
        # identical rows — so the source core is in the name.
        for core in range(1, 9):
            _, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                   a_core_no=core, to_device_id=self.olt)
            self.assertTrue(out["ok"], out)
        names = [c["name"] for c in self._cables() if c["cores"] == 1]
        self.assertEqual(len(set(names)), 8, names)

    def test_a_fibre_already_spoken_for_is_refused_and_lays_NOTHING(self):
        self._tail(device_id=self.jc, a_cable_id=self.trunk, a_core_no=7,
                   to_device_id=self.olt)
        before = len(self._cables())
        st, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                a_core_no=7, to_device_id=self.spl)
        self.assertEqual(st, 200)
        self.assertFalse(out["ok"])
        self.assertEqual(out["refused"], "taken")
        # A refused tail that still laid its cable would leave a line on the map
        # the operator would reasonably read as the connection having been made.
        self.assertEqual(len(self._cables()), before)

    def test_a_core_that_is_not_open_here_is_refused(self):
        # The trunk does not end at the splitter, so nothing of it can be taken
        # out there — the same physics the plain termination enforces.
        _, out, _ = self._tail(device_id=self.spl, a_cable_id=self.trunk,
                               a_core_no=3, to_device_id=self.olt)
        self.assertFalse(out["ok"])
        self.assertEqual(out["refused"], "absent")

    def test_a_tail_to_the_point_it_leaves_is_refused(self):
        st, out, _ = self._tail(device_id=self.jc, a_cable_id=self.trunk,
                                a_core_no=3, to_device_id=self.jc)
        self.assertEqual(st, 422, out)

    def test_a_box_that_is_gone_is_refused_rather_than_cabled_to(self):
        # The far end is picked from a list the browser may have held for a
        # while, so the box can be gone by the time the click lands.
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
        # and the splice it made at the closure goes with the cable, or the tray
        # would show core 7 joined to a sheath that no longer exists
        _, here, _ = self._req(
            "GET", f"/api/inventory/fibre?org=ispA&device={self.jc}", None,
            self.cookie)
        self.assertEqual(here["joints"], [])

    def test_undoing_it_is_the_ordinary_clear(self):
        # No special reverse gesture: the splice it made is a splice, so the
        # tray's own clear undoes it like any other.
        self._tail(device_id=self.jc, a_cable_id=self.trunk, a_core_no=7,
                   to_device_id=self.olt)
        st, out, _ = self._req("POST", "/api/inventory/fibre/clear", {
            "device_id": self.jc, "cable_id": self.trunk, "core_no": 7},
            self.cookie)
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
