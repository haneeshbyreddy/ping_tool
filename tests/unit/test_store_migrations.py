import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central.store import CentralStore


_OLD_TABLE = """
CREATE TABLE onu_places (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       TEXT NOT NULL,
    mac          TEXT NOT NULL,
    lat          REAL NOT NULL,
    lng          REAL NOT NULL,
    label        TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    witness      INTEGER NOT NULL DEFAULT 1,
    accuracy_m   REAL,
    place_source TEXT,
    placed_by    TEXT,
    placed_at    TEXT,
    phone        TEXT,
    UNIQUE(org_id, mac)
)"""


class OnuPlaceCoordRelaxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "central.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_old_db(self, rows=()):
        conn = sqlite3.connect(self.db)
        conn.execute(_OLD_TABLE)
        for r in rows:
            cols = ", ".join(r)
            marks = ", ".join("?" * len(r))
            conn.execute(f"INSERT INTO onu_places ({cols}) VALUES ({marks})",
                         tuple(r.values()))
        conn.commit()
        conn.close()

    @staticmethod
    def _row(mac, **kw):
        base = {"org_id": "ispA", "mac": mac, "lat": 15.85, "lng": 74.5,
                "created_at": "2026-07-28T00:00:00+00:00",
                "updated_at": "2026-07-28T00:00:00+00:00"}
        base.update(kw)
        return base

    def _notnull(self, col):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        info = list(conn.execute("PRAGMA table_info(onu_places)"))
        conn.close()
        return next(r["notnull"] for r in info if r["name"] == col)

    def test_the_coordinates_stop_being_mandatory(self):
        self._seed_old_db()
        self.assertTrue(self._notnull("lat"))
        CentralStore(self.db)
        self.assertFalse(self._notnull("lat"))
        self.assertFalse(self._notnull("lng"))

    def test_every_placement_survives_the_rebuild_intact(self):
        self._seed_old_db([
            self._row("AA:BB", label="RAMESH", phone="9876543210", witness=0,
                      accuracy_m=6.5, place_source="gps", placed_by="field",
                      placed_at="2026-07-29T05:00:00+00:00"),
            self._row("CC:DD", label="WATER TANK", witness=1, notes="on the UPS"),
            self._row("EE:FF", org_id="ispB", label="OTHER ORG", witness=1),
        ])
        store = CentralStore(self.db)

        a = store.get_onu_place("ispA", "AA:BB")
        self.assertEqual(a["label"], "RAMESH")
        self.assertEqual(a["phone"], "9876543210")
        self.assertAlmostEqual(a["lat"], 15.85)
        self.assertEqual(a["accuracy_m"], 6.5)
        self.assertEqual(a["place_source"], "gps")
        self.assertEqual(a["placed_by"], "field")
        self.assertEqual(a["witness"], 0)

        b = store.get_onu_place("ispA", "CC:DD")
        self.assertEqual(b["notes"], "on the UPS")
        self.assertEqual(b["witness"], 1)

        self.assertIsNone(store.get_onu_place("ispA", "EE:FF"))
        self.assertEqual(store.get_onu_place("ispB", "EE:FF")["label"],
                         "OTHER ORG")

    def test_the_witness_set_is_unchanged_by_the_migration(self):
        self._seed_old_db([
            self._row("AA:BB", witness=1),
            self._row("CC:DD", witness=0),
        ])
        store = CentralStore(self.db)
        self.assertEqual(store.onu_place_macs("ispA"), {"AA:BB"})

    def test_the_unique_key_survives_so_one_sticker_stays_one_row(self):
        self._seed_old_db([self._row("AA:BB")])
        store = CentralStore(self.db)
        store.set_onu_place("ispA", "AA:BB", 16.0, 75.0, "MOVED", None, witness=True)
        self.assertEqual(len(store.list_onu_places("ispA")), 1)
        self.assertAlmostEqual(store.list_onu_places("ispA")[0]["lat"], 16.0)

    def test_it_is_a_no_op_on_a_database_that_has_already_run_it(self):
        self._seed_old_db([self._row("AA:BB", label="RAMESH")])
        CentralStore(self.db)
        store = CentralStore(self.db)
        self.assertFalse(self._notnull("lat"))
        self.assertEqual(store.get_onu_place("ispA", "AA:BB")["label"], "RAMESH")

    def test_a_fresh_database_needs_no_migration_at_all(self):
        store = CentralStore(self.db)
        self.assertFalse(self._notnull("lat"))
        store.set_onu_contact("ispA", "AA:BB", "RAMESH", "9876543210", None)
        self.assertIsNone(store.get_onu_place("ispA", "AA:BB")["lat"])

    def test_an_older_db_missing_a_bolted_on_column_still_migrates(self):
        conn = sqlite3.connect(self.db)
        conn.execute("""
            CREATE TABLE onu_places (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id     TEXT NOT NULL,
                mac        TEXT NOT NULL,
                lat        REAL NOT NULL,
                lng        REAL NOT NULL,
                label      TEXT,
                notes      TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(org_id, mac)
            )""")
        conn.execute("INSERT INTO onu_places (org_id, mac, lat, lng, label,"
                     " created_at, updated_at) VALUES"
                     " ('ispA','AA:BB',15.85,74.5,'RAMESH','t','t')")
        conn.commit()
        conn.close()
        store = CentralStore(self.db)
        rec = store.get_onu_place("ispA", "AA:BB")
        self.assertEqual(rec["label"], "RAMESH")
        self.assertEqual(rec["witness"], 1)
        self.assertIsNone(rec["phone"])


if __name__ == "__main__":
    unittest.main()
