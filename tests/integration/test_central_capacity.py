import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import api, auth, server as server_mod
from wisp.central.api import capacity as capacity_api
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.central.store_capacity import CapacityStoreMixin
from wisp.central.store_history import DAY_S, HOUR_S
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"

ORG_ROUTE = "/api/history/capacity"
PORT_ROUTE = "/api/history/port"


# CapacityStoreMixin is composed into CentralStore's bases; re-composing it
# here would be an MRO conflict.
_Store = CentralStore
assert issubclass(_Store, CapacityStoreMixin)


class _HttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = _Store(self.cfg.central_db)
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        # Stamped at CALL time — discovery imports every test file up front, so
        # an import-time "now" is already stale by the time the file runs.
        self.day = (int(time.time()) // DAY_S) * DAY_S - 3 * DAY_S
        # The window clamps to history_since, which __init__ stamps at "now" —
        # so a store that has been recording for days has to SAY so, or every
        # fixture bucket sits before recording began and is honestly excluded.
        self._recording_since(self.day - DAY_S)
        self.sw = self.store.create_org_device(ORG, {
            "name": "HLY-SW", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": "north", "parent_device_id": None})
        self.olt = self.store.create_org_device(ORG, {
            "name": "PDVR-OLT", "ip_address": "10.0.0.3", "device_type": "olt",
            "region": "north", "parent_device_id": None})

        # The orchestrator wires these rows into api/__init__.py and
        # server._WORKER_GET; the tests wire them locally so this workstream
        # stands on its own, and restore whatever was there on the way out.
        self._routes = {p: api.GET.get(p) for p in (ORG_ROUTE, PORT_ROUTE)}
        api.GET[ORG_ROUTE] = capacity_api.capacity
        api.GET[PORT_ROUTE] = capacity_api.port_history
        self._worker_get = PORT_ROUTE in server_mod._WORKER_GET
        server_mod._WORKER_GET.add(PORT_ROUTE)

        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        for path, prior in self._routes.items():
            if prior is None:
                api.GET.pop(path, None)
            else:
                api.GET[path] = prior
        if not self._worker_get:
            server_mod._WORKER_GET.discard(PORT_ROUTE)
        self.tmp.cleanup()

    # -- fixtures ------------------------------------------------------------

    def _recording_since(self, epoch_s):
        stamp = datetime.fromtimestamp(int(epoch_s), tz=timezone.utc).isoformat(
            timespec="seconds")
        with self.store._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value)"
                         " VALUES ('history_since', ?)", (stamp,))
            conn.commit()

    def _walk(self, device_id, if_index, *, name=None, alias=None,
              oper="up", admin="up"):
        self.store.upsert_switch_port(
            ORG, device_id, if_index, name or f"GE0/{if_index}", alias,
            admin, oper, None, 0, False, None, "2026-08-14T05:00:00")

    def _mark(self, device_id, if_index, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.store._connect() as conn:
            conn.execute(
                f"UPDATE switch_ports SET {sets}"
                " WHERE org_id=? AND device_id=? AND if_index=?",
                (*cols.values(), ORG, device_id, if_index))
            conn.commit()

    def _sweep(self, device_id, if_index, day_offset, hour, minute, in_bps,
               out_bps=1.0e6, up=True):
        ts = self.day + day_offset * DAY_S + hour * HOUR_S + minute * 60
        self.store.record_port_sweeps(ORG, device_id, ts,
                                      [(if_index, in_bps, out_bps, up)])

    def _uid(self, username):
        return next(u["id"] for u in self.store.list_users(ORG)
                    if u["username"] == username)

    # -- transport -----------------------------------------------------------

    def _req(self, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data else {})

    def _login(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username,
                                      "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _capacity(self, days=None, cookie=None):
        # The window is anchored on the fixture's own day, which is in the
        # past relative to the test clock — ask for enough days to cover it.
        q = f"?days={days}" if days is not None else ""
        return self._req(f"{ORG_ROUTE}{q}", cookie or self._login())


class GateTest(_HttpBase):
    def test_the_org_ranking_is_owner_only(self):
        # Layer one: the route is not in _WORKER_GET, so a worker never
        # reaches the handler at all.
        st, _ = self._req(ORG_ROUTE, self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)

    def test_the_handlers_own_gate_refuses_a_worker_if_the_route_ever_opens(self):
        # Layer two, and the one that has to hold: reaching a route says
        # nothing about being allowed to enumerate the org's whole port list.
        server_mod._WORKER_GET.add(ORG_ROUTE)
        try:
            st, body = self._req(ORG_ROUTE,
                                 self._login("field", "fieldpassword"))
        finally:
            server_mod._WORKER_GET.discard(ORG_ROUTE)
        self.assertEqual(st, 403)
        self.assertEqual(body["error"], "owner only")

    def test_signing_out_is_a_401_not_an_empty_ranking(self):
        self.assertEqual(self._req(ORG_ROUTE)[0], 401)

    def test_the_per_port_drill_refuses_a_device_a_worker_is_not_assigned(self):
        self._walk(self.sw, 1)
        self._mark(self.sw, 1, monitored=1)
        st, _ = self._req(f"{PORT_ROUTE}?device_id={self.sw}&if_index=1",
                          self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)

    def test_the_per_port_drill_reaches_an_assigned_device(self):
        self._walk(self.sw, 1)
        self._mark(self.sw, 1, monitored=1)
        self.store.set_device_assignees(ORG, self.sw, [self._uid("field")],
                                        "owner")
        st, body = self._req(f"{PORT_ROUTE}?device_id={self.sw}&if_index=1",
                             self._login("field", "fieldpassword"))
        self.assertEqual(st, 200)
        self.assertEqual(body["device_id"], self.sw)

    def test_a_missing_if_index_is_a_400_not_a_guess(self):
        st, _ = self._req(f"{PORT_ROUTE}?device_id={self.sw}", self._login())
        self.assertEqual(st, 400)


class RankingTest(_HttpBase):
    def setUp(self):
        super().setUp()
        # uplink: a real ceiling, evening peak at 20:00 on two days
        self._walk(self.sw, 1, name="GE0/1", alias="uplink")
        self._mark(self.sw, 1, monitored=1, bw_max_mbps=1000.0,
                   bw_direction="in")
        for d in (0, 1):
            self._sweep(self.sw, 1, d, 3, 5, 20e6)
            self._sweep(self.sw, 1, d, 20, 5, 800e6)
            self._sweep(self.sw, 1, d, 20, 10, 900e6)
        # a fed port with NO ceiling recorded
        self._walk(self.olt, 2, name="GE0/2")
        self._mark(self.olt, 2, feeds_device_id=self.sw)
        self._sweep(self.olt, 2, 0, 21, 5, 400e6)
        # a bare walked port: eligible for nothing, sampled by nothing
        self._walk(self.sw, 9, name="GE0/9")

    def test_only_eligible_ports_are_ranked(self):
        st, body = self._capacity(days=30)
        self.assertEqual(st, 200)
        keys = {(r["device_id"], r["if_index"]) for r in body["ranking"]}
        self.assertEqual(keys, {(self.sw, 1), (self.olt, 2)})
        self.assertEqual(body["eligible"], 2)

    def test_the_busy_hour_is_the_heatmaps_own_darkest_cell(self):
        st, body = self._capacity(days=30)
        row = next(r for r in body["ranking"] if r["if_index"] == 1)
        cells = next(hm["cells"] for hm in body["heatmap"]
                     if hm["if_index"] == 1)
        darkest = max(cells, key=lambda c: c["in_bps"])
        self.assertEqual(row["busy_in_bps"], darkest["in_bps"])
        self.assertEqual(row["busy_in_hour"], darkest["h"])
        self.assertEqual(row["busy_in_hour"], 20)
        self.assertEqual(row["busy_in_bps"], 850e6)     # (800+900)/2

    def test_utilisation_reads_the_declared_direction_against_the_ceiling(self):
        st, body = self._capacity(days=30)
        row = next(r for r in body["ranking"] if r["if_index"] == 1)
        self.assertEqual(row["bw_direction"], "in")
        self.assertEqual(row["busy_bps"], 850e6)
        self.assertEqual(row["util_pct"], 85.0)

    def test_a_port_with_no_ceiling_recorded_gets_no_percentage(self):
        st, body = self._capacity(days=30)
        row = next(r for r in body["ranking"] if r["if_index"] == 2)
        self.assertIsNone(row["bw_max_mbps"])
        self.assertIsNone(row["util_pct"])
        self.assertEqual(row["busy_in_bps"], 400e6)     # still measured
        self.assertEqual(body["no_ceiling"], 1)

    def test_a_measured_ceiling_outranks_a_bigger_unjudgeable_rate(self):
        # the 400 Mb/s port has no ceiling, so it sorts below the one that can
        # be judged against one — "most pinned" needs a denominator
        st, body = self._capacity(days=30)
        self.assertEqual([r["if_index"] for r in body["ranking"]], [1, 2])

    def test_the_shaded_cell_is_the_number_printed_beside_it(self):
        # The heatmap ships the direction-resolved rate, so the darkest cell
        # of a row IS that row's busy figure — no second rule in the SPA.
        st, body = self._capacity(days=30)
        row = next(r for r in body["ranking"] if r["if_index"] == 1)
        cells = next(hm["cells"] for hm in body["heatmap"]
                     if hm["if_index"] == 1)
        self.assertEqual(max(c["bps"] for c in cells), row["busy_bps"])
        # direction 'in' on this port: the shaded value ignores the upload
        self.assertEqual([c["bps"] for c in cells],
                         [c["in_bps"] for c in cells])

    def test_the_heatmap_is_a_prefix_of_the_ranking(self):
        st, body = self._capacity(days=30)
        keys = [(r["device_id"], r["if_index"]) for r in body["ranking"]]
        heat = [(hm["device_id"], hm["if_index"]) for hm in body["heatmap"]]
        self.assertEqual(heat, keys[:len(heat)])
        self.assertLessEqual(len(heat), body["heatmap_ports"])

    def test_an_hour_nobody_walked_is_absent_never_a_zero_cell(self):
        st, body = self._capacity(days=30)
        cells = next(hm["cells"] for hm in body["heatmap"]
                     if hm["if_index"] == 1)
        self.assertEqual(sorted(c["h"] for c in cells), [3, 20])
        self.assertTrue(all(c["n"] > 0 for c in cells))

    def test_coverage_rides_along_with_every_row(self):
        st, body = self._capacity(days=30)
        row = next(r for r in body["ranking"] if r["if_index"] == 1)
        self.assertEqual(row["days"], 2)
        self.assertEqual(row["samples"], 6)
        self.assertEqual(row["rate_n"], 6)
        self.assertEqual(row["peak_in_bps"], 900e6)
        self.assertTrue(body["recording_since"])


class YoungRecordTest(_HttpBase):
    def test_an_eligible_port_with_no_samples_is_reported_not_hidden(self):
        self._walk(self.sw, 1)
        self._mark(self.sw, 1, monitored=1, bw_max_mbps=100.0)
        st, body = self._capacity(days=30)
        self.assertEqual(st, 200)
        row = body["ranking"][0]
        self.assertEqual(row["days"], 0)
        self.assertIsNone(row["busy_in_bps"])
        self.assertIsNone(row["util_pct"])       # nothing to divide
        self.assertEqual(body["eligible"], 1)
        self.assertEqual(body["sampled"], 0)
        self.assertEqual(body["heatmap"], [])

    def test_a_walked_hour_that_computed_no_rate_reports_coverage_only(self):
        self._walk(self.sw, 1)
        self._mark(self.sw, 1, monitored=1)
        self._sweep(self.sw, 1, 0, 8, 5, None, out_bps=None)
        st, body = self._capacity(days=30)
        row = body["ranking"][0]
        self.assertEqual(row["samples"], 1)
        self.assertEqual(row["rate_n"], 0)
        self.assertEqual(row["days"], 1)
        self.assertIsNone(row["busy_in_bps"])
        self.assertIsNone(row["busy_in_hour"])
        self.assertEqual(body["sampled"], 0)


class WindowTest(_HttpBase):
    def test_the_ask_clamps_to_what_the_hour_tier_keeps(self):
        st, body = self._capacity(days=365)
        self.assertEqual(st, 200)
        self.assertEqual(body["days_requested"], 365)
        self.assertEqual(body["days"], self.cfg.hist_port_hour_days)
        self.assertTrue(body["clamped"])
        self.assertEqual(body["max_days"], self.cfg.hist_port_hour_days)

    def test_a_window_inside_the_retention_is_served_whole(self):
        st, body = self._capacity(days=7)
        self.assertEqual(body["days"], 7)
        self.assertFalse(body["clamped"])

    def test_the_per_port_drill_clamps_on_the_same_rule(self):
        self._walk(self.sw, 1)
        self._mark(self.sw, 1, monitored=1)
        st, body = self._req(
            f"{PORT_ROUTE}?device_id={self.sw}&if_index=1&days=365",
            self._login())
        self.assertEqual(st, 200)
        self.assertEqual(body["days"], self.cfg.hist_port_hour_days)
        self.assertTrue(body["clamped"])


class DrillAgreementTest(_HttpBase):
    def setUp(self):
        super().setUp()
        self._walk(self.sw, 1, name="GE0/1")
        self._mark(self.sw, 1, monitored=1, bw_max_mbps=1000.0)
        for d, peak in ((0, 600e6), (1, 900e6)):
            self._sweep(self.sw, 1, d, 2, 5, 30e6)
            self._sweep(self.sw, 1, d, 20, 5, peak)

    def test_the_drill_reports_the_ranking_rows_own_busy_hour(self):
        cookie = self._login()
        _, org = self._capacity(days=30, cookie=cookie)
        row = next(r for r in org["ranking"] if r["if_index"] == 1)
        _, drill = self._req(
            f"{PORT_ROUTE}?device_id={self.sw}&if_index=1&days=30", cookie)
        self.assertEqual(drill["busy_in_bps"], row["busy_in_bps"])
        self.assertEqual(drill["busy_in_hour"], row["busy_in_hour"])
        self.assertEqual(drill["busy_in_bps"], 750e6)     # (600+900)/2

    def test_the_drills_hours_are_the_heatmaps_cells(self):
        cookie = self._login()
        _, org = self._capacity(days=30, cookie=cookie)
        cells = next(hm["cells"] for hm in org["heatmap"] if hm["if_index"] == 1)
        _, drill = self._req(
            f"{PORT_ROUTE}?device_id={self.sw}&if_index=1&days=30", cookie)
        self.assertEqual([(c["h"], c["in_bps"]) for c in drill["hours"]],
                         [(c["h"], c["in_bps"]) for c in cells])

    def test_the_drill_grades_the_ceiling_exactly_as_the_ranking_does(self):
        cookie = self._login()
        _, org = self._capacity(days=30, cookie=cookie)
        row = next(r for r in org["ranking"] if r["if_index"] == 1)
        _, drill = self._req(
            f"{PORT_ROUTE}?device_id={self.sw}&if_index=1&days=30", cookie)
        for key in ("bw_max_mbps", "bw_direction", "busy_bps", "busy_hour",
                    "util_pct", "label"):
            self.assertEqual(drill[key], row[key], key)
        self.assertEqual(drill["util_pct"], 75.0)

    def test_a_total_direction_port_is_graded_on_the_server_not_reconstructed(self):
        # busiest hour of (in + out) is NOT the sum of each direction's own
        # busiest hour, so the SPA could never rebuild this from the two
        # per-direction figures. The reply carries the answer.
        # 06:00 is nobody's busiest direction on its own (400 in, 500 out,
        # against 750 down at 20:00) and IS the busiest combined hour at 900.
        self._mark(self.sw, 1, bw_direction="total")
        self._sweep(self.sw, 1, 0, 6, 5, 400e6, out_bps=500e6)
        cookie = self._login()
        _, drill = self._req(
            f"{PORT_ROUTE}?device_id={self.sw}&if_index=1&days=30", cookie)
        self.assertEqual(drill["bw_direction"], "total")
        self.assertEqual(drill["busy_hour"], 6)          # 400 + 500 combined
        self.assertEqual(drill["busy_bps"], 900e6)
        self.assertEqual(drill["busy_in_bps"], 750e6)    # 20:00, the other hour
        self.assertNotEqual(drill["busy_hour"], drill["busy_in_hour"])

    def test_the_day_series_names_each_days_own_busiest_hour(self):
        _, drill = self._req(
            f"{PORT_ROUTE}?device_id={self.sw}&if_index=1&days=30",
            self._login())
        series = drill["series"]
        self.assertEqual(len(series), 2)
        self.assertEqual([d["busy_in_hour"] for d in series], [20, 20])
        self.assertEqual([d["busy_in_bps"] for d in series], [600e6, 900e6])
        self.assertEqual(series[0]["day"] % 86400, 0)
        self.assertEqual(drill["days_covered"], 2)


if __name__ == "__main__":
    unittest.main()
