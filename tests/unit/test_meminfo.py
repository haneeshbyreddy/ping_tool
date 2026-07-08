import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.runtime.meminfo import KEYS, memory_snapshot

class MemorySnapshotTest(unittest.TestCase):
    def test_shape_and_types(self):
        snap = memory_snapshot()
        self.assertEqual(set(snap), set(KEYS))
        for v in snap.values():
            self.assertTrue(v is None or (isinstance(v, int) and v >= 0), v)

    def test_never_raises(self):
        for _ in range(3):
            memory_snapshot()

    def test_linux_values_are_sane(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("linux-only /proc assertions")
        snap = memory_snapshot()
        self.assertGreater(snap["rss_bytes"], 0)
        self.assertGreater(snap["mem_total_bytes"], 0)
        self.assertIsNotNone(snap["mem_available_bytes"])
        self.assertLessEqual(snap["mem_available_bytes"], snap["mem_total_bytes"])

if __name__ == "__main__":
    unittest.main()
