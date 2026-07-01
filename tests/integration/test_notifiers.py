"""`send_with_retry` policy tests — the retry/backoff contract that keeps a
transient ntfy blip from silently eating a page. Pure (clock injected), no
network, no DB. The DB-coupled alert POLICY this file used to also cover
(`AlertDispatcher`: routing, anti-spam, the escalation ladder) moved to
central (`central/dispatch.py`'s `CentralAlertDispatcher`) along with the FSM
— see `tests/integration/test_central_brain.py`'s `CentralAlertDispatcherTest`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.egress.notifiers import NotifyResult, _Attempt, send_with_retry


class SendRetryTest(unittest.TestCase):
    """The retry policy that keeps a transient blip from silently eating a page."""

    def _runner(self, outcomes):
        """outcomes: list of _Attempt to return in order. Captures backoff sleeps."""
        slept: list[float] = []
        seq = iter(outcomes)
        res = send_with_retry(lambda: next(seq), attempts=len(outcomes),
                              backoff=0.5, sleep=slept.append)
        return res, slept

    def test_succeeds_first_try_no_sleep(self):
        res, slept = self._runner([_Attempt(NotifyResult(True), False)])
        self.assertTrue(res.ok)
        self.assertEqual(slept, [])

    def test_retries_transient_then_succeeds_with_backoff(self):
        res, slept = self._runner([
            _Attempt(NotifyResult(False, "timeout"), True),
            _Attempt(NotifyResult(False, "timeout"), True),
            _Attempt(NotifyResult(True), False),
        ])
        self.assertTrue(res.ok)
        self.assertEqual(slept, [0.5, 1.0])  # exponential backoff between attempts

    def test_all_transient_returns_last_failure(self):
        res, slept = self._runner([
            _Attempt(NotifyResult(False, "boom"), True),
            _Attempt(NotifyResult(False, "boom"), True),
        ])
        self.assertFalse(res.ok)
        self.assertEqual(len(slept), 1)  # one backoff between the two attempts

    def test_non_retryable_stops_immediately(self):
        # a 4xx (bad topic/config) won't self-heal — fail fast, don't burn retries
        slept: list[float] = []
        calls = {"n": 0}
        def _attempt():
            calls["n"] += 1
            return _Attempt(NotifyResult(False, "HTTP 403"), False)
        res = send_with_retry(_attempt, attempts=5, backoff=0.5, sleep=slept.append)
        self.assertFalse(res.ok)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
