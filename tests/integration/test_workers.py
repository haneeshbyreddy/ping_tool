"""Phase 8.4 — team directory (workers): CRUD, backfill, last-owner guard, routing.

Temp DB, direct service calls. The backfill normally runs inside migrate (when an
existing deployment upgrades with devices already present); here we exercise the
idempotent 0004 SQL directly after inserting devices.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.database.client import connect, migrate
from wisp.server import services
from wisp.egress.notifiers import role_topic


class WorkersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=Path(self.tmp.name) / "t.db")
        migrate(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _add_owner(self, name="Owner", active=1):
        return services.create_worker(
            {"name": name, "role": "owner", "is_active": active}, cfg=self.cfg)

    # -- CRUD ----------------------------------------------------------------
    def test_create_list_update_delete(self):
        wid = services.create_worker(
            {"name": "Suresh", "role": "tech", "region": "Rampur"},
            cfg=self.cfg)
        self.assertTrue(wid > 0)
        workers = services.list_workers(self.cfg)
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["name"], "Suresh")

        self.assertTrue(services.update_worker(
            wid, {"name": "Suresh K", "role": "tech",
                   "region": "Rampur", "is_active": 1}, cfg=self.cfg))
        self.assertEqual(services.list_workers(self.cfg)[0]["name"], "Suresh K")

        self.assertEqual(services.delete_worker(wid, self.cfg), {"ok": True})
        self.assertEqual(services.list_workers(self.cfg), [])

    def test_validation(self):
        with self.assertRaises(services.WorkerError):
            services.create_worker({"name": "", "role": "tech"}, cfg=self.cfg)
        with self.assertRaises(services.WorkerError):
            services.create_worker({"name": "X", "role": "wizard"}, cfg=self.cfg)

    # -- last-owner guard ----------------------------------------------------
    def test_cannot_remove_last_owner(self):
        oid = self._add_owner()
        with self.assertRaises(services.LastOwnerError):
            services.delete_worker(oid, self.cfg)
        # demoting the sole owner is also blocked
        with self.assertRaises(services.LastOwnerError):
            services.update_worker(oid, {"name": "Owner", "role": "tech",
                                         "is_active": 1}, cfg=self.cfg)
        # deactivating the sole owner is blocked
        with self.assertRaises(services.LastOwnerError):
            services.update_worker(oid, {"name": "Owner", "role": "owner",
                                         "is_active": 0}, cfg=self.cfg)

    def test_second_owner_unblocks_removal(self):
        first = self._add_owner("Owner A")
        self._add_owner("Owner B")
        self.assertEqual(services.delete_worker(first, self.cfg), {"ok": True})

    # -- role → channel routing ----------------------------------------------
    def test_role_topic_maps_to_config_channels(self):
        self.assertEqual(role_topic("owner", self.cfg), self.cfg.ntfy_topic_owner)
        self.assertEqual(role_topic("operator", self.cfg), self.cfg.ntfy_topic_operator)
        self.assertEqual(role_topic("tech", self.cfg), self.cfg.ntfy_topic_tech)
        # unknown role falls back to the tech channel
        self.assertEqual(role_topic("nobody", self.cfg), self.cfg.ntfy_topic_tech)

    # -- backfill from existing devices --------------------------------------
    def test_backfill_idempotent(self):
        with connect(self.cfg) as conn:
            for did, name, phone in [(1, "T1", "+91A"), (2, "T2", "+91A"),
                                     (3, "T3", "+91B")]:
                conn.execute(
                    "INSERT INTO devices (id, name, ip_address, region,"
                    " technician_phone) VALUES (?,?,?,?,?)",
                    (did, name, f"ip{did}", "Rampur", phone))
            conn.commit()
        sql = (self.cfg.migrations_dir / "0004_workers.sql").read_text()
        with connect(self.cfg) as conn:
            conn.executescript(sql)
            conn.executescript(sql)  # second run must not duplicate
            phones = sorted(r["phone"] for r in
                            conn.execute("SELECT phone FROM workers"))
        # one worker per distinct technician_phone, no dupes on re-run
        self.assertEqual(phones, ["+91A", "+91B"])


if __name__ == "__main__":
    unittest.main()
