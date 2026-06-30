"""Hourly rollup tests (core/rollup.roll_up).

The rollup tier folds raw poll_results into one compact row per device per hour so
trend charts don't scan a billion raw rows. These check the aggregation math, that
the in-progress hour is left alone, idempotency (a double run never double-counts),
and the services reader that charts consume.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from wisp.config import Config
from wisp.core.rollup import roll_up
from wisp.database.client import connect, migrate
from wisp.server import services


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class RollupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)
        # Pin "now" to the top of an hour so "the in-progress hour" is unambiguous.
        self.now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address) VALUES (1,'n1','d1')")
            c.execute("INSERT INTO devices (id,name,ip_address) VALUES (2,'n2','d2')")
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _poll(self, dev, when, latency, loss, state):
        with connect(self.cfg) as c:
            c.execute(
                "INSERT INTO poll_results (device_id,timestamp,latency_ms,packet_loss,state)"
                " VALUES (?,?,?,?,?)", (dev, _iso(when), latency, loss, state))
            c.commit()

    def _rollups(self):
        with connect(self.cfg) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM poll_rollups ORDER BY device_id, bucket")]

    def test_aggregates_a_closed_hour(self):
        prev = self.now - timedelta(hours=1)            # a fully closed hour
        # device 1: three polls, one a 100%-loss DOWN sample (NULL latency)
        self._poll(1, prev + timedelta(minutes=5), 10.0, 0.0, "UP")
        self._poll(1, prev + timedelta(minutes=25), 30.0, 0.0, "UP")
        self._poll(1, prev + timedelta(minutes=45), None, 100.0, "DOWN")
        # device 2: one healthy poll
        self._poll(2, prev + timedelta(minutes=10), 20.0, 0.0, "UP")

        written = roll_up(self.cfg, now=self.now)
        self.assertEqual(written, 2)                     # one row per device-hour

        rows = {r["device_id"]: r for r in self._rollups()}
        d1 = rows[1]
        self.assertEqual(d1["samples"], 3)
        self.assertEqual(d1["bucket"], _iso(prev))       # pinned to top of the hour
        self.assertAlmostEqual(d1["latency_avg"], 20.0)  # NULL latency ignored -> (10+30)/2
        self.assertEqual(d1["latency_min"], 10.0)
        self.assertEqual(d1["latency_max"], 30.0)
        self.assertAlmostEqual(d1["loss_avg"], 100.0 / 3)
        self.assertEqual(d1["down_polls"], 1)
        self.assertEqual(d1["up_polls"], 2)
        self.assertEqual(rows[2]["samples"], 1)

    def test_in_progress_hour_is_not_rolled(self):
        # A poll in the current (still-open) hour must be left for the next run.
        self._poll(1, self.now + timedelta(minutes=5), 10.0, 0.0, "UP")
        self.assertEqual(roll_up(self.cfg, now=self.now), 0)
        self.assertEqual(self._rollups(), [])

    def test_idempotent_double_run(self):
        prev = self.now - timedelta(hours=1)
        self._poll(1, prev + timedelta(minutes=5), 10.0, 0.0, "UP")
        self.assertEqual(roll_up(self.cfg, now=self.now), 1)
        # Second run over the same closed hour writes nothing (watermark + OR IGNORE).
        self.assertEqual(roll_up(self.cfg, now=self.now), 0)
        self.assertEqual(len(self._rollups()), 1)

    def test_device_trend_reader(self):
        prev = self.now - timedelta(hours=1)
        self._poll(1, prev + timedelta(minutes=5), 10.0, 0.0, "UP")
        self._poll(1, prev + timedelta(minutes=35), None, 100.0, "DOWN")
        roll_up(self.cfg, now=self.now)
        series = services.device_trend(self.cfg, device_id=1, hours=24)
        self.assertEqual(len(series), 1)
        point = series[0]
        self.assertEqual(point["samples"], 2)
        self.assertEqual(point["uptime_pct"], 50.0)       # 1 of 2 polls UP
        self.assertEqual(point["down_polls"], 1)

    # --- Phase 10 Part A: the fold also queues its rows for the central shipper ---
    def _outbox(self):
        with connect(self.cfg) as c:
            return c.execute("SELECT kind, payload FROM outbox ORDER BY id").fetchall()

    def test_central_disabled_enqueues_nothing(self):
        prev = self.now - timedelta(hours=1)
        self._poll(1, prev + timedelta(minutes=5), 10.0, 0.0, "UP")
        roll_up(self.cfg, now=self.now)                   # central_url empty -> dormant
        self.assertEqual(self._outbox(), [])

    def test_central_enabled_enqueues_rollups_in_fold_txn(self):
        prev = self.now - timedelta(hours=1)
        self._poll(1, prev + timedelta(minutes=5), 10.0, 0.0, "UP")
        self._poll(2, prev + timedelta(minutes=5), 20.0, 0.0, "UP")
        cfg = Config(db_path=self.cfg.db_path, central_url="https://central.test")
        self.assertEqual(roll_up(cfg, now=self.now), 2)
        rows = self._outbox()
        self.assertEqual([r["kind"] for r in rows], ["rollup", "rollup"])
        import json
        rec = json.loads(rows[0]["payload"])
        self.assertEqual(rec["type"], "Rollup")
        self.assertIn("bucket", rec)
        self.assertIn("device_id", rec)


if __name__ == "__main__":
    unittest.main()
