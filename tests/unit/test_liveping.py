"""Live ping: the hub's bounds, the probers' stream, and the FSM firewall.

The isolation tests here are the STRUCTURAL half of the guarantee — they read
the source and assert what it may not reach. The behavioural half (a full
session against a real server leaving device state, events and outages
untouched) lives in `integration/test_central_liveping.py`. Both are needed:
the source test catches an import somebody adds in a hurry, the live one
catches a path that reaches the engine without naming it.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.liveping import (_ARM_S, LivePingHub, MIN_EDGE_VERSION,
                                   STOP_EXPIRED, STOP_OPERATOR, STOP_REFUSED,
                                   is_infra)
from wisp.ingress.probers import SingleSocketIcmpProber
from wisp.version import version_tuple

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "wisp"
_HUB = _SRC / "central" / "liveping.py"
_ROUTES = _SRC / "central" / "api" / "liveping.py"
_EDGE = _SRC / "ingress" / "liveping.py"


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


class FsmIsolationTest(unittest.TestCase):
    """A live-ping packet may never reach the state machine.

    `api/edge.report()` routes `mode="recheck"` into
    `central_engine.run_cycle`. If live packets reached that, an operator
    merely WATCHING a device would move its flap counters and could page a
    human at 3am about a device that is fine. These tests pin the reason that
    is impossible rather than merely untrue today.
    """

    # Anything that can move device state, emit an event, or send a page.
    _FORBIDDEN = ("wisp.central.engine", "wisp.central.dispatch",
                  "wisp.central.store", "wisp.core.state_machine",
                  "wisp.central.ports", "wisp.central.perf",
                  "wisp.central.optics", "wisp.egress.notifiers",
                  "wisp.central.notify_policy")

    def test_the_hub_imports_nothing_that_can_reach_the_fsm(self):
        mods = _imported_modules(_HUB)
        for bad in self._FORBIDDEN:
            self.assertFalse(
                any(m == bad or m.startswith(bad + ".") for m in mods),
                f"central/liveping.py imports {bad} — the hub holds no handle"
                f" on the engine, the store or a notifier, and that is what"
                f" makes 'a live ping cannot page anyone' structural")

    def test_the_hub_reaches_no_wisp_module_at_all(self):
        self.assertEqual(
            [m for m in _imported_modules(_HUB) if m.startswith("wisp")], [],
            "the hub is pure stdlib on purpose — nothing to reach the rest of"
            " central WITH")

    def test_the_routes_never_touch_the_engine(self):
        mods = _imported_modules(_ROUTES)
        for bad in ("wisp.central.engine", "wisp.central.dispatch",
                    "wisp.egress.notifiers"):
            self.assertNotIn(bad, mods)

    def test_no_liveping_source_mentions_run_cycle_or_PingResult(self):
        """The two names that would BE the leak, in any of the three files.

        `run_cycle` is the FSM's door and `PingResult` is the only shape it
        accepts. A live sample is `(seq, rtt)` precisely so that feeding one to
        the engine takes a conversion somebody has to write on purpose.
        """
        for path in (_HUB, _ROUTES, _EDGE):
            body = path.read_text()
            code = "\n".join(
                line for line in body.splitlines()
                if not line.strip().startswith("#"))
            # Strip docstrings so the explanations above may name the danger.
            tree = ast.parse(body)
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            for bad in ("run_cycle", "PingResult", "compute_recheck"):
                self.assertNotIn(bad, names, f"{path.name} references {bad}")
            self.assertNotIn("MonitorEngine", code)

    def test_the_edge_tunnel_never_reaches_the_report_path(self):
        mods = _imported_modules(_EDGE)
        self.assertNotIn("wisp.core.state_machine", mods)
        # It may import the prober PROTOCOL, but not the result type the
        # report envelope is built from.
        self.assertNotIn("wisp.ingress.probers.PingResult", mods)


class HubBoundsTest(unittest.TestCase):

    def setUp(self):
        self.hub = LivePingHub(max_s=300, interval_ms=1000,
                               infra_interval_ms=2000, max_per_org=2)

    def _start(self, device_id=1, org="ispA", node="probe1", infra=False,
               now=None):
        return self.hub.start(org_id=org, node_id=node, device_id=device_id,
                              device_ip=f"10.0.0.{device_id}", infra=infra,
                              started_by="ravi", now=now)

    def test_the_rate_is_one_packet_a_second(self):
        sess, err = self._start()
        self.assertIsNone(err)
        self.assertEqual(sess.interval_ms, 1000)

    def test_aggregation_gear_gets_the_slower_cadence(self):
        sess, _ = self._start(infra=True)
        self.assertEqual(sess.interval_ms, 2000)

    def test_a_faster_rate_cannot_be_configured_below_the_floor(self):
        hub = LivePingHub(interval_ms=1, infra_interval_ms=1)
        self.assertGreaterEqual(hub.interval_ms, 200)
        self.assertGreaterEqual(hub.infra_interval_ms, hub.interval_ms)

    def test_the_five_minutes_run_from_FIRST_CONTACT_not_from_the_click(self):
        t0 = 1_000_000.0
        sess, _ = self._start(now=t0)
        # Waiting for the probe: the arming bound, not the ping budget.
        self.assertLess(sess.expires_at - sess.started_at, 300.0)

        # The probe answers 100 s later. The five minutes start THERE, so the
        # operator gets the packets they asked for rather than five minutes
        # minus however long the channel took to wake up.
        self.hub.mark_picked_up("ispA", "probe1", now=t0 + 100)
        self.assertAlmostEqual(sess.expires_at, t0 + 100 + 300.0)
        self.assertTrue(sess.live(t0 + 399))
        self.assertFalse(sess.live(t0 + 401))

    def test_first_contact_moves_the_deadline_once_and_only_once(self):
        t0 = 1_000_000.0
        sess, _ = self._start(now=t0)
        self.hub.mark_picked_up("ispA", "probe1", now=t0 + 10)
        deadline = sess.expires_at
        self.hub.mark_picked_up("ispA", "probe1", now=t0 + 20)
        self.hub.ingest("ispA", "probe1", sess.sid, [[1, 5.0]], now=t0 + 30)
        self.assertEqual(sess.expires_at, deadline)

    def test_the_arming_clock_is_a_real_deadline(self):
        t0 = 1_000_000.0
        sess, _ = self._start(now=t0)
        self.assertFalse(sess.live(t0 + _ARM_S + 1))
        # And a probe turning up afterwards does not restart it.
        self.hub.mark_picked_up("ispA", "probe1", now=t0 + _ARM_S + 2)
        self.assertIsNone(sess.picked_up_at)

    def test_an_expired_session_drops_out_of_the_probes_set(self):
        """The auto-stop reaches the probe with no message being sent."""
        self._start(now=time.time() - 10_000)
        self.assertEqual(self.hub.exchange("ispA", "probe1", token=0,
                                           hold_s=0, deliver=True)[1], [])

    def test_an_expired_session_stops_itself_and_says_why(self):
        t0 = time.time() - 10_000
        sess, _ = self._start(now=t0)
        # Any read reaps: the deadline is a clock fact, not an event.
        self.hub.org_live_count("ispA")
        self.assertEqual(sess.stop_reason, STOP_EXPIRED)
        self.assertFalse(sess.live(time.time()))

    def test_an_expired_session_stops_accepting_samples(self):
        t0 = time.time() - 10_000
        sess, _ = self._start(now=t0)
        self.hub.ingest("ispA", "probe1", sess.sid, [[1, 5.0]])
        self.assertEqual(sess.sent, 0)

    def test_one_session_per_device_and_the_second_viewer_joins_it(self):
        first, _ = self._start(device_id=7)
        second, err = self._start(device_id=7)
        self.assertIsNone(err)
        self.assertIs(second, first)
        self.assertEqual(self.hub.org_live_count("ispA"), 1)

    def test_the_per_org_cap_refuses_the_next_one(self):
        self._start(device_id=1)
        self._start(device_id=2)
        sess, err = self._start(device_id=3)
        self.assertIsNone(sess)
        self.assertIn("2", err)
        # ...and the cap is per ORG, not global.
        other, err2 = self._start(device_id=4, org="ispB")
        self.assertIsNotNone(other)
        self.assertIsNone(err2)

    def test_a_stopped_session_frees_its_slot(self):
        a, _ = self._start(device_id=1)
        self._start(device_id=2)
        self.assertIsNone(self._start(device_id=3)[0])
        self.hub.stop(a.sid, "ispA")
        self.assertIsNotNone(self._start(device_id=3)[0])
        self.assertEqual(a.stop_reason, STOP_OPERATOR)

    def test_a_session_that_ran_out_keeps_expired_as_its_reason(self):
        """Only one of the two reasons is a fact about a person."""
        sess, _ = self._start(now=time.time() - 10_000)
        self.assertFalse(self.hub.stop(sess.sid, "ispA"))
        self.assertEqual(sess.stop_reason, STOP_EXPIRED)

    def test_a_stopped_session_stays_readable_so_the_panel_can_say_so(self):
        sess, _ = self._start()
        self.hub.ingest("ispA", "probe1", sess.sid, [[1, 4.0]])
        self.hub.stop(sess.sid, "ispA")
        found = self.hub.for_device("ispA", 1)
        self.assertIsNotNone(found)
        self.assertFalse(found.public(time.time())["live"])
        self.assertEqual(self.hub.read(found, 0)[0], [[1, 4.0]])

    def test_a_session_belongs_to_its_org(self):
        sess, _ = self._start()
        self.assertFalse(self.hub.stop(sess.sid, "ispB"))
        self.assertIsNone(self.hub.for_device("ispB", 1))
        self.hub.ingest("ispB", "probe1", sess.sid, [[1, 4.0]])
        self.assertEqual(sess.sent, 0)


class SampleFoldTest(unittest.TestCase):

    def setUp(self):
        self.hub = LivePingHub()
        self.sess, _ = self.hub.start(
            org_id="ispA", node_id="probe1", device_id=1, device_ip="10.0.0.1",
            infra=False, started_by="ravi")

    def test_a_lost_packet_keeps_its_sequence_number(self):
        self.hub.ingest("ispA", "probe1", self.sess.sid,
                        [[1, 4.0], [2, None], [3, None], [4, 5.0]])
        self.assertEqual(self.hub.read(self.sess, 0)[0],
                         [[1, 4.0], [2, None], [3, None], [4, 5.0]])
        self.assertEqual(self.sess.sent, 4)
        self.assertEqual(self.sess.lost, 2)

    def test_the_cursor_only_returns_what_is_new(self):
        self.hub.ingest("ispA", "probe1", self.sess.sid, [[1, 4.0], [2, 5.0]])
        self.assertEqual(self.hub.read(self.sess, 1)[0], [[2, 5.0]])
        self.assertEqual(self.hub.read(self.sess, 2)[0], [])

    def test_a_replayed_batch_is_not_counted_twice(self):
        self.hub.ingest("ispA", "probe1", self.sess.sid, [[1, 4.0], [2, 5.0]])
        self.hub.ingest("ispA", "probe1", self.sess.sid, [[1, 4.0], [2, 5.0]])
        self.assertEqual(self.sess.sent, 2)
        self.assertEqual(self.hub.read(self.sess, 0)[0], [[1, 4.0], [2, 5.0]])

    def test_junk_is_dropped_not_charted(self):
        self.hub.ingest("ispA", "probe1", self.sess.sid,
                        [["x", 1.0], [1, "nope"], [2, -5.0], [3, 999_999.0],
                         [4, 6.0], "garbage", [5]])
        self.assertEqual(self.hub.read(self.sess, 0)[0],
                         [[1, None], [2, None], [3, None], [4, 6.0]])

    def test_the_probe_picking_it_up_is_visible_to_the_panel(self):
        self.assertFalse(self.sess.public(time.time())["picked_up"])
        self.hub.mark_picked_up("ispA", "probe1")
        self.assertTrue(self.sess.public(time.time())["picked_up"])

    def test_a_refusal_stops_the_session_and_names_the_reason(self):
        self.hub.stop(self.sess.sid, "ispA", reason=STOP_REFUSED,
                      detail="target is not a device this node probes")
        pub = self.sess.public(time.time())
        self.assertEqual(pub["stop_reason"], STOP_REFUSED)
        self.assertIn("not a device", pub["stop_detail"])


class EdgeExchangeTest(unittest.TestCase):

    def setUp(self):
        self.hub = LivePingHub()

    def test_the_reply_is_the_whole_truth_about_what_to_ping(self):
        a, _ = self.hub.start(org_id="ispA", node_id="probe1", device_id=1,
                              device_ip="10.0.0.1", infra=False, started_by="o")
        token, sessions = self.hub.exchange("ispA", "probe1", token=0,
                                            hold_s=0, deliver=True)
        self.assertEqual([s["sid"] for s in sessions], [a.sid])
        self.assertEqual(sessions[0]["device_ip"], "10.0.0.1")
        self.hub.stop(a.sid, "ispA")
        _, after = self.hub.exchange("ispA", "probe1", token=token,
                                     hold_s=0, deliver=True)
        self.assertEqual(after, [],
                         "a stop is a session leaving the set, so it cannot be"
                         " a message that goes missing")

    def test_another_nodes_session_is_never_handed_over(self):
        self.hub.start(org_id="ispA", node_id="probe1", device_id=1,
                       device_ip="10.0.0.1", infra=False, started_by="o")
        self.assertEqual(self.hub.exchange("ispA", "probe2", token=0,
                                           hold_s=0, deliver=True)[1], [])
        self.assertEqual(self.hub.exchange("ispB", "probe1", token=0,
                                           hold_s=0, deliver=True)[1], [])

    def test_an_idle_channel_holds_and_a_start_releases_it(self):
        import threading
        out: list = []

        def poll():
            out.append(self.hub.exchange("ispA", "probe1", token=0,
                                         hold_s=5.0, deliver=False))

        t = threading.Thread(target=poll, daemon=True)
        started = time.monotonic()
        t.start()
        time.sleep(0.15)
        self.hub.start(org_id="ispA", node_id="probe1", device_id=1,
                       device_ip="10.0.0.1", infra=False, started_by="o")
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(len(out[0][1]), 1)

    def test_the_node_wake_flag_follows_live_work(self):
        self.assertFalse(self.hub.node_has_work("ispA", "probe1"))
        sess, _ = self.hub.start(org_id="ispA", node_id="probe1", device_id=1,
                                 device_ip="10.0.0.1", infra=False,
                                 started_by="o")
        self.assertTrue(self.hub.node_has_work("ispA", "probe1"))
        self.assertFalse(self.hub.node_has_work("ispA", "probe2"))
        self.hub.stop(sess.sid, "ispA")
        self.assertFalse(self.hub.node_has_work("ispA", "probe1"))


class InfraTest(unittest.TestCase):

    def test_a_device_with_children_is_infra(self):
        devs = [{"id": 1, "parent_device_id": None},
                {"id": 2, "parent_device_id": 1},
                {"id": 3, "parent_device_id": 2}]
        self.assertTrue(is_infra(1, devs))
        self.assertTrue(is_infra(2, devs))
        self.assertFalse(is_infra(3, devs))


class VersionGateTest(unittest.TestCase):

    def test_an_older_probe_is_below_the_gate(self):
        self.assertLess(version_tuple("0.15.1"), version_tuple(MIN_EDGE_VERSION))
        self.assertLess(version_tuple(None), version_tuple(MIN_EDGE_VERSION))
        self.assertGreaterEqual(version_tuple("0.16.0"),
                                version_tuple(MIN_EDGE_VERSION))
        self.assertGreaterEqual(version_tuple("1.0.0"),
                                version_tuple(MIN_EDGE_VERSION))


# -- the prober's stream ---------------------------------------------------

def _fake_socket_factory(prober_box, drop: set[int] | None = None):
    """A raw-socket stand-in that replies to every echo except `drop`ped seqs."""
    import queue
    import struct
    from wisp.ingress.probers import icmp_checksum

    class Sock:
        def __init__(self):
            self.q: queue.Queue = queue.Queue()
            self.sent = 0
            self._timeout = 1.0

        def settimeout(self, t):
            self._timeout = t

        def sendto(self, data, addr):
            self.sent += 1
            if drop and self.sent in drop:
                return
            ident, seq = struct.unpack("!HH", data[4:8])
            payload = data[8:]
            head = struct.pack("!BBHHH", 0, 0, 0, ident, seq)
            csum = icmp_checksum(head + payload)
            icmp = struct.pack("!BBHHH", 0, 0, csum, ident, seq) + payload
            self.q.put((bytes([0x45]) + bytes(19) + icmp, (addr[0], 0)))

        def recvfrom(self, n):
            try:
                item = self.q.get(timeout=self._timeout)
            except queue.Empty:
                raise socket.timeout()
            if item is None:
                raise OSError("closed")
            return item

        def close(self):
            self.q.put(None)

    sock = Sock()
    prober_box.append(sock)
    return lambda: sock


class PingStreamTest(unittest.TestCase):

    def _drain(self, prober, ip="10.0.0.5", count=4, interval=0.0):
        async def go():
            return [s async for s in prober.ping_stream(
                ip, count=count, interval=interval)]
        return asyncio.run(go())

    def test_every_echo_arrives_with_its_own_sequence_number(self):
        box: list = []
        p = SingleSocketIcmpProber(timeout=0.5,
                                   sock_factory=_fake_socket_factory(box))
        self.addCleanup(p.close)
        got = self._drain(p, count=4)
        self.assertEqual([seq for seq, _ in got], [1, 2, 3, 4])
        self.assertTrue(all(rtt is not None for _, rtt in got))

    def test_a_lost_packet_is_a_gap_at_a_known_sequence(self):
        """Three in a row lost and three scattered are different facts.

        This is why the stream carries seq at all: an average and a loss
        percentage cannot tell those apart, and the panel must not draw them
        alike.
        """
        box: list = []
        p = SingleSocketIcmpProber(timeout=0.15,
                                   sock_factory=_fake_socket_factory(box, drop={2, 3}))
        self.addCleanup(p.close)
        got = self._drain(p, count=4)
        self.assertEqual([seq for seq, _ in got], [1, 2, 3, 4])
        self.assertIsNone(got[1][1])
        self.assertIsNone(got[2][1])
        self.assertIsNotNone(got[0][1])
        self.assertIsNotNone(got[3][1])

    def test_the_sequence_is_session_local_not_the_wire_seq(self):
        """A wrapped 16-bit wire seq would draw a gap that never happened."""
        box: list = []
        p = SingleSocketIcmpProber(timeout=0.5,
                                   sock_factory=_fake_socket_factory(box))
        self.addCleanup(p.close)
        p._seq = 65530  # about to wrap
        got = self._drain(p, count=8)
        self.assertEqual([seq for seq, _ in got], list(range(1, 9)))

    def test_ping_is_untouched_by_the_stream_existing(self):
        """The probe cycle depends on `ping`'s exact shape."""
        box: list = []
        p = SingleSocketIcmpProber(timeout=0.5,
                                   sock_factory=_fake_socket_factory(box, drop={2}))
        self.addCleanup(p.close)
        res = asyncio.run(p.ping("10.0.0.5", 4))
        self.assertEqual(res.ip, "10.0.0.5")
        self.assertEqual(res.packet_loss, 25.0)
        self.assertIsNotNone(res.latency_ms)
        self.assertIsNotNone(res.jitter_ms)

    def test_a_zero_count_yields_nothing(self):
        box: list = []
        p = SingleSocketIcmpProber(sock_factory=_fake_socket_factory(box))
        self.addCleanup(p.close)
        self.assertEqual(self._drain(p, count=0), [])

    def test_the_interval_is_a_ceiling_on_the_rate(self):
        box: list = []
        p = SingleSocketIcmpProber(timeout=0.5,
                                   sock_factory=_fake_socket_factory(box))
        self.addCleanup(p.close)
        started = time.monotonic()
        got = self._drain(p, count=3, interval=0.12)
        elapsed = time.monotonic() - started
        self.assertEqual(len(got), 3)
        # Two gaps between three packets, and never faster than the ceiling.
        self.assertGreaterEqual(elapsed, 0.24 - 0.05)


