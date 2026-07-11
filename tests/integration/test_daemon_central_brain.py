import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

_spec = importlib.util.spec_from_file_location(
    "wisp_daemon_main_cb", _REPO / "apps" / "daemon" / "main.py")
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

from wisp.config import Config
from wisp.ingress.probers import PingResult
from wisp.ingress.snmp import PortStatus
from wisp.runtime.central_client import CentralClientError

def _dev(id, ip, parent=None, snmp_enabled=False, snmp_community=None):
    return {"id": id, "name": f"d{id}", "ip_address": ip, "region": "R",
            "parent_device_id": parent, "snmp_enabled": snmp_enabled,
            "snmp_community": snmp_community, "snmp_port": 161, "snmp_version": "2c"}

class _FakeProber:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    async def ping(self, ip, count):
        return self._behaviour[ip]()

    def on_cycle_start(self):
        pass

class RecordingCentralClient:
    def __init__(self, devices, canary_ip="1.1.1.1", fail_report=False, fail_fetch=False,
                 replies=None, fail_heartbeat=False, heartbeat_reply=None):
        self.devices = devices
        self.canary_ip = canary_ip
        self.fail_report = fail_report
        self.fail_fetch = fail_fetch
        self.fail_heartbeat = fail_heartbeat
        self.heartbeat_reply = heartbeat_reply if heartbeat_reply is not None else {"ok": True}
        self.reports: list[dict] = []
        self.heartbeats: list[dict] = []
        self.walk_results: list[dict] = []
        self.fail_walk_result = False
        self.fetch_calls = 0
        self._replies = list(replies) if replies is not None else None

    def fetch_devices(self) -> dict:
        self.fetch_calls += 1
        if self.fail_fetch:
            raise CentralClientError("fetch boom")
        return {"devices": self.devices, "canary_ip": self.canary_ip}

    def report(self, pings: dict, ts: str, *, mode: str = "full", ports=None,
               optics=None, health=None, snmp_status=None) -> dict:
        if self.fail_report:
            raise CentralClientError("report boom")
        self.reports.append({"pings": pings, "ts": ts, "mode": mode, "ports": ports,
                             "optics": optics, "health": health,
                             "snmp_status": snmp_status})
        if self._replies:
            return self._replies.pop(0)
        return {"ok": True}

    def walk_result(self, walk_id: int, *, varbinds=None, error=None) -> dict:
        if self.fail_walk_result:
            raise CentralClientError("walk upload boom")
        self.walk_results.append({"walk_id": walk_id, "varbinds": varbinds,
                                  "error": error})
        return {"ok": True}

    def heartbeat(self, body: dict) -> dict:
        self.heartbeats.append(body)
        if self.fail_heartbeat:
            raise CentralClientError("heartbeat boom")
        return self.heartbeat_reply

class _FakeSnmpPoller:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    async def walk(self, target):
        result = self._behaviour[target.ip]
        if isinstance(result, Exception):
            raise result
        return result

class GentleProbePlanTest(unittest.TestCase):
    def test_parent_gets_infra_cadence_leaf_gets_full(self):
        cfg = Config(pings_per_poll=5, pings_per_poll_infra=2)
        devices = [_dev(1, "10.0.0.1"), _dev(2, "10.0.0.2", parent=1)]
        plan = daemon._gentle_probe_plan(devices, "1.1.1.1", cfg)
        self.assertEqual(plan["10.0.0.1"], 2)
        self.assertEqual(plan["10.0.0.2"], 5)
        self.assertEqual(plan["1.1.1.1"], 5)

