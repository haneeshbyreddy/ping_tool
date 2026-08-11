import asyncio
import os
import sys
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.ingress.snmp import SnmpTarget
from wisp.ingress.walker import PysnmpDiagWalker, _MAX_REPETITIONS

try:
    import pysnmp.hlapi.asyncio  # noqa: F401
    _HAS_PYSNMP = True
except ImportError:
    _HAS_PYSNMP = False

ROOT = "1.3.6.1.4.1.9999.1.1"
OUTSIDE = "1.3.6.1.4.1.9999.2.1.0"


class _Val:
    def __init__(self, s):
        self._s = s

    def prettyPrint(self):
        return str(self._s)


@unittest.skipUnless(_HAS_PYSNMP, "pysnmp not installed")
class DiagWalkResumeTest(unittest.TestCase):

    def _agent(self, universe, batches_per_call, calls):

        async def bulk_walk_cmd(engine, authData, transport, ctx,
                                nonRepeaters, maxRepetitions, varBind, **options):
            start = str(varBind)
            calls.append((start, options.get("lexicographicMode")))
            after = [o for o in universe if o > start]
            for b in range(batches_per_call):
                batch = after[b * maxRepetitions:(b + 1) * maxRepetitions]
                if not batch:
                    return
                yield (None, 0, 0, [(o, _Val("v")) for o in batch])
        return bulk_walk_cmd

    def _walk(self, universe, batches_per_call, max_varbinds=20000):
        import pysnmp.hlapi.asyncio as hlapi
        calls: list[tuple] = []
        walker = PysnmpDiagWalker(Config(snmp_request_timeout_s=0.05,
                                         snmp_request_retries=1))
        with mock.patch.multiple(
            hlapi,
            ObjectIdentity=lambda oid: oid,
            ObjectType=lambda ident: ident,
            bulk_walk_cmd=self._agent(universe, batches_per_call, calls),
        ):
            try:
                res = asyncio.run(walker.walk(
                    SnmpTarget(ip="127.0.0.1", community="public", port=1),
                    ROOT, max_varbinds))
            finally:
                if walker._engine is not None:
                    walker._engine.close_dispatcher()
        return res, calls

    @staticmethod
    def _universe(n, root=ROOT):
        return [f"{root}.{i:04d}" for i in range(1, n + 1)]

    def test_a_batch_boundary_stop_is_resumed_not_reported_complete(self):
        universe = self._universe(60)
        res, calls = self._walk(universe, batches_per_call=2)
        self.assertEqual(len(res.varbinds), 60)
        self.assertFalse(res.truncated)
        self.assertEqual([o for o, _ in res.varbinds], universe)
        self.assertGreater(len(calls), 1, "should have resumed at least once")

    def test_the_resume_starts_after_the_last_oid_and_never_duplicates(self):
        universe = self._universe(60)
        res, calls = self._walk(universe, batches_per_call=2)
        oids = [o for o, _ in res.varbinds]
        self.assertEqual(len(oids), len(set(oids)))
        self.assertEqual(calls[1][0], universe[49])

    def test_the_first_sweep_keeps_pysnmps_own_subtree_bound(self):
        _, calls = self._walk(self._universe(60), batches_per_call=2)
        self.assertIs(calls[0][1], False)
        self.assertTrue(all(c[1] is True for c in calls[1:]))

    def test_a_resume_stops_at_the_first_oid_outside_the_subtree(self):
        universe = self._universe(50) + [OUTSIDE]
        res, _ = self._walk(universe, batches_per_call=2)
        self.assertEqual(len(res.varbinds), 50)
        self.assertNotIn(OUTSIDE, [o for o, _ in res.varbinds])

    def test_a_natural_end_is_not_resumed(self):
        universe = self._universe(194)
        res, calls = self._walk(universe, batches_per_call=100)
        self.assertEqual(len(res.varbinds), 194)
        self.assertEqual(len(calls), 1)
        self.assertFalse(res.truncated)

    def test_an_exact_multiple_that_really_is_the_end_costs_one_probe_and_stops(self):
        universe = self._universe(_MAX_REPETITIONS * 2)
        res, calls = self._walk(universe, batches_per_call=100)
        self.assertEqual(len(res.varbinds), _MAX_REPETITIONS * 2)
        self.assertEqual(len(calls), 2)
        self.assertFalse(res.truncated)

    def test_the_varbind_cap_still_bounds_a_resumed_walk(self):
        res, _ = self._walk(self._universe(300), batches_per_call=2,
                            max_varbinds=60)
        self.assertEqual(len(res.varbinds), 60)
        self.assertTrue(res.truncated)


if __name__ == "__main__":
    unittest.main()