class IcmplibStreamTest(unittest.TestCase):
    """The icmplib prober streams by asking for ONE packet at a time.

    Honest, not faked: with count=1 the aggregate IS the single measurement.
    """

    def test_it_yields_one_sample_per_echo_and_marks_losses(self):
        from wisp.ingress.probers import IcmpProber, PingResult
        p = IcmpProber()
        calls: list[int] = []

        async def fake_ping(ip, count):
            calls.append(count)
            if len(calls) == 2:
                return PingResult(ip, None, 100.0)
            return PingResult(ip, 7.5, 0.0, 0.0)

        p.ping = fake_ping

        async def go():
            return [s async for s in p.ping_stream("10.0.0.5", count=3,
                                                   interval=0.0)]
        got = asyncio.run(go())
        self.assertEqual(calls, [1, 1, 1],
                         "one echo per tick, so nothing is averaged")
        self.assertEqual(got, [(1, 7.5), (2, None), (3, 7.5)])


class _FakeCentral:
    """A central that hands back a scripted sequence of session sets."""

    def __init__(self, script: list[list[dict]]):
        self.script = list(script)
        self.calls: list[dict] = []
        self.samples: dict[str, list] = {}
        self.refusals: list[dict] = []

    def liveping_exchange(self, token, samples, refusals, hold_s):
        self.calls.append({"token": token, "samples": samples,
                           "refusals": refusals, "hold_s": hold_s})
        for sid, batch in (samples or {}).items():
            self.samples.setdefault(sid, []).extend(batch)
        self.refusals.extend(refusals or [])
        sessions = self.script.pop(0) if self.script else []
        return {"token": token + 1, "sessions": sessions}


