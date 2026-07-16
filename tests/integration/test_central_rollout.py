import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central import rollout

NOW = datetime(2026, 1, 1, 12, 0, 0)
ART = {"linux-amd64": {"url": "https://c/v2/wisp-edge", "sha256": "abc"}}

def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")

class RolloutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "c.db")
        self.cfg = Config(central_node_stale_s=180, rollout_health_window_s=600)
        self.store.set_release("v2", ART)

    def tearDown(self):
        self.tmp.cleanup()

    def _node(self, node_id, version, age_s=0):
        self.store.record_heartbeat("ispA", node_id,
            {"version": version, "platform": "linux-amd64"}, now=_iso(NOW - timedelta(seconds=age_s)))

    def test_canary_node_gets_directive_others_dont(self):
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        d = rollout.directive_for(self.store, "ispA", "edge-1", "v1", "linux-amd64", now=NOW)
        self.assertEqual(d["target_version"], "v2")
        self.assertEqual(d["url"], ART["linux-amd64"]["url"])
        self.assertIsNone(rollout.directive_for(self.store, "ispA", "edge-2", "v1",
                                                "linux-amd64", now=NOW))

    def test_up_to_date_node_gets_nothing(self):
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        self.assertIsNone(rollout.directive_for(self.store, "ispA", "edge-1", "v2",
                                                "linux-amd64", now=NOW))

    def test_downgrade_directive_refused(self):
        self.store.set_release("0.11.2", {"linux-amd64": {"url": "u", "sha256": "h"}})
        self.store.set_rollout("ispA", "0.11.2", ["edge-1"], now=_iso(NOW))
        self.assertIsNone(rollout.directive_for(self.store, "ispA", "edge-1", "0.12.0",
                                                "linux-amd64", now=NOW))

    def test_no_artifact_for_platform_no_directive(self):
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        self.assertIsNone(rollout.directive_for(self.store, "ispA", "edge-1", "v1",
                                                "win-amd64", now=NOW))

    def test_promoted_state_directs_every_node(self):
        self.store.set_rollout("ispA", "v2", ["edge-1"], state="promoted", now=_iso(NOW))
        self.assertIsNotNone(rollout.directive_for(self.store, "ispA", "edge-2", "v1",
                                                   "linux-amd64", now=NOW))

    def test_halted_and_done_direct_nothing(self):
        for st in ("halted", "done"):
            self.store.set_rollout("ispA", "v2", ["edge-1"], state=st, now=_iso(NOW))
            self.assertIsNone(rollout.directive_for(self.store, "ispA", "edge-1", "v1",
                                                    "linux-amd64", now=NOW))

    def test_canary_promotes_when_canary_healthy_on_target(self):
        self._node("edge-1", "v2")
        self._node("edge-2", "v1")
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        self.assertEqual(rollout.evaluate(self.store, "ispA", cfg=self.cfg, now=NOW), "promoted")

    def test_canary_stays_until_healthy(self):
        self._node("edge-1", "v1")
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        self.assertEqual(rollout.evaluate(self.store, "ispA", cfg=self.cfg, now=NOW), "canary")

    def test_canary_halts_past_window_when_unhealthy(self):
        self._node("edge-1", "v1")
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        later = NOW + timedelta(seconds=700)
        self.assertEqual(rollout.evaluate(self.store, "ispA", cfg=self.cfg, now=later), "halted")

    def test_canary_halts_when_canary_goes_silent(self):
        self._node("edge-1", "v2", age_s=900)
        self.store.set_rollout("ispA", "v2", ["edge-1"], now=_iso(NOW))
        later = NOW + timedelta(seconds=700)
        self.assertEqual(rollout.evaluate(self.store, "ispA", cfg=self.cfg, now=later), "halted")

    def test_promoted_finishes_when_all_on_target(self):
        self._node("edge-1", "v2")
        self._node("edge-2", "v2")
        self.store.set_rollout("ispA", "v2", ["edge-1"], state="promoted", now=_iso(NOW))
        self.assertEqual(rollout.evaluate(self.store, "ispA", cfg=self.cfg, now=NOW), "done")

    def test_empty_canary_promotes_immediately(self):
        self.store.set_rollout("ispA", "v2", [], now=_iso(NOW))
        self.assertEqual(rollout.evaluate(self.store, "ispA", cfg=self.cfg, now=NOW), "promoted")


class AutoRolloutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "c.db")
        self.store.set_release("v2", ART)
        self.store.set_org_auto_update("ispA", True)

    def tearDown(self):
        self.tmp.cleanup()

    def _arm(self, node="edge-1", version="v1"):
        return rollout.maybe_auto_rollout(self.store, "ispA", node, version)

    def test_arms_canary_rollout_for_stale_node(self):
        self.assertTrue(self._arm())
        ro = self.store.get_rollout("ispA")
        self.assertEqual((ro["state"], ro["target_version"], ro["canary"]),
                         ("canary", "v2", ["edge-1"]))

    def test_disabled_org_never_arms(self):
        self.store.set_org_auto_update("ispA", False)
        self.assertFalse(self._arm())
        self.assertIsNone(self.store.get_rollout("ispA"))

    def test_current_or_ahead_node_never_arms(self):
        self.assertFalse(self._arm(version="v2"))
        self.assertFalse(self._arm(version="v3"))
        self.assertIsNone(self.store.get_rollout("ispA"))

    def test_live_rollout_not_replaced(self):
        self.store.set_rollout("ispA", "v2", ["edge-9"])
        self.assertFalse(self._arm())
        self.assertEqual(self.store.get_rollout("ispA")["canary"], ["edge-9"])

    def test_halted_same_target_needs_a_human(self):
        # halting means the build failed its health gate — auto-retrying it
        # would loop a broken build through the fleet forever
        self.store.set_rollout("ispA", "v2", ["edge-1"], state="halted")
        self.assertFalse(self._arm())
        self.assertEqual(self.store.get_rollout("ispA")["state"], "halted")

    def test_halted_older_target_rearms_for_newer_release(self):
        self.store.set_rollout("ispA", "v1.5", ["edge-1"], state="halted")
        self.assertTrue(self._arm())
        self.assertEqual(self.store.get_rollout("ispA")["target_version"], "v2")

    def test_done_rollout_rearms_for_fresh_install_of_old_build(self):
        self.store.set_rollout("ispA", "v2", ["edge-1"], state="done")
        self.assertTrue(self._arm(node="edge-new"))
        ro = self.store.get_rollout("ispA")
        self.assertEqual((ro["state"], ro["canary"]), ("canary", ["edge-new"]))

    def test_no_release_no_arm(self):
        with tempfile.TemporaryDirectory() as tmp2:
            store = CentralStore(Path(tmp2) / "c2.db")
            store.set_org_auto_update("ispA", True)
            self.assertFalse(rollout.maybe_auto_rollout(store, "ispA", "edge-1", "v1"))


if __name__ == "__main__":
    unittest.main()