class RunCycleCentralBrainTest(unittest.TestCase):
    def test_reports_raw_pings_for_every_probed_ip(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 12.0, 0.0, 1.5),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 8.0, 0.0, 0.5),
        })
        client = RecordingCentralClient(devices)
        cfg = Config()
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(len(client.reports), 1)
        pings = client.reports[0]["pings"]
        self.assertEqual(pings["10.0.0.1"], {"loss_pct": 0.0, "latency_ms": 12.0,
                                              "jitter_ms": 1.5})
        self.assertIn("1.1.1.1", pings)

    def test_report_failure_does_not_raise(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, fail_report=True)
        cfg = Config()
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(client.reports, [])

    def test_follows_recheck_hint_returned_by_the_full_report(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, replies=[
            {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}},
            {"ok": True},
        ])
        cfg = Config(retry_interval_s=0.001)
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(len(client.reports), 2)
        self.assertEqual(client.reports[0]["mode"], "full")
        self.assertEqual(client.reports[1]["mode"], "recheck")
        self.assertEqual(set(client.reports[1]["pings"]), {"10.0.0.1"})

    def test_recheck_disabled_when_retry_interval_zero(self):
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, replies=[
            {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 2.0}},
        ])
        cfg = Config(retry_interval_s=0)
        asyncio.run(daemon.run_cycle_central_brain(prober, client, devices, "1.1.1.1", cfg))
        self.assertEqual(len(client.reports), 1)

    def test_snmp_ports_attached_to_the_full_report_when_poller_given(self):
        devices = [_dev(1, "10.0.0.1", snmp_enabled=True, snmp_community="public")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices)
        snmp = _FakeSnmpPoller({"10.0.0.1": [
            PortStatus(if_index=2, if_name="Gi0/2", if_alias=None,
                      admin_status="up", oper_status="down")]})
        cfg = Config()
        asyncio.run(daemon.run_cycle_central_brain(
            prober, client, devices, "1.1.1.1", cfg, snmp_poller=snmp))
        self.assertEqual(len(client.reports), 1)
        ports = client.reports[0]["ports"]
        self.assertEqual(ports[1], [{"if_index": 2, "if_name": "Gi0/2", "if_alias": None,
                                     "admin_status": "up", "oper_status": "down",
                                     "last_change": None, "in_octets": None,
                                     "out_octets": None, "speed_bps": None}])

    def test_snmp_skips_devices_without_it_enabled(self):
        devices = [_dev(1, "10.0.0.1", snmp_enabled=False)]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices)
        snmp = _FakeSnmpPoller({})
        cfg = Config()
        asyncio.run(daemon.run_cycle_central_brain(
            prober, client, devices, "1.1.1.1", cfg, snmp_poller=snmp))
        self.assertIsNone(client.reports[0]["ports"])

    def test_a_dead_switch_does_not_sink_the_icmp_cycle(self):
        devices = [_dev(1, "10.0.0.1", snmp_enabled=True, snmp_community="public"),
                  _dev(2, "10.0.0.2", snmp_enabled=True, snmp_community="public")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
            "10.0.0.2": lambda: PingResult("10.0.0.2", 5.0, 0.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices)
        snmp = _FakeSnmpPoller({"10.0.0.1": RuntimeError("SNMP walk boom"),
                               "10.0.0.2": [PortStatus(1, "Gi0/1", None, "up", "up")]})
        cfg = Config()
        asyncio.run(daemon.run_cycle_central_brain(
            prober, client, devices, "1.1.1.1", cfg, snmp_poller=snmp))
        self.assertEqual(len(client.reports), 1)
        self.assertEqual(set(client.reports[0]["ports"]), {2})

class GatherSnmpPortsTest(unittest.TestCase):
    def test_walks_only_snmp_enabled_devices(self):
        devices = [_dev(1, "10.0.0.1", snmp_enabled=True, snmp_community="public"),
                  _dev(2, "10.0.0.2", snmp_enabled=False)]
        snmp = _FakeSnmpPoller({"10.0.0.1": [PortStatus(3, "Gi0/3", "-> X", "up", "down")]})
        cfg = Config()
        ports, status = asyncio.run(daemon._gather_snmp_ports(snmp, devices, cfg))
        self.assertEqual(set(ports), {1})
        self.assertEqual(ports[1], [{"if_index": 3, "if_name": "Gi0/3", "if_alias": "-> X",
                                     "admin_status": "up", "oper_status": "down",
                                     "last_change": None, "in_octets": None,
                                     "out_octets": None, "speed_bps": None}])
        self.assertEqual(status[1]["state"], "ok")
        self.assertNotIn(2, status)  # snmp off: no diagnosis row

    def test_a_hung_walk_is_capped_not_waited_out(self):
        class _HangingPoller:
            async def walk(self, target):
                if target.ip == "10.0.0.1":
                    await asyncio.sleep(3600)
                return [PortStatus(1, "Gi0/1", None, "up", "up")]

        devices = [_dev(1, "10.0.0.1", snmp_enabled=True, snmp_community="public"),
                  _dev(2, "10.0.0.2", snmp_enabled=True, snmp_community="public")]
        # Port walks are capped by their own dedicated timeout, not snmp_walk_timeout_s.
        cfg = Config(port_walk_timeout_s=0.05)
        ports, status = asyncio.run(daemon._gather_snmp_ports(_HangingPoller(), devices, cfg))
        self.assertEqual(set(ports), {2})
        self.assertEqual(status[1]["state"], "timeout")

    def test_slow_port_walk_rides_the_port_cap_not_the_snmp_cap(self):
        # A big OLT (HILL/PYLON class, 200+ interfaces) blows the generic 20s cap on
        # the ifTable walk but must still land under the dedicated port budget — the
        # 2026-07-09 stale-ports regression: switch_ports starved by snmp_walk_timeout_s
        # while health/optics (smaller walks) stayed fresh.
        class _SlowPoller:
            async def walk(self, target):
                await asyncio.sleep(0.1)
                return [PortStatus(1, "Gi0/1", None, "up", "up")]

        devices = [_dev(8, "10.0.0.8", snmp_enabled=True, snmp_community="public")]
        cfg = Config(snmp_walk_timeout_s=0.01, port_walk_timeout_s=5.0)
        ports, _ = asyncio.run(daemon._gather_snmp_ports(_SlowPoller(), devices, cfg))
        self.assertEqual(set(ports), {8})

    def test_walks_run_concurrently_not_serially(self):
        inflight = {"now": 0, "peak": 0}

        class _SlowPoller:
            async def walk(self, target):
                inflight["now"] += 1
                inflight["peak"] = max(inflight["peak"], inflight["now"])
                await asyncio.sleep(0.02)
                inflight["now"] -= 1
                return [PortStatus(1, "Gi0/1", None, "up", "up")]

        devices = [_dev(i, f"10.0.0.{i}", snmp_enabled=True, snmp_community="public")
                  for i in range(1, 5)]
        cfg = Config(snmp_max_inflight=4)
        ports, _ = asyncio.run(daemon._gather_snmp_ports(_SlowPoller(), devices, cfg))
        self.assertEqual(set(ports), {1, 2, 3, 4})
        self.assertGreater(inflight["peak"], 1)

    def test_inflight_bound_is_respected(self):
        inflight = {"now": 0, "peak": 0}

        class _SlowPoller:
            async def walk(self, target):
                inflight["now"] += 1
                inflight["peak"] = max(inflight["peak"], inflight["now"])
                await asyncio.sleep(0.01)
                inflight["now"] -= 1
                return []

        devices = [_dev(i, f"10.0.0.{i}", snmp_enabled=True, snmp_community="public")
                  for i in range(1, 7)]
        cfg = Config(snmp_max_inflight=2)
        asyncio.run(daemon._gather_snmp_ports(_SlowPoller(), devices, cfg))
        self.assertLessEqual(inflight["peak"], 2)

class _FakeDiagWalker:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result
        self._exc = exc

    async def walk(self, target, root_oid, max_varbinds):
        self.calls.append((target.ip, root_oid, max_varbinds))
        if self._exc:
            raise self._exc
        return self._result


def _walk_directive(wid=7, ip="10.0.0.9", root="1.3.6.1", max_varbinds=100):
    return {"id": wid, "ip_address": ip, "snmp_community": "public",
            "snmp_port": 161, "snmp_version": "2c", "root_oid": root,
            "max_varbinds": max_varbinds}


class DiagWalkRunnerTest(unittest.TestCase):
    def setUp(self):
        from wisp.ingress.walker import WalkResult
        self.WalkResult = WalkResult
        self.devices = [_dev(1, "10.0.0.9", snmp_enabled=True,
                             snmp_community="public")]

    def _run(self, runner, directives):
        async def scenario():
            runner.accept(directives, self.devices)
            if runner._task is not None:
                await runner._task
        asyncio.run(scenario())

    def test_runs_the_walk_and_posts_varbinds(self):
        client = RecordingCentralClient(self.devices)
        walker = _FakeDiagWalker(result=self.WalkResult(
            varbinds=[("1.3.6.1.2.1.1.5.0", "sw1")]))
        runner = daemon._DiagWalkRunner(client, Config(), walker=walker)
        self._run(runner, [_walk_directive()])
        self.assertEqual(walker.calls, [("10.0.0.9", "1.3.6.1", 100)])
        self.assertEqual(client.walk_results, [{
            "walk_id": 7, "varbinds": [["1.3.6.1.2.1.1.5.0", "sw1"]],
            "error": None}])

    def test_redelivered_directive_is_deduped(self):
        client = RecordingCentralClient(self.devices)
        walker = _FakeDiagWalker(result=self.WalkResult())
        runner = daemon._DiagWalkRunner(client, Config(), walker=walker)
        self._run(runner, [_walk_directive()])
        self._run(runner, [_walk_directive()])  # central re-delivers until done
        self.assertEqual(len(walker.calls), 1)
        self.assertEqual(len(client.walk_results), 1)

    def test_target_outside_the_device_list_is_refused(self):
        client = RecordingCentralClient(self.devices)
        walker = _FakeDiagWalker(result=self.WalkResult())
        runner = daemon._DiagWalkRunner(client, Config(), walker=walker)
        self._run(runner, [_walk_directive(ip="192.168.99.99")])
        self.assertEqual(walker.calls, [])
        self.assertEqual(len(client.walk_results), 1)
        self.assertIn("not a device", client.walk_results[0]["error"])

    def test_walk_failure_reports_an_error_result(self):
        client = RecordingCentralClient(self.devices)
        walker = _FakeDiagWalker(exc=RuntimeError("No SNMP response"))
        runner = daemon._DiagWalkRunner(client, Config(), walker=walker)
        self._run(runner, [_walk_directive()])
        self.assertEqual(len(client.walk_results), 1)
        self.assertIn("No SNMP response", client.walk_results[0]["error"])

    def test_failed_upload_retries_on_redelivery(self):
        client = RecordingCentralClient(self.devices)
        walker = _FakeDiagWalker(result=self.WalkResult())
        runner = daemon._DiagWalkRunner(client, Config(), walker=walker)
        client.fail_walk_result = True
        self._run(runner, [_walk_directive()])
        self.assertEqual(client.walk_results, [])
        client.fail_walk_result = False
        self._run(runner, [_walk_directive()])  # re-delivery retries
        self.assertEqual(len(walker.calls), 2)
        self.assertEqual(len(client.walk_results), 1)

    def test_run_cycle_hands_reply_walks_to_the_runner(self):
        devices = [_dev(1, "10.0.0.9", snmp_enabled=True, snmp_community="public")]
        prober = _FakeProber({
            "10.0.0.9": lambda: PingResult("10.0.0.9", 5.0, 0.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices, replies=[
            {"ok": True, "snmp_walks": [_walk_directive()]}])
        walker = _FakeDiagWalker(result=self.WalkResult(
            varbinds=[("1.3.6.1.2.1.1.5.0", "sw1")]))
        runner = daemon._DiagWalkRunner(client, Config(), walker=walker)

        async def scenario():
            await daemon.run_cycle_central_brain(
                prober, client, devices, "1.1.1.1", Config(retry_interval_s=0),
                walk_runner=runner)
            if runner._task is not None:
                await runner._task
        asyncio.run(scenario())
        self.assertEqual(len(client.walk_results), 1)
        self.assertEqual(client.walk_results[0]["walk_id"], 7)


class HealthPassthroughTest(unittest.TestCase):
    def test_health_readings_attach_to_the_full_report(self):
        # Regression: HttpCentralClient.report once lacked the health kwarg, so
        # every cycle with a health sweep result died on a TypeError.
        devices = [_dev(1, "10.0.0.1")]
        prober = _FakeProber({
            "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
            "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
        })
        client = RecordingCentralClient(devices)
        readings = {1: {"cpu_pct": 40.0, "temp_c": 51.0}}
        asyncio.run(daemon.run_cycle_central_brain(
            prober, client, devices, "1.1.1.1", Config(retry_interval_s=0),
            health=readings))
        self.assertEqual(client.reports[0]["health"], readings)

    def test_http_client_report_accepts_health(self):
        import inspect
        from wisp.runtime.central_client import HttpCentralClient
        self.assertIn("health", inspect.signature(HttpCentralClient.report).parameters)


class FollowRecheckTest(unittest.TestCase):

    def _cfg(self, **over):
        return Config(retry_interval_s=0.001, down_consecutive=3, recover_consecutive=2,
                     **over)

    def test_follows_hint_until_empty(self):
        prober = _FakeProber({"10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0)})
        client = RecordingCentralClient([], replies=[
            {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}},
            {"ok": True},
        ])
        first_reply = {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}}
        asyncio.run(daemon._follow_recheck(prober, client, first_reply, self._cfg()))
        self.assertEqual(len(client.reports), 2)
        self.assertTrue(all(r["mode"] == "recheck" for r in client.reports))
        self.assertEqual(client.reports[0]["pings"]["10.0.0.1"]["loss_pct"], 100.0)

    def test_stops_immediately_when_no_hint(self):
        prober = _FakeProber({})
        client = RecordingCentralClient([])
        asyncio.run(daemon._follow_recheck(prober, client, {"ok": True}, self._cfg()))
        self.assertEqual(client.reports, [])

    def test_stops_at_round_cap_even_if_central_keeps_hinting(self):
        prober = _FakeProber({"10.0.0.1": lambda: PingResult("10.0.0.1", None, 100.0)})
        always_hint = {"recheck": {"down_ips": ["10.0.0.1"], "interval_s": 0.001}}
        client = RecordingCentralClient([], replies=[always_hint] * 20)
        cfg = self._cfg()
        asyncio.run(daemon._follow_recheck(prober, client, always_hint, cfg))
        self.assertEqual(len(client.reports), 5)

class RunForeverCentralBrainTest(unittest.TestCase):
    def test_refetches_topology_and_reports_each_cycle(self):
        devices = [_dev(1, "10.0.0.1")]
        client = RecordingCentralClient(devices)

        async def fake_preflight():
            return None

        class _Prober(_FakeProber):
            def __init__(self):
                super().__init__({
                    "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
                    "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
                })

            async def preflight(self):
                await fake_preflight()

        prober = _Prober()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(poll_interval_s=0, db_path=Path(tmp) / "wisp.db")

            async def _run():
                import unittest.mock as mock
                with mock.patch.object(daemon, "build_prober", return_value=prober), \
                     mock.patch.object(daemon, "build_central_client", return_value=client):
                    await daemon.run_forever_central_brain(cfg, interval=0, max_cycles=2)

            asyncio.run(_run())
        self.assertEqual(len(client.reports), 2)
        self.assertEqual(client.fetch_calls, 1)

    def test_aborts_when_initial_fetch_fails(self):
        client = RecordingCentralClient([], fail_fetch=True)

        class _Prober(_FakeProber):
            def __init__(self):
                super().__init__({})

            async def preflight(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(db_path=Path(tmp) / "wisp.db")

            async def _run():
                import unittest.mock as mock
                with mock.patch.object(daemon, "build_prober", return_value=_Prober()), \
                     mock.patch.object(daemon, "build_central_client", return_value=client):
                    await daemon.run_forever_central_brain(cfg, interval=0, max_cycles=1)

            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(_run())
            self.assertEqual(ctx.exception.code, 2)
            status = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(status["phase"], "error")
            self.assertIn("cannot fetch devices", status["error"])

    def test_status_file_tracks_cycles(self):
        devices = [_dev(1, "10.0.0.1")]

        class _Prober(_FakeProber):
            def __init__(self):
                super().__init__({
                    "10.0.0.1": lambda: PingResult("10.0.0.1", 5.0, 0.0),
                    "1.1.1.1": lambda: PingResult("1.1.1.1", 5.0, 0.0),
                })

            async def preflight(self):
                return None

        for fail_report, expect_ok in ((False, True), (True, False)):
            client = RecordingCentralClient(devices, fail_report=fail_report)
            with tempfile.TemporaryDirectory() as tmp:
                cfg = Config(db_path=Path(tmp) / "wisp.db", retry_interval_s=0)

                async def _run():
                    import unittest.mock as mock
                    with mock.patch.object(daemon, "build_prober", return_value=_Prober()), \
                         mock.patch.object(daemon, "build_central_client",
                                           return_value=client):
                        await daemon.run_forever_central_brain(cfg, interval=0, max_cycles=1)

                asyncio.run(_run())
                status = json.loads((Path(tmp) / "status.json").read_text())
                self.assertEqual(status["phase"], "running")
                self.assertEqual(status["ok"], expect_ok)
                self.assertEqual(status["devices"], 1)

class SendHeartbeatTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(db_path=Path(self._tmp.name) / "wisp.db")
        self.request_path = Path(self._tmp.name) / "update_request.json"

    def test_reports_version_and_platform(self):
        client = RecordingCentralClient([])
        daemon._send_heartbeat(client, self.cfg, fleet_size=7)
        self.assertEqual(len(client.heartbeats), 1)
        body = client.heartbeats[0]
        from wisp.version import VERSION, platform_tag
        self.assertEqual(body["version"], VERSION)
        self.assertEqual(body["platform"], platform_tag())
        self.assertEqual(body["fleet_size"], 7)
        from wisp.runtime.meminfo import KEYS
        for key in KEYS:
            self.assertIn(key, body)

    def test_update_directive_dropped_as_request_file(self):
        directive = {"target_version": "9.9.9", "url": "https://x/wisp-edge", "sha256": "ab"}
        client = RecordingCentralClient([], heartbeat_reply={"ok": True, "update": directive})
        daemon._send_heartbeat(client, self.cfg, fleet_size=1)
        self.assertEqual(json.loads(self.request_path.read_text()), directive)

    def test_no_directive_writes_nothing(self):
        client = RecordingCentralClient([])
        daemon._send_heartbeat(client, self.cfg, fleet_size=1)
        self.assertFalse(self.request_path.exists())

    def test_heartbeat_failure_never_raises(self):
        client = RecordingCentralClient([], fail_heartbeat=True)
        daemon._send_heartbeat(client, self.cfg, fleet_size=1)
        self.assertFalse(self.request_path.exists())

if __name__ == "__main__":
    unittest.main()