class _StubProber:
    def __init__(self, per_tick: float = 0.0):
        self.per_tick = per_tick
        self.runs: list[tuple[str, int, float]] = []

    async def ping(self, ip, count):
        raise AssertionError("live ping must never call the cycle's ping()")

    async def ping_stream(self, ip, *, count, interval):
        self.runs.append((ip, count, interval))
        for seq in range(1, count + 1):
            if self.per_tick:
                await asyncio.sleep(self.per_tick)
            yield seq, 4.2


class EdgeTunnelTest(unittest.TestCase):
    """The probe half: reconcile to the declared set, and refuse the rest."""

    def _tunnel(self, central, prober, devices, cfg=None):
        from wisp.config import Config
        from wisp.ingress.liveping import LivePingTunnel
        cfg = cfg or Config(liveping_poll_hold_s=0.05, liveping_max_s=300)
        return LivePingTunnel(central, cfg, prober=prober,
                              devices_provider=lambda: devices)

    def test_a_session_it_is_handed_gets_pinged(self):
        central = _FakeCentral([[{"sid": "s1", "device_ip": "10.0.0.5",
                                  "interval_ms": 1000, "remaining_s": 5.0}]])
        prober = _StubProber()

        async def go():
            t = self._tunnel(central, prober, [{"ip_address": "10.0.0.5"}])
            t.notify(True)
            for _ in range(40):
                await asyncio.sleep(0.02)
                if central.samples.get("s1"):
                    break
            await t.aclose()

        asyncio.run(go())
        self.assertEqual(prober.runs[0][0], "10.0.0.5")
        self.assertEqual(prober.runs[0][2], 1.0)
        self.assertTrue(central.samples["s1"])
        self.assertEqual(central.samples["s1"][0], [1, 4.2])

    def test_the_probe_refuses_a_target_it_does_not_probe(self):
        """Central resolves a device row; the probe checks it anyway.

        Not redundant: this is what stops the channel becoming a way to aim
        packets at an arbitrary address, whatever central asks for. Same rule
        the remote diag walk makes.
        """
        central = _FakeCentral([[{"sid": "s1", "device_ip": "8.8.8.8",
                                  "interval_ms": 1000, "remaining_s": 60.0}]])
        prober = _StubProber()

        async def go():
            t = self._tunnel(central, prober, [{"ip_address": "10.0.0.5"}])
            t.notify(True)
            for _ in range(40):
                await asyncio.sleep(0.02)
                if central.refusals:
                    break
            await t.aclose()

        asyncio.run(go())
        self.assertEqual(prober.runs, [], "not one packet was sent")
        self.assertEqual(central.refusals[0]["sid"], "s1")
        self.assertIn("not a device", central.refusals[0]["error"])

    def test_the_probe_bounds_the_run_itself_if_central_goes_silent(self):
        """`remaining_s` becomes a packet COUNT, so the generator runs out.

        A probe left pinging a customer's gear because the other end of a
        tunnel died is exactly what this feature must not be able to cause.
        """
        central = _FakeCentral([[{"sid": "s1", "device_ip": "10.0.0.5",
                                  "interval_ms": 1000, "remaining_s": 4.0}]])
        prober = _StubProber()

        async def go():
            t = self._tunnel(central, prober, [{"ip_address": "10.0.0.5"}])
            t.notify(True)
            for _ in range(40):
                await asyncio.sleep(0.02)
                if prober.runs:
                    break
            await t.aclose()

        asyncio.run(go())
        self.assertEqual(prober.runs[0][1], 4, "four seconds is four packets")

    def test_a_session_leaving_the_set_stops_the_pinger(self):
        central = _FakeCentral([
            [{"sid": "s1", "device_ip": "10.0.0.5", "interval_ms": 1000,
              "remaining_s": 300.0}],
            [],
        ])
        prober = _StubProber(per_tick=0.01)
        seen: dict = {}

        async def go():
            t = self._tunnel(central, prober, [{"ip_address": "10.0.0.5"}])
            t.notify(True)
            for _ in range(60):
                await asyncio.sleep(0.02)
                if len(central.calls) >= 2:
                    break
            seen["tasks"] = dict(t._sessions)
            await t.aclose()

        asyncio.run(go())
        self.assertEqual(seen["tasks"], {},
                         "a stop is a session leaving the set, not a message")

    def test_the_channel_stays_dormant_until_it_is_woken(self):
        central = _FakeCentral([])
        prober = _StubProber()

        async def go():
            t = self._tunnel(central, prober, [{"ip_address": "10.0.0.5"}])
            t.notify(False)
            await asyncio.sleep(0.15)
            await t.aclose()

        asyncio.run(go())
        self.assertEqual(central.calls, [],
                         "no wake-up means no parked request on central")

    def test_a_prober_with_no_stream_is_reported_not_faked(self):
        class Plain:
            async def ping(self, ip, count):
                raise AssertionError("must not be called")

        central = _FakeCentral([[{"sid": "s1", "device_ip": "10.0.0.5",
                                  "interval_ms": 1000, "remaining_s": 60.0}]])

        async def go():
            t = self._tunnel(central, Plain(), [{"ip_address": "10.0.0.5"}])
            t.notify(True)
            for _ in range(40):
                await asyncio.sleep(0.02)
                if central.refusals:
                    break
            await t.aclose()

        asyncio.run(go())
        self.assertIn("stream", central.refusals[0]["error"])


