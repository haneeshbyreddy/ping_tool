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

from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"


def _iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


class _HttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        self.now = datetime.now(timezone.utc)
        self.dev = self.store.create_org_device(ORG, {
            "name": "CH-SW", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": "north", "parent_device_id": None})
        self.other = self.store.create_org_device(ORG, {
            "name": "FAR-SW", "ip_address": "10.0.0.3", "device_type": "switch",
            "region": "north", "parent_device_id": None})
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
        self.tmp.cleanup()

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
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _outage(self, device_id, start, end=None, state="DOWN", acked=None):
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at,"
                " resolved_at, final_state, acknowledged_at)"
                " VALUES (?,?,?,?,?,?)",
                (ORG, device_id, _iso(start),
                 _iso(end) if end else None, state,
                 _iso(acked) if acked else None))
            conn.commit()


class ReliabilityDeviceTest(_HttpBase):
    def test_the_strip_folds_an_outage_across_utc_days(self):
        # 23:30 -> 01:30 the next day: 30 min on day one, 90 min on day two
        day = (self.now - timedelta(days=3)).replace(
            hour=23, minute=30, second=0, microsecond=0)
        self._outage(self.dev, day, day + timedelta(hours=2))
        st, body = self._req(f"/api/history/reliability?device_id={self.dev}"
                             "&days=30", self._login())
        self.assertEqual(st, 200)
        downs = {d["day"]: d["down_s"] for d in body["days"]}
        self.assertEqual(sorted(downs.values()), [1800, 5400])
        self.assertEqual(len(body["spans"]), 1)
        self.assertEqual(body["spans"][0]["duration_s"], 7200)
        self.assertTrue(body["recording_since"])

    def test_an_unreachable_span_is_listed_but_never_counts_as_downtime(self):
        start = self.now - timedelta(days=2)
        self._outage(self.dev, start, start + timedelta(hours=1),
                     state="UNREACHABLE")
        st, body = self._req(f"/api/history/reliability?device_id={self.dev}"
                             "&days=30", self._login())
        self.assertEqual(st, 200)
        self.assertEqual(body["days"], [])
        self.assertEqual(body["spans"][0]["final_state"], "UNREACHABLE")

    def test_coverage_days_come_from_the_rollups(self):
        bucket = _iso((self.now - timedelta(days=1)).replace(
            minute=0, second=0, microsecond=0))
        self.store.fold_device_rollups(
            [(ORG, self.dev, bucket, 5.0, 0.0, 0)] * 3)
        st, body = self._req(f"/api/history/reliability?device_id={self.dev}"
                             "&days=30", self._login())
        self.assertEqual(st, 200)
        self.assertEqual(len(body["coverage"]), 1)
        self.assertEqual(body["coverage"][0]["samples"], 3)

    def test_a_worker_sees_only_assigned_devices(self):
        # unassigned org: for_device narrows to nobody, so the worker is
        # refused outright (the 2026-08-12 visibility rule)
        st, _ = self._req(f"/api/history/reliability?device_id={self.dev}"
                          "&days=30", self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)


class ReliabilityOrgTest(_HttpBase):
    def test_weekly_stats_carry_ttr_and_tta_percentiles(self):
        base = self.now - timedelta(days=10)
        for i, minutes in enumerate((10, 30, 120)):
            start = base + timedelta(hours=i * 6)
            self._outage(self.dev if i % 2 else self.other, start,
                         start + timedelta(minutes=minutes),
                         acked=start + timedelta(minutes=5))
        st, body = self._req("/api/history/reliability?days=60", self._login())
        self.assertEqual(st, 200)
        weeks = body["weeks"]
        self.assertEqual(sum(w["outages"] for w in weeks), 3)
        wk = next(w for w in weeks if w["outages"] == 3)
        self.assertEqual(wk["ttr_p50_s"], 30 * 60)
        self.assertEqual(wk["tta_p50_s"], 5 * 60)
        self.assertEqual(wk["week"] % 86400, 0)

    def test_downtime_splits_at_the_thirty_minute_line(self):
        # The Home panel's whole point: a week's outage COUNT and its downtime
        # answer different questions. 10 min is a flap, 30 and 120 are jobs —
        # and 30 lands ON the boundary, which counts as long.
        base = self.now - timedelta(days=10)
        for i, minutes in enumerate((10, 30, 120)):
            start = base + timedelta(hours=i * 6)
            self._outage(self.dev, start, start + timedelta(minutes=minutes))
        st, body = self._req("/api/history/reliability?days=60", self._login())
        self.assertEqual(st, 200)
        wk = next(w for w in body["weeks"] if w["outages"] == 3)
        self.assertEqual(wk["long_outages"], 2)
        self.assertEqual(wk["down_long_s"], (30 + 120) * 60)
        self.assertEqual(wk["down_short_s"], 10 * 60)

    def test_an_unreachable_span_never_reaches_the_downtime_bands(self):
        # Same rule device_reliability keeps: an UNREACHABLE span is a
        # parent's outage restated on its victims, so it is not our downtime.
        start = self.now - timedelta(days=5)
        self._outage(self.dev, start, start + timedelta(hours=3),
                     state="UNREACHABLE")
        st, body = self._req("/api/history/reliability?days=60", self._login())
        self.assertEqual(st, 200)
        self.assertEqual(sum(w["down_long_s"] + w["down_short_s"]
                             for w in body["weeks"]), 0)

    def test_downtime_spreads_across_every_week_the_outage_covered(self):
        # A 105 h fault dumped whole into the week it OPENED misreports every
        # week it actually ran through (byreddy's week 26, the case that drove
        # this). The count still belongs to the opening week; the hours do not.
        start = (self.now - timedelta(days=12)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        # Walk to the Monday after `start`, then stop 6 h past it: the outage
        # straddles exactly one week boundary whatever weekday `now` is.
        nxt_monday = start + timedelta(days=7 - start.weekday())
        nxt_monday = nxt_monday.replace(hour=0)
        self._outage(self.dev, start, nxt_monday + timedelta(hours=6))
        st, body = self._req("/api/history/reliability?days=60", self._login())
        self.assertEqual(st, 200)
        weeks = {w["week"]: w for w in body["weeks"]}
        self.assertEqual(len(weeks), 2)
        opened = [w for w in weeks.values() if w["outages"] == 1]
        self.assertEqual(len(opened), 1, "the COUNT stays in the opening week")
        # Every second is banked, and the second week gets exactly its 6 h.
        later = max(weeks)
        self.assertEqual(weeks[later]["outages"], 0)
        self.assertEqual(weeks[later]["down_long_s"], 6 * 3600)
        self.assertEqual(sum(w["down_long_s"] for w in weeks.values()),
                         int((nxt_monday + timedelta(hours=6) - start)
                             .total_seconds()))
        self.assertEqual(sum(w["down_short_s"] for w in weeks.values()), 0)

    def test_the_org_view_is_owner_only(self):
        st, _ = self._req("/api/history/reliability?days=30",
                          self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)


class OnuTrendTest(_HttpBase):
    def _sample(self, device_id, ts, crit):
        from wisp.central import history
        acc = history.OpticsAccumulator()
        acc.add("EPON0/1", "online", -18.0, "ok")
        for _ in range(crit):
            acc.add("EPON0/1", "online", -28.5, "crit")
        acc.add("EPON0/2", "online", -25.0, "warn")
        history.record_optics(self.store, self.cfg, ORG, device_id, ts, acc)

    def test_buckets_sum_across_olts(self):
        # inside the recording_since clamp — a sample from before the
        # historian existed is deliberately outside the window
        ts = _iso(self.now)
        self._sample(self.dev, ts, crit=2)
        self._sample(self.other, ts, crit=1)
        st, body = self._req("/api/history/onus?days=14", self._login())
        self.assertEqual(st, 200)
        self.assertEqual(len(body["buckets"]), 1)
        b = body["buckets"][0]
        self.assertEqual(b["olts"], 2)
        self.assertEqual(b["crit"], 3)
        self.assertEqual(b["warn"], 2)
        self.assertEqual(b["onus"], 7)
        self.assertTrue(body["recording_since"])
        # the window clamps to the historian's own start, floored to the
        # bucket grid so the partial first hour still ships
        rec = datetime.fromisoformat(body["recording_since"][:19])
        floor = rec.replace(minute=0, second=0)
        self.assertEqual(body["since"], floor.isoformat(timespec="seconds"))

    def test_owner_only(self):
        st, _ = self._req("/api/history/onus?days=14",
                          self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)


class PagingTest(_HttpBase):
    def test_counts_group_by_day_kind_and_status(self):
        ts1 = _iso(self.now - timedelta(days=2))
        ts2 = _iso(self.now - timedelta(days=1))
        for ts, kind, status, n in ((ts1, "PON_FAULT", "suppressed", 3),
                                    (ts1, "DEVICE_DOWN", "sent", 2),
                                    (ts2, None, "sent", 1)):
            for _ in range(n):
                self.store.log_alert(ORG, None, self.dev, "whatsapp", "x",
                                     status, "p", ts, kind)
        st, body = self._req("/api/history/paging?days=30", self._login())
        self.assertEqual(st, 200)
        rows = {(r["day"], r["kind"], r["status"]): r["n"]
                for r in body["rows"]}
        self.assertEqual(rows[(ts1[:10], "PON_FAULT", "suppressed")], 3)
        self.assertEqual(rows[(ts1[:10], "DEVICE_DOWN", "sent")], 2)
        # the pre-kind era ships as '' so the chart can label it, not drop it
        self.assertEqual(rows[(ts2[:10], "", "sent")], 1)

    def test_the_ledger_is_owner_only(self):
        st, _ = self._req("/api/history/paging?days=30",
                          self._login("field", "fieldpassword"))
        self.assertEqual(st, 403)
