"""Central shipper tests (Phase 10 Part A): the drain/ack/backoff/heartbeat/evict logic,
driven against a temp DB with an injected recording-shipper double — NO real network to
central, exactly like the recording-notifier doubles elsewhere in the suite."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import connect, migrate, transaction
from wisp.database import outbox
from wisp.egress.shipper import ShipResult, ShipperWorker, WIRE_V, start_shipper_thread

TS = "2026-06-30T12:00:00+00:00"


class RecordingShipper:
    def __init__(self, ok=True, accept="all"):
        self.ok = ok
        self.accept = accept           # "all" | "first" | "none"
        self.batches: list[dict] = []
        self.heartbeats: list[dict] = []

    def ship(self, envelope) -> ShipResult:
        self.batches.append(envelope)
        if not self.ok:
            return ShipResult(False, [], 0, "central down")
        ids = [r["id"] for r in envelope["records"]]
        accepted = {"all": ids, "first": ids[:1], "none": []}[self.accept]
        return ShipResult(True, accepted, 200, "")

    def heartbeat(self, envelope) -> ShipResult:
        self.heartbeats.append(envelope)
        return ShipResult(self.ok, [], 200 if self.ok else 0, "")


class ShipperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db",
                          central_url="https://central.test",
                          central_token="tok", tenant_id="ispA", node_id="edge-1",
                          ship_batch=10, outbox_max_rows=4)
        migrate(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _enqueue(self, kind, n=1):
        with connect(self.cfg) as c:
            with transaction(c):
                for i in range(n):
                    outbox._enqueue(c, kind, {"k": kind, "i": i}, TS)

    def _outbox_ids(self):
        with connect(self.cfg) as c:
            return [r["id"] for r in c.execute("SELECT id FROM outbox ORDER BY id")]

    # --- drain ---
    def test_drain_ships_and_deletes_on_ack(self):
        self._enqueue("event", 3)
        ship = RecordingShipper(ok=True)
        worker = ShipperWorker(self.cfg, ship)
        healthy, n = worker.drain_once()
        self.assertTrue(healthy)
        self.assertEqual(n, 3)
        self.assertEqual(self._outbox_ids(), [])          # acked rows gone
        env = ship.batches[0]
        self.assertEqual(env["v"], WIRE_V)
        self.assertEqual((env["tenant_id"], env["node_id"]), ("ispA", "edge-1"))
        self.assertEqual(env["kind"], "batch")
        self.assertEqual(env["records"][0]["kind"], "event")
        self.assertIn("body", env["records"][0])

    def test_drain_empty_is_healthy_no_send(self):
        ship = RecordingShipper()
        worker = ShipperWorker(self.cfg, ship)
        self.assertEqual(worker.drain_once(), (True, 0))
        self.assertEqual(ship.batches, [])

    def test_failed_ship_keeps_rows_and_bumps_attempts(self):
        self._enqueue("event", 2)
        worker = ShipperWorker(self.cfg, RecordingShipper(ok=False))
        healthy, n = worker.drain_once()
        self.assertFalse(healthy)
        self.assertEqual(n, 0)
        with connect(self.cfg) as c:
            rows = c.execute("SELECT attempts FROM outbox").fetchall()
        self.assertEqual([r["attempts"] for r in rows], [1, 1])  # all bumped, none deleted

    def test_partial_accept_deletes_only_acked_and_is_unhealthy(self):
        self._enqueue("event", 3)
        worker = ShipperWorker(self.cfg, RecordingShipper(ok=True, accept="first"))
        healthy, n = worker.drain_once()
        self.assertFalse(healthy)          # central took < we sent -> retry the rest
        self.assertEqual(n, 1)
        self.assertEqual(self._outbox_ids(), [2, 3])

    # --- heartbeat ---
    def test_heartbeat_body_reflects_db(self):
        with connect(self.cfg) as c:
            c.execute("INSERT INTO devices (id,name,ip_address,region,is_active)"
                      " VALUES (1,'T','10.0.0.1','R',1),(2,'U','10.0.0.2','R',1)")
            c.execute("INSERT INTO poll_results (device_id,timestamp,packet_loss,state)"
                      " VALUES (1,?,0,'UP')", (TS,))
            c.execute("INSERT INTO outages (device_id,started_at,final_state)"
                      " VALUES (2,?,'DOWN')", (TS,))
            c.commit()
        self._enqueue("rollup", 2)
        ship = RecordingShipper()
        worker = ShipperWorker(self.cfg, ship)
        self.assertTrue(worker.heartbeat_once())
        body = ship.heartbeats[0]["body"]
        self.assertEqual(body["fleet_size"], 2)
        self.assertEqual(body["open_outages"], 1)
        self.assertEqual(body["last_poll_ts"], TS)
        self.assertEqual(body["outbox_backlog"], 2)
        self.assertIn("version", body)

    # --- eviction ---
    def test_evict_enforces_cap_dropping_rollups(self):
        # cap = 4. queue: 2 events + 4 rollups = 6 -> evict 2 oldest rollups.
        self._enqueue("event", 2)
        self._enqueue("rollup", 4)
        worker = ShipperWorker(self.cfg, RecordingShipper())
        evicted = worker.evict_once()
        self.assertEqual(evicted, 2)
        with connect(self.cfg) as c:
            kinds = [r["kind"] for r in c.execute("SELECT kind FROM outbox ORDER BY id")]
        self.assertEqual(kinds.count("event"), 2)         # both events survive
        self.assertEqual(len(kinds), 4)                   # back at the cap

    def test_evict_zero_cap_is_unlimited(self):
        cfg = Config(db_path=self.cfg.db_path, outbox_max_rows=0)
        self._enqueue("rollup", 5)
        self.assertEqual(ShipperWorker(cfg, RecordingShipper()).evict_once(), 0)

    # --- thread gating (the back-compat anchor) ---
    def test_thread_noop_when_central_disabled(self):
        cfg = Config(db_path=self.cfg.db_path)   # central_url empty -> dormant
        self.assertIsNone(start_shipper_thread(cfg, RecordingShipper()))

    def test_thread_starts_when_central_enabled(self):
        # Long intervals so the daemon thread does its first (no-network) pass then sleeps
        # well past the suite's lifetime — it never wakes to touch the torn-down temp DB.
        cfg = Config(db_path=self.cfg.db_path, central_url="https://central.test",
                     ship_interval_s=3600, heartbeat_interval_s=3600)
        t = start_shipper_thread(cfg, RecordingShipper())
        self.assertIsNotNone(t)
        self.assertTrue(t.is_alive())
        # Stop + join BEFORE teardown removes the temp DB, so the worker never wakes onto a
        # deleted file (it would only log, but we keep the suite output clean).
        t.worker.stop()
        t.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