if __name__ == "__main__":
    unittest.main()


class SpentSessionTest(unittest.IsolatedAsyncioTestCase):
    """A pinger that ran its budget out is NOT restarted.

    `_reconcile` deleted any task that was `done()` and rebuilt it if central
    still listed the session. But `ping_stream` numbers from 1 every time and
    central drops anything at or below `high_seq`, so the rebuilt run is
    invisible: the panel goes dead while the probe keeps pinging the device.
    Benign when both ends agree on `liveping_max_s`; permanent the moment the
    edge's is shorter, since the edge finishes first every time.
    """

    async def test_a_finished_session_is_not_rebuilt(self):
        from wisp.config import Config
        from wisp.ingress.liveping import LivePingTunnel

        starts = []

        class _Prober:
            async def ping_stream(self, ip, *, count, interval):
                starts.append(ip)
                for seq in range(1, 3):
                    yield seq, 1.0

        tunnel = LivePingTunnel(_FakeCentral([]), Config(), prober=_Prober(),
                                devices_provider=lambda: [{"ip_address": "10.0.0.3"}])
        spec = {"sid": "s1", "device_ip": "10.0.0.3", "interval_ms": 1,
                "remaining_s": 300}
        tunnel._reconcile([spec])
        await asyncio.sleep(0.05)
        self.assertEqual(len(starts), 1)

        # Central still lists it: the budget is spent, not the session.
        tunnel._reconcile([spec])
        await asyncio.sleep(0.05)
        self.assertEqual(len(starts), 1, "a spent session was restarted")

        # Central drops it, then a genuinely NEW session on the same device
        # must still start — "spent" may not become a permanent refusal.
        tunnel._reconcile([])
        tunnel._reconcile([dict(spec, sid="s2")])
        await asyncio.sleep(0.05)
        self.assertEqual(len(starts), 2)
        await tunnel.aclose()

    async def test_a_spent_sid_does_not_leak_once_central_drops_it(self):
        from wisp.config import Config
        from wisp.ingress.liveping import LivePingTunnel

        class _Prober:
            async def ping_stream(self, ip, *, count, interval):
                yield 1, 1.0

        tunnel = LivePingTunnel(_FakeCentral([]), Config(), prober=_Prober(),
                                devices_provider=lambda: [{"ip_address": "10.0.0.3"}])
        spec = {"sid": "s1", "device_ip": "10.0.0.3", "interval_ms": 1,
                "remaining_s": 300}
        tunnel._reconcile([spec])
        await asyncio.sleep(0.05)
        tunnel._reconcile([spec])
        tunnel._reconcile([])
        self.assertEqual(tunnel._spent, set())
        await tunnel.aclose()
