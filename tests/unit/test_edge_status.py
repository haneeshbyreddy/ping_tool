import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wisp.runtime import edge_status
from wisp.runtime.edge_status import StatusWriter, read_status, status_path

def _writer(tmp: str, **over) -> StatusWriter:
    kw = dict(org_id="ispA", node_id="edge-1", central_url="https://c.example.net",
              interval_s=60, version="1.2.3")
    kw.update(over)
    return StatusWriter(Path(tmp) / "status.json", **kw)

class StatusWriterTest(unittest.TestCase):
    def test_write_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = _writer(tmp)
            w.write(edge_status.PHASE_RUNNING, ok=True, devices=12)
            raw = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(raw["phase"], "running")
            self.assertTrue(raw["ok"])
            self.assertEqual(raw["devices"], 12)
            self.assertEqual(raw["node_id"], "edge-1")
            self.assertEqual(raw["version"], "1.2.3")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             ["status.json"])

    def test_write_never_raises_on_bad_path(self):
        w = StatusWriter(Path("/proc/nonexistent/status.json"), org_id="o", node_id="n",
                         central_url="", interval_s=60, version="v")
        w.write(edge_status.PHASE_RUNNING, ok=True)

    def test_set_interval_reflected(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = _writer(tmp, interval_s=60)
            w.set_interval(30)
            w.write(edge_status.PHASE_RUNNING, ok=True)
            raw = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(raw["interval_s"], 30)

    def test_status_path_next_to_lock(self):
        self.assertEqual(status_path("/var/lib/wisp/wisp.db"),
                         Path("/var/lib/wisp/status.json"))

class ReadStatusTest(unittest.TestCase):
    def _read(self, tmp, *, age_s=0.0):
        now = datetime.now(timezone.utc) + timedelta(seconds=age_s)
        return read_status(Path(tmp) / "status.json", now=now)

    def test_missing_file_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._read(tmp).state, edge_status.STATE_UNKNOWN)

    def test_garbage_file_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "status.json").write_text("not json{{")
            self.assertEqual(self._read(tmp).state, edge_status.STATE_UNKNOWN)

    def test_fresh_ok_reports_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            _writer(tmp).write(edge_status.PHASE_RUNNING, ok=True, devices=7)
            view = self._read(tmp)
            self.assertEqual(view.state, edge_status.STATE_OK)
            self.assertIn("7 device(s)", view.detail)

    def test_fresh_failed_report_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _writer(tmp).write(edge_status.PHASE_RUNNING, ok=False,
                               error="last report to central failed")
            view = self._read(tmp)
            self.assertEqual(view.state, edge_status.STATE_DEGRADED)
            self.assertIn("report to central failed", view.detail)

    def test_starting_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            _writer(tmp).write(edge_status.PHASE_STARTING)
            self.assertEqual(self._read(tmp).state, edge_status.STATE_STARTING)

    def test_error_phase_carries_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            _writer(tmp).write(edge_status.PHASE_ERROR, ok=False,
                               error="cannot fetch devices from https://x: boom")
            view = self._read(tmp)
            self.assertEqual(view.state, edge_status.STATE_ERROR)
            self.assertIn("cannot fetch devices", view.detail)

    def test_error_phase_beats_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            _writer(tmp).write(edge_status.PHASE_ERROR, ok=False, error="boom")
            self.assertEqual(self._read(tmp, age_s=9999).state, edge_status.STATE_ERROR)

    def test_old_status_goes_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _writer(tmp).write(edge_status.PHASE_RUNNING, ok=True, devices=7)
            self.assertEqual(self._read(tmp, age_s=240).state, edge_status.STATE_STALE)
            self.assertEqual(self._read(tmp, age_s=120).state, edge_status.STATE_OK)

    def test_stale_floor_for_tiny_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = _writer(tmp, interval_s=5)
            w.write(edge_status.PHASE_RUNNING, ok=True)
            self.assertEqual(self._read(tmp, age_s=60).state, edge_status.STATE_OK)
            self.assertEqual(self._read(tmp, age_s=200).state, edge_status.STATE_STALE)

class ParseEnvPs1Test(unittest.TestCase):
    def test_parses_installer_shape(self):
        text = (
            "# GENERATED by the WISP Edge installer — edit values, not structure.\n"
            "$env:WISP_CENTRAL_URL = 'https://central.example.net'\n"
            "$env:WISP_CENTRAL_TOKEN = 's3cret'\n"
            "$env:WISP_ORG_ID = 'ispA'\n"
            "$env:WISP_NODE_ID = 'edge-w1'\n"
            "$env:WISP_DB = 'C:\\ProgramData\\WISP\\wisp.db'\n"
        )
        cfg = edge_status.parse_env_ps1(text)
        self.assertEqual(cfg["WISP_CENTRAL_URL"], "https://central.example.net")
        self.assertEqual(cfg["WISP_NODE_ID"], "edge-w1")
        self.assertEqual(cfg["WISP_DB"], "C:\\ProgramData\\WISP\\wisp.db")

    def test_ignores_comments_and_junk(self):
        cfg = edge_status.parse_env_ps1("# $env:WISP_X = 'no'\nrandom line\n")
        self.assertEqual(cfg, {})

if __name__ == "__main__":
    unittest.main()
