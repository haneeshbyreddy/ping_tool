import ast
import os
import sys
import threading
import time
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.egress.notifiers import NotifyResult, SendPool, queue_send

_STORE_DIR = Path(__file__).resolve().parents[2] / "src" / "wisp" / "central"


class SendsStayOutsideTheStoreTest(unittest.TestCase):
    def _store_files(self):
        return sorted(_STORE_DIR.glob("store*.py"))

    def test_no_store_method_takes_a_notifier(self):
        offenders = []
        for f in self._store_files():
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = {a.arg for a in
                             node.args.args + node.args.kwonlyargs}
                    if names & {"notifier", "notify", "whatsapp_notifier"}:
                        offenders.append(f"{f.name}:{node.name}")
        self.assertEqual(offenders, [])

    def test_the_store_never_imports_the_egress_layer(self):
        offenders = []
        for f in self._store_files():
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                if any(m.startswith("wisp.egress") for m in mods):
                    offenders.append(f.name)
        self.assertEqual(offenders, [])


class _BareNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send(self, title, body, priority=3, *, whatsapp=(), facts=None):
        self.sent.append(title)
        return NotifyResult(self.ok)


class _QueuedNotifier(_BareNotifier):
    def send_queued(self, title, body, priority=3, *, whatsapp=(), facts=None,
                    on_result=None):
        self.sent.append(f"queued:{title}")
        if on_result is not None:
            on_result(NotifyResult(self.ok))
        return NotifyResult(True, "queued")


class QueueSendTest(unittest.TestCase):
    def test_a_double_without_send_queued_runs_inline(self):
        n = _BareNotifier(ok=False)
        results = []
        res = queue_send(n, "T", "B", whatsapp=["911"],
                         on_result=results.append)
        self.assertEqual(n.sent, ["T"])
        self.assertFalse(res.ok)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)

    def test_a_notifier_with_send_queued_is_delegated_to(self):
        n = _QueuedNotifier()
        results = []
        res = queue_send(n, "T", "B", whatsapp=["911"],
                         on_result=results.append)
        self.assertEqual(n.sent, ["queued:T"])
        self.assertTrue(res.ok)
        self.assertEqual(len(results), 1)

    def test_a_crashing_callback_never_raises_out(self):
        n = _BareNotifier()

        def boom(res):
            raise RuntimeError("callback bug")

        res = queue_send(n, "T", "B", whatsapp=["911"], on_result=boom)
        self.assertTrue(res.ok)


class SendPoolTest(unittest.TestCase):
    def test_jobs_run_on_a_worker_thread(self):
        pool = SendPool(workers=1)
        seen = []
        done = threading.Event()

        def job():
            seen.append(threading.current_thread().name)
            done.set()

        self.assertTrue(pool.submit(job))
        self.assertTrue(done.wait(timeout=5))
        self.assertNotEqual(seen[0], threading.current_thread().name)

    def test_a_full_queue_refuses_instead_of_blocking(self):
        pool = SendPool(workers=1, capacity=1)
        release = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=5)

        self.assertTrue(pool.submit(slow))
        self.assertTrue(started.wait(timeout=5))
        self.assertTrue(pool.submit(lambda: None))
        t0 = time.perf_counter()
        accepted = pool.submit(lambda: None)
        self.assertFalse(accepted)
        self.assertLess(time.perf_counter() - t0, 0.5)
        release.set()

    def test_a_crashing_job_never_kills_the_worker(self):
        pool = SendPool(workers=1)
        done = threading.Event()

        def bad():
            raise RuntimeError("job bug")

        self.assertTrue(pool.submit(bad))
        self.assertTrue(pool.submit(done.set))
        self.assertTrue(done.wait(timeout=5))


if __name__ == "__main__":
    unittest.main()
