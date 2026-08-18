"""The edge half of live ping: its own channel, dormant until asked.

Shape copied from `ProxyTunnel` because the invariant is the same one — **the
edge never accepts an inbound connection**, it dials central — but this is its
OWN channel, not a passenger on the proxy's. Live ping must not be gated behind
`orgs.web_proxy`: that flag is a superadmin grant for a browser session onto a
device's admin page, which is a far larger thing than watching a box answer
pings, and an ISP that has not been given the web tunnel still has technicians
standing in front of broken gear.

Nothing here touches the probe cycle. Live sessions run on their own asyncio
tasks, produce `(seq, rtt|None)` pairs, and post them to `POST /edge/liveping`.
They never enter `_gather_pings`, never become a `PingResult`, and never ride
the report envelope — which is what keeps a watched device's packets out of the
state machine.

WAKE-UP AND ITS COST
--------------------
The channel is dormant by default. It arms when a `/report` reply carries
`liveping: true`, so the first session of a quiet spell waits for the probe's
next report — the poll cadence, clamped 10-120 s. That wait is REAL and the
dashboard prints it rather than spinning. It is the price of "the edge dials
central, never the reverse" plus "don't make the long-poll always-on", and both
of those are worth more than a few seconds. Once armed, the channel stays up
for `_ARM_TTL_S` past the last session, so start/stop and any session after the
first land in about a second.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from wisp.config import CONFIG, Config
from wisp.ingress.probers import Prober
from wisp.runtime.central_client import CentralBrainClient, CentralClientError

log = logging.getLogger("wisp.edge.liveping")


class LivePingTunnel:

    # How long the channel stays awake after the last session disappears. Long
    # enough that a technician stopping and restarting, or moving to the next
    # device, does not pay the report-cadence wait twice; short enough that an
    # idle fleet holds no parked requests on central.
    _ARM_TTL_S = 120.0

    def __init__(self, client: CentralBrainClient, cfg: Config = CONFIG, *,
                 prober: Prober, devices_provider: Callable[[], list[dict]]) -> None:
        self._client = client
        self._cfg = cfg
        self._prober = prober
        self._devices = devices_provider
        self._deadline = 0.0
        self._running = False
        self._worker: asyncio.Task | None = None
        self._sessions: dict[str, asyncio.Task] = {}
        # Sessions whose pinger ran its budget out while central still lists
        # them. See `_reconcile`: they must not be restarted.
        self._spent: set[str] = set()
        self._outbox: dict[str, list] = {}
        self._refusals: list[dict] = []
        self._nudge = asyncio.Event()
        self._token = 0

    # -- arming ------------------------------------------------------------

    def notify(self, flag: bool) -> None:
        """Called with the `/report` reply's `liveping` flag. Never raises."""
        if not flag or not self._cfg.liveping_enabled:
            return
        self._deadline = max(self._deadline, time.monotonic() + self._ARM_TTL_S)
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._running = True
        self._worker = asyncio.create_task(self._loop(), name="liveping-tunnel")
        log.info("live ping channel armed")

    async def aclose(self) -> None:
        self._running = False
        tasks = [t for t in (self._worker, *self._sessions.values()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._sessions.clear()
        self._worker = None

    # -- the exchange loop -------------------------------------------------

    async def _loop(self) -> None:
        while self._running and time.monotonic() < self._deadline:
            if not self._outbox and not self._refusals and self._sessions:
                # A session is running, so something will arrive within a tick.
                # Wait for it rather than parking a long poll, or the panel
                # would receive twenty lines at once instead of one a second.
                try:
                    await asyncio.wait_for(self._nudge.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._nudge.clear()
            samples, refusals = self._outbox, self._refusals
            self._outbox, self._refusals = {}, []
            if samples or refusals:
                hold = 0.0
            elif self._sessions:
                hold = 2.0
            else:
                hold = float(self._cfg.liveping_poll_hold_s)
            try:
                reply = await asyncio.to_thread(
                    self._client.liveping_exchange, self._token, samples,
                    refusals, hold)
            except CentralClientError as exc:
                # Put the batch back: a dropped exchange must lose a second of
                # the stream at most, and a gap the viewer can see is better
                # than a renumbered one they cannot.
                self._requeue(samples, refusals)
                log.debug("live ping exchange failed, backing off: %s", exc)
                await asyncio.sleep(2.0)
                continue
            except asyncio.CancelledError:
                return
            reply = reply or {}
            try:
                self._token = int(reply.get("token") or 0)
            except (TypeError, ValueError):
                self._token = 0
            sessions = reply.get("sessions") or []
            if sessions:
                self._deadline = max(self._deadline,
                                     time.monotonic() + self._ARM_TTL_S)
            self._reconcile(sessions)
        self._reconcile([])
        log.debug("live ping channel standing down")

    def _requeue(self, samples: dict, refusals: list) -> None:
        for sid, batch in samples.items():
            self._outbox.setdefault(sid, [])[:0] = batch
        self._refusals[:0] = refusals

    def _reconcile(self, sessions: list) -> None:
        """Match the running pingers to the set central just declared.

        Declarative, not a command log: whatever is in the reply is what should
        be running. A stop is a session no longer being in the set, so there is
        no stop message that can go missing, and a probe that restarts simply
        picks up the current truth on its first exchange.
        """
        wanted: dict[str, dict] = {}
        for s in sessions:
            if isinstance(s, dict) and s.get("sid"):
                wanted[str(s["sid"])] = s
        for sid, task in list(self._sessions.items()):
            if sid in wanted and not task.done():
                continue
            if sid in wanted:
                # Finished its packet budget while central still lists the
                # session. It must NOT be cancelled and rebuilt: `ping_stream`
                # numbers from 1 every time and central drops anything at or
                # below `high_seq`, so a restart is invisible — the panel goes
                # dead while the probe keeps pinging the device. Keep the
                # outbox too; those samples are still owed. Bites whenever the
                # edge's `liveping_max_s` is shorter than central's.
                self._spent.add(sid)
                del self._sessions[sid]
                continue
            task.cancel()
            del self._sessions[sid]
            self._outbox.pop(sid, None)
        self._spent &= set(wanted)
        for sid, spec in wanted.items():
            if sid in self._sessions or sid in self._spent:
                continue
            self._sessions[sid] = asyncio.create_task(
                self._run(sid, spec), name=f"liveping-{sid[:8]}")

    # -- one session -------------------------------------------------------

    def _refuse(self, sid: str, msg: str) -> None:
        self._refusals.append({"sid": sid, "error": msg})
        self._nudge.set()

    async def _run(self, sid: str, spec: dict) -> None:
        ip = str(spec.get("device_ip") or "")
        # The probe's OWN refusal, and it is not redundant with central's
        # device-row lookup: this is the check that keeps the channel from
        # being a way to aim packets at an arbitrary address, whatever central
        # asks for. Same rule the remote diag walk makes — it refuses target
        # IPs outside the node's device list.
        if ip not in {d.get("ip_address") for d in (self._devices() or [])}:
            self._refuse(sid, "target is not a device this node probes")
            return
        stream_fn = getattr(self._prober, "ping_stream", None)
        if stream_fn is None:
            self._refuse(sid, "this prober cannot stream individual echoes")
            return
        interval = max(0.2, float(spec.get("interval_ms") or 1000) / 1000.0)
        try:
            budget = float(spec.get("remaining_s") or 0.0)
        except (TypeError, ValueError):
            budget = 0.0
        # The edge's own hard stop, and the reason it exists: if central goes
        # silent mid-session, the generator still runs out. A probe left
        # pinging a customer's gear because the other end of a tunnel died is
        # exactly the failure this feature must not be able to cause.
        budget = min(budget, float(self._cfg.liveping_max_s))
        count = int(budget // interval)
        if count <= 0:
            return
        deadline = time.monotonic() + budget + interval
        try:
            async for seq, rtt in stream_fn(ip, count=count, interval=interval):
                self._outbox.setdefault(sid, []).append([seq, rtt])
                self._nudge.set()
                if time.monotonic() >= deadline:
                    break
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            # Config/permission faults are the loud ones (no ping group, no raw
            # socket). They are the operator's answer, not a silent blank panel.
            self._refuse(sid, str(exc)[:200])
        except Exception as exc:
            log.debug("live ping session %s failed", sid[:8], exc_info=True)
            self._refuse(sid, str(exc)[:200] or exc.__class__.__name__)


def build_live_ping_tunnel(client: CentralBrainClient, cfg: Config, *,
                           prober: Prober,
                           devices_provider: Callable[[], list[dict]]
                           ) -> LivePingTunnel:
    return LivePingTunnel(client, cfg, prober=prober,
                          devices_provider=devices_provider)
