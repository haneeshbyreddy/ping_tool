from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG, Config
from wisp.runtime.central_client import (
    CentralBrainClient,
    CentralClientError,
    build_central_client,
)
from wisp.runtime.edge_status import (
    PHASE_ERROR,
    PHASE_RUNNING,
    PHASE_STARTING,
    StatusWriter,
    status_path,
)
from wisp.runtime.single_instance import AlreadyRunning, SingleInstance
from wisp.ingress.probers import PingResult, Prober, build_prober
from wisp.ingress.health import HealthPoller, build_health_poller
from wisp.ingress.snmp import (
    PortStatus, SnmpPoller, SnmpTarget, build_snmp_poller, missing_detail,
)
from wisp.ingress.gpon import GponPollerPool
from wisp.ingress.liveping import LivePingTunnel, build_live_ping_tunnel
from wisp.ingress.webproxy import ProxyTunnel, build_proxy_tunnel

log = logging.getLogger("wisp.daemon")

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

async def _gather_pings(
    prober: Prober,
    ips: list[str],
    count: int | dict[str, int],
    *,
    max_inflight: int | None = None,
) -> dict[str, PingResult]:
    limit = max_inflight or len(ips) or 1
    sem = asyncio.Semaphore(limit)

    async def one(ip: str) -> tuple[str, PingResult]:
        n = count[ip] if isinstance(count, dict) else count
        async with sem:
            try:
                return ip, await prober.ping(ip, n)
            except RuntimeError:
                raise
            except Exception:
                return ip, PingResult(ip, None, 100.0)

    pairs = await asyncio.gather(*(one(ip) for ip in ips))
    return dict(pairs)

def _gentle_probe_plan(devices: list[dict], canary_ip: str, cfg: Config) -> dict[str, int]:
    parent_ids = {d["parent_device_id"] for d in devices if d["parent_device_id"] is not None}
    plan = {d["ip_address"]: (cfg.pings_per_poll_infra if d["id"] in parent_ids
                              else cfg.pings_per_poll)
            for d in devices}
    plan[canary_ip] = cfg.pings_per_poll
    return plan

def _pings_payload(results: dict[str, PingResult]) -> dict:
    return {
        ip: {"loss_pct": r.packet_loss, "latency_ms": r.latency_ms, "jitter_ms": r.jitter_ms}
        for ip, r in results.items()
    }


def _snmp_target(d: dict) -> SnmpTarget:
    return SnmpTarget(ip=d["ip_address"], community=d.get("snmp_community") or "",
                      port=d.get("snmp_port") or 161,
                      version=d.get("snmp_version") or "2c")

class _SnmpAirtime:

    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(max(1, limit))
        self._locks: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def slot(self, ip: str):
        async with self._locks.setdefault(ip, asyncio.Lock()):
            async with self._sem:
                yield

# How long the walk itself took, in seconds, started AFTER the airtime gates are
# held. Queueing behind a busy box is contention, not walk cost — the number has
# to answer "can this walk finish inside a faster clock?", which is a property of
# the walk. Monotonic, so a clock step can't invent a duration. Every outcome
# carries one: a walk that TIMED OUT is exactly the one whose duration matters.
def _walk_elapsed(t0: float) -> float:
    return round(time.monotonic() - t0, 2)

def _rest_after(due: float, landed: float, interval: float) -> float:
    """When may the next sweep start, given the one that just landed?

    `due` is stamped at LAUNCH, so a sweep that ran longer than its interval
    lands with its own deadline already in the past and the next loop
    iteration starts another one immediately — the device gets walked back to
    back with no gap at all. That is what CLAUDE.md means by "the polling
    caused the failure": a box that cannot answer inside its interval needs
    LESS polling, not continuous polling.

    So an overrunning sweep is treated as if it had launched when it landed,
    and a sweep that finished in time is left alone — the normal cadence keeps
    its phase and does not drift by a probe cycle per sweep.
    """
    return landed + interval if landed >= due else due

def _classify_snmp_exc(exc: Exception) -> tuple[str, str]:
    msg = str(exc)[:200]
    low = msg.lower()
    if "no snmp response" in low or "timed out" in low or "timeout" in low:
        return "no_response", msg
    return "error", msg

# A key the walk did not fetch is OMITTED, so central preserves what it stored;
# a key that arrived empty rides as None, which is authoritative.
_WIRE_OPTIONAL = ("if_name", "if_alias", "last_change", "in_octets",
                  "out_octets", "speed_bps")

def _wire_port(p: PortStatus) -> dict:
    row = {"if_index": p.if_index, "if_name": p.if_name, "if_alias": p.if_alias,
           "admin_status": p.admin_status, "oper_status": p.oper_status,
           "last_change": p.last_change, "in_octets": p.in_octets,
           "out_octets": p.out_octets, "speed_bps": p.speed_bps}
    if p.carried is not None:
        for key in _WIRE_OPTIONAL:
            if key not in p.carried:
                del row[key]
    return row

class _PortScopePlanner:

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._every = cfg.port_identity_interval_s
        self._full_at: dict[int, float] = {}
        self._indexes: dict[int, frozenset[int]] = {}
        self._refresh: set[int] = set()

    def plan(self, devices: list[dict], now: float) -> dict[int, str]:
        scopes: dict[int, str] = {}
        for d in devices:
            if not d.get("snmp_enabled"):
                continue
            last = self._full_at.get(d["id"])
            full = (last is None or d["id"] in self._refresh
                    or (self._every > 0 and now - last >= self._every))
            scopes[d["id"]] = "full" if full else "hot"
        return scopes

    def record(self, scopes: dict[int, str], data: dict[int, list[dict]],
               now: float) -> None:
        for dev_id, scope in scopes.items():
            rows = data.get(dev_id)
            if not rows:
                continue
            seen = frozenset(r["if_index"] for r in rows)
            if scope == "full":
                self._full_at[dev_id] = now
                self._indexes[dev_id] = seen
                self._refresh.discard(dev_id)
            elif seen - self._indexes.get(dev_id, frozenset()):
                self._refresh.add(dev_id)

async def _gather_snmp_ports(
    snmp_poller: SnmpPoller, devices: list[dict], cfg: Config = CONFIG,
    gate: _SnmpAirtime | None = None, scopes: dict[int, str] | None = None,
) -> tuple[dict[int, list[dict]], dict[int, dict]]:
    gate = gate or _SnmpAirtime(cfg.snmp_max_inflight)
    scopes = scopes or {}

    async def one(d: dict) -> tuple[int, list[dict] | None, dict]:
        async with gate.slot(d["ip_address"]):
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    snmp_poller.walk(_snmp_target(d),
                                     scope=scopes.get(d["id"], "full")),
                    timeout=cfg.port_walk_timeout_s or None)
            except asyncio.TimeoutError:
                log.warning("SNMP port walk of device %s (%s) exceeded %.0fs cap; skipping",
                            d.get("id"), d["ip_address"], cfg.port_walk_timeout_s)
                return d["id"], None, {
                    "state": "timeout", "elapsed_s": _walk_elapsed(t0),
                    "detail": f"port walk exceeded {cfg.port_walk_timeout_s:.0f}s"}
            except Exception as exc:
                log.exception("SNMP walk failed for device %s (%s); continuing",
                              d.get("id"), d["ip_address"])
                state, detail = _classify_snmp_exc(exc)
                return d["id"], None, {"state": state, "detail": detail,
                                       "elapsed_s": _walk_elapsed(t0)}
            took = _walk_elapsed(t0)
        if not result.ports:
            return d["id"], None, {
                "state": "empty", "elapsed_s": took,
                "detail": "agent answered but the interface table had no rows"}
        wire = [_wire_port(p) for p in result.ports]
        if result.missing_columns:
            return d["id"], wire, {
                "state": "partial", "count": len(result.ports), "elapsed_s": took,
                "detail": missing_detail(result.missing_columns)}
        return d["id"], wire, {"state": "ok", "count": len(result.ports),
                               "elapsed_s": took}

    rows = await asyncio.gather(*(one(d) for d in devices if d.get("snmp_enabled")))
    data = {dev_id: wire for dev_id, wire, _ in rows if wire is not None}
    return data, {dev_id: st for dev_id, _, st in rows}

async def _gather_snmp_health(
    health_poller: HealthPoller, devices: list[dict], cfg: Config = CONFIG,
    profiles: list[dict] | None = None,
    gate: _SnmpAirtime | None = None,
) -> tuple[dict[int, dict], dict[int, dict]]:
    gate = gate or _SnmpAirtime(cfg.snmp_max_inflight)

    async def one(d: dict) -> tuple[int, dict | None, dict]:
        async with gate.slot(d["ip_address"]):
            t0 = time.monotonic()
            try:
                reading = await asyncio.wait_for(
                    health_poller.walk(_snmp_target(d), profiles),
                    timeout=cfg.snmp_walk_timeout_s or None)
            except asyncio.TimeoutError:
                log.warning("SNMP health walk of device %s (%s) exceeded %.0fs cap; skipping",
                            d.get("id"), d["ip_address"], cfg.snmp_walk_timeout_s)
                return d["id"], None, {
                    "state": "timeout", "elapsed_s": _walk_elapsed(t0),
                    "detail": f"health walk exceeded {cfg.snmp_walk_timeout_s:.0f}s"}
            except Exception as exc:
                log.exception("SNMP health walk failed for device %s (%s); continuing",
                              d.get("id"), d["ip_address"])
                state, detail = _classify_snmp_exc(exc)
                return d["id"], None, {"state": state, "detail": detail,
                                       "elapsed_s": _walk_elapsed(t0)}
            took = _walk_elapsed(t0)
        status: dict = {"sysobjectid": reading.sysobjectid,
                        "profile": reading.profile_name, "elapsed_s": took}
        if reading.health.is_empty():
            if not reading.responded:
                status.update(state="no_response",
                              detail="agent never answered any subtree")
            elif reading.profile_name:
                status.update(
                    state="empty",
                    detail=f"profile {reading.profile_name!r} matched but returned"
                           " no readings")
            else:
                status.update(
                    state="empty",
                    detail="agent answered but exposes no standard health OIDs —"
                           " needs a vendor profile")
            return d["id"], None, status
        status.update(state="ok")
        return d["id"], reading.health.to_wire(), status

    rows = await asyncio.gather(*(one(d) for d in devices if d.get("snmp_enabled")))
    data = {dev_id: h for dev_id, h, _ in rows if h is not None}
    return data, {dev_id: st for dev_id, _, st in rows}

async def _gather_onu_optics(
    pool: GponPollerPool, devices: list[dict], cfg: Config = CONFIG,
    gate: _SnmpAirtime | None = None,
) -> tuple[dict[int, list[dict]], dict[int, dict]]:
    gate = gate or _SnmpAirtime(cfg.snmp_max_inflight)

    async def one(d: dict) -> tuple[int, list[dict] | None, dict]:
        target = _snmp_target(d)
        async with gate.slot(d["ip_address"]):
            # The sysObjectID read is SNMP work this subsystem pays for on every
            # sweep, so the clock starts before it and covers the whole optics
            # walk — including the runs that end at "no profile claims this OLT".
            t0 = time.monotonic()
            poller, info = await pool.resolve_info(d, target)
            status: dict = {"sysobjectid": info.get("sysobjectid"),
                            "profile": info.get("vendor")}
            if poller is None:
                if info.get("reason") == "no_response":
                    status.update(state="no_response",
                                  detail="sysObjectID read got no answer")
                else:
                    status.update(
                        state="no_profile",
                        detail="no GPON vendor profile claims this OLT — optics"
                               " stay off rather than guessing OIDs")
                status["elapsed_s"] = _walk_elapsed(t0)
                return d["id"], None, status
            try:
                onus = await asyncio.wait_for(
                    poller.walk(target), timeout=cfg.gpon_walk_timeout_s or None)
            except asyncio.TimeoutError:
                log.warning("GPON walk of OLT %s (%s) exceeded %.0fs cap; skipping",
                            d.get("id"), d["ip_address"], cfg.gpon_walk_timeout_s)
                status.update(
                    state="timeout", elapsed_s=_walk_elapsed(t0),
                    detail=f"ONU walk exceeded {cfg.gpon_walk_timeout_s:.0f}s")
                return d["id"], None, status
            except Exception as exc:
                log.exception("GPON walk failed for OLT %s (%s); continuing",
                              d.get("id"), d["ip_address"])
                state, detail = _classify_snmp_exc(exc)
                status.update(state=state, detail=detail,
                              elapsed_s=_walk_elapsed(t0))
                return d["id"], None, status
            took = _walk_elapsed(t0)
        status.update(state="ok", count=len(onus), elapsed_s=took)
        return d["id"], [o.to_wire() for o in onus], status

    eligible = [d for d in devices
                if d.get("snmp_enabled") and (d.get("device_type") or "").upper() == "OLT"]
    rows = await asyncio.gather(*(one(d) for d in eligible))
    data = {dev_id: onus for dev_id, onus, _ in rows if onus is not None}
    return data, {dev_id: st for dev_id, _, st in rows}

def _merge_snmp_status(into: dict[int, dict], subsystem: str,
                       statuses: dict[int, dict] | None) -> None:
    for dev_id, st in (statuses or {}).items():
        into.setdefault(dev_id, {})[subsystem] = st

class _DiagWalkRunner:

    def __init__(self, client: CentralBrainClient, cfg: Config = CONFIG,
                 walker=None, gate: _SnmpAirtime | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._walker = walker
        self._gate = gate or _SnmpAirtime(cfg.snmp_max_inflight)
        self._queue: list[tuple[dict, bool]] = []
        self._seen: set[int] = set()
        self._task: asyncio.Task | None = None

    def accept(self, walks: list | None, devices: list[dict]) -> None:
        if not walks:
            return
        allowed = {d["ip_address"] for d in devices}
        for w in walks:
            try:
                wid = int(w.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if wid in self._seen:
                continue
            self._seen.add(wid)
            self._queue.append((w, w.get("ip_address") in allowed))
        if self._queue and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._queue:
            w, allowed = self._queue.pop(0)
            wid = int(w["id"])
            try:
                if not allowed:
                    self._client.walk_result(
                        wid, error="target is not a device this node probes")
                    continue
                if self._walker is None:
                    from wisp.ingress.walker import build_diag_walker
                    self._walker = build_diag_walker(self._cfg)
                target = SnmpTarget(
                    ip=w["ip_address"], community=w.get("snmp_community") or "",
                    port=w.get("snmp_port") or 161,
                    version=w.get("snmp_version") or "2c")
                async with self._gate.slot(target.ip):
                    res = await self._walker.walk(
                        target, w.get("root_oid") or "1.3.6.1",
                        int(w.get("max_varbinds") or 2000))
                self._client.walk_result(
                    wid, varbinds=[[o, v] for o, v in res.varbinds],
                    truncated=res.truncated)
                log.info("diagnostic walk %d of %s done: %d varbinds%s", wid,
                         w["ip_address"], len(res.varbinds),
                         " (truncated)" if res.truncated else "")
            except CentralClientError as exc:
                log.warning("walk %d result upload failed, will retry on"
                            " re-delivery: %s", wid, exc)
                self._seen.discard(wid)
            except Exception as exc:
                log.exception("diagnostic walk %d failed", wid)
                try:
                    self._client.walk_result(wid, error=str(exc)[:500])
                except CentralClientError:
                    self._seen.discard(wid)

async def _follow_recheck(
    prober: Prober, client: CentralBrainClient, reply: dict, cfg: Config,
) -> None:
    rounds = 0
    cap = max(cfg.down_consecutive, cfg.recover_consecutive) + 2
    recheck = reply.get("recheck")
    while recheck and rounds < cap:
        interval = recheck.get("interval_s") or cfg.retry_interval_s
        ips = sorted(set(recheck.get("down_ips") or []) | set(recheck.get("up_ips") or []))
        if interval <= 0 or not ips:
            return
        await asyncio.sleep(interval)
        counts = {ip: 1 for ip in ips}
        probe_res = await _gather_pings(
            prober, ips, counts, max_inflight=cfg.probe_max_inflight
        )
        try:
            reply = client.report(_pings_payload(probe_res), _utc_now_iso(), mode="recheck")
        except CentralClientError as exc:
            log.warning("central recheck report failed: %s", exc)
            return
        recheck = reply.get("recheck")
        rounds += 1

def _send_heartbeat(client: CentralBrainClient, cfg: Config, fleet_size: int) -> None:
    from wisp.version import VERSION, platform_tag
    from wisp.runtime.meminfo import memory_snapshot
    body = {"version": VERSION, "platform": platform_tag(),
            "fleet_size": fleet_size, "last_poll_ts": _utc_now_iso(),
            **memory_snapshot()}
    try:
        reply = client.heartbeat(body)
    except CentralClientError as exc:
        log.warning("central heartbeat failed: %s", exc)
        return
    if (reply or {}).get("restart"):
        restart_path = Path(cfg.db_path).parent / "restart_request.json"
        tmp = restart_path.with_name("restart_request.json.tmp")
        restart_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"requested_at": _utc_now_iso()}))
        os.replace(tmp, restart_path)
        log.info("central requested an agent restart; dropped %s", restart_path)
    directive = (reply or {}).get("update")
    if not directive:
        return
    request_path = Path(cfg.db_path).parent / "update_request.json"
    tmp = request_path.with_name("update_request.json.tmp")
    request_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(directive))
    os.replace(tmp, request_path)
    log.info("update directive for %s dropped at %s",
             directive.get("target_version"), request_path)

async def run_cycle_central_brain(
    prober: Prober, client: CentralBrainClient, devices: list[dict], canary_ip: str,
    cfg: Config = CONFIG, *, snmp_poller: SnmpPoller | None = None,
    ports: dict[int, list[dict]] | None = None,
    gpon_pool: GponPollerPool | None = None,
    optics: dict[int, list[dict]] | None = None,
    health: dict[int, dict] | None = None,
    snmp_status: dict[int, dict] | None = None,
    walk_runner: _DiagWalkRunner | None = None,
    proxy_tunnel: ProxyTunnel | None = None,
    live_tunnel: LivePingTunnel | None = None,
) -> bool:
    prober.on_cycle_start()
    ts = _utc_now_iso()
    plan = _gentle_probe_plan(devices, canary_ip, cfg)
    results = await _gather_pings(
        prober, sorted(plan), plan, max_inflight=cfg.probe_max_inflight
    )
    snmp_status = dict(snmp_status or {})
    if ports is None:
        ports, st = (await _gather_snmp_ports(snmp_poller, devices, cfg)
                     if snmp_poller else ({}, {}))
        _merge_snmp_status(snmp_status, "ports", st)
    if optics is None:
        optics, st = (await _gather_onu_optics(gpon_pool, devices, cfg)
                      if gpon_pool else ({}, {}))
        _merge_snmp_status(snmp_status, "optics", st)
    extra: dict = {}
    if ports:
        extra["ports"] = ports
    if optics:
        extra["optics"] = optics
    if health:
        extra["health"] = health
    if snmp_status:
        extra["snmp_status"] = snmp_status
    try:
        reply = client.report(_pings_payload(results), ts, **extra)
    except CentralClientError as exc:
        log.warning("central report failed: %s", exc)
        return False
    if walk_runner is not None:
        walk_runner.accept(reply.get("snmp_walks"), devices)
    if proxy_tunnel is not None:
        proxy_tunnel.notify_sessions(reply.get("proxy_sessions"))
        proxy_tunnel.notify_standby(bool(reply.get("proxy_standby")))
    # The live-ping wake-up, and the ONLY contact live ping has with this
    # function: a boolean off the reply, going one way. Nothing a live session
    # measures comes back through `results`, so a watched device's packets can
    # never reach the report envelope and therefore never reach the FSM.
    if live_tunnel is not None:
        live_tunnel.notify(bool(reply.get("liveping")))
    if cfg.retry_interval_s > 0:
        await _follow_recheck(prober, client, reply, cfg)
    return True

def _maybe_dump_memory(cfg: Config, cycle: int) -> None:
    every = cfg.tracemalloc_every
    if every <= 0 or cycle % every != 0:
        return
    try:
        import gc
        import tracemalloc
        if not tracemalloc.is_tracing():
            tracemalloc.start(25)
            return
        current, peak = tracemalloc.get_traced_memory()
        log.warning(
            "tracemalloc @ cycle %d: python-traced=%.1f MiB (peak %.1f MiB), live objects=%d",
            cycle, current / 1e6, peak / 1e6, len(gc.get_objects()))
        for stat in tracemalloc.take_snapshot().statistics("lineno")[:10]:
            log.warning("  %s", stat)
    except Exception:
        log.exception("tracemalloc dump failed; continuing")

async def run_forever_central_brain(
    cfg: Config = CONFIG, *, interval: float | None = None, max_cycles: int | None = None,
) -> None:
    client = build_central_client(cfg)
    try:
        await _run_central_brain(client, cfg, interval=interval, max_cycles=max_cycles)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()

async def _run_central_brain(
    client: CentralBrainClient, cfg: Config = CONFIG, *,
    interval: float | None = None, max_cycles: int | None = None,
) -> None:
    prober = build_prober(cfg)
    from wisp.version import VERSION
    status = StatusWriter(
        status_path(cfg.db_path), org_id=cfg.org_id, node_id=cfg.node_id,
        central_url=cfg.central_url,
        interval_s=interval if interval is not None else cfg.poll_interval_s,
        version=VERSION)
    status.write(PHASE_STARTING)
    preflight = getattr(prober, "preflight", None)
    if preflight is not None:
        try:
            await preflight()
        except RuntimeError as exc:
            log.error("prober preflight failed — refusing to start: %s", exc)
            status.write(PHASE_ERROR, ok=False, error=str(exc))
            raise SystemExit(2)

    try:
        topo = client.fetch_devices()
    except CentralClientError as exc:
        log.error("could not fetch the device list from central — refusing to start: %s", exc)
        status.write(PHASE_ERROR, ok=False,
                     error=f"cannot fetch devices from {cfg.central_url or '<no URL set>'}: {exc}")
        raise SystemExit(2)
    devices = topo.get("devices") or []
    canary_ip = topo.get("canary_ip") or cfg.canary_ip
    snmp_profiles = topo.get("snmp_profiles") or []
    gpon_profiles = topo.get("gpon_profiles")

    def _pick_interval(central_s) -> float:
        if cli_interval is not None:
            return cli_interval
        try:
            central_s = int(central_s or 0)
        except (TypeError, ValueError):
            central_s = 0
        if central_s > 0:
            return float(min(120, max(10, central_s)))
        return float(cfg.effective_interval(len(devices)))

    cli_interval = interval
    interval = _pick_interval(topo.get("poll_interval_s"))
    status.set_interval(interval)
    print(
        f"central-brain mode: probing {len(devices)} device(s) for "
        f"{cfg.org_id}/{cfg.node_id} every {interval}s -> {cfg.central_url} "
        f"[prober={cfg.prober}] (Ctrl-C to stop)"
    )

    airtime = _SnmpAirtime(cfg.snmp_max_inflight) if cfg.snmp_interval_s > 0 else None
    snmp_poller = build_snmp_poller(cfg) if cfg.snmp_interval_s > 0 else None
    next_ports = 0.0
    snmp_task: asyncio.Task | None = None
    port_plan = _PortScopePlanner(cfg)
    port_scopes: dict[int, str] = {}
    gpon_pool = GponPollerPool(cfg) if cfg.snmp_interval_s > 0 else None
    if gpon_pool is not None:
        gpon_pool.set_profiles(gpon_profiles)
    next_optics = 0.0
    gpon_task: asyncio.Task | None = None
    health_poller = build_health_poller(cfg) if cfg.snmp_interval_s > 0 else None
    next_health = 0.0
    health_task: asyncio.Task | None = None
    walk_runner = (_DiagWalkRunner(client, cfg, gate=airtime)
                   if cfg.snmp_interval_s > 0 else None)

    proxy_client = build_central_client(cfg) if cfg.proxy_enabled else None
    proxy_tunnel = (build_proxy_tunnel(proxy_client, cfg, lambda: devices)
                    if proxy_client is not None else None)

    # Its own client, like the proxy's: the exchange parks for up to
    # `liveping_poll_hold_s`, and parking that on the client the report cycle
    # uses would stall the sweep. The PROBER is shared on purpose — one raw
    # socket, one receiver thread is the Windows invariant, and a live reading
    # that came from a second socket could disagree with the sweep's.
    live_client = build_central_client(cfg) if cfg.liveping_enabled else None
    live_tunnel = (build_live_ping_tunnel(live_client, cfg, prober=prober,
                                          devices_provider=lambda: devices)
                   if live_client is not None else None)

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        if max_cycles is None:
            try:
                topo = client.fetch_devices()
                devices = topo.get("devices") or devices
                canary_ip = topo.get("canary_ip") or canary_ip
                snmp_profiles = topo.get("snmp_profiles") or snmp_profiles
                if topo.get("gpon_profiles") is not None:
                    gpon_profiles = topo.get("gpon_profiles")
                new_interval = _pick_interval(topo.get("poll_interval_s"))
                if new_interval != interval:
                    log.info("probe interval changed %.0fs -> %.0fs (central org"
                             " setting)", interval, new_interval)
                    interval = new_interval
                    status.set_interval(interval)
            except CentralClientError as exc:
                log.warning("topology refresh failed, probing last-known set: %s", exc)
        started = asyncio.get_running_loop().time()
        if max_cycles is None:
            if snmp_poller is not None and snmp_task is None and started >= next_ports:
                port_scopes = port_plan.plan(devices, started)
                snmp_task = asyncio.create_task(
                    _gather_snmp_ports(snmp_poller, list(devices), cfg,
                                       gate=airtime, scopes=port_scopes))
                next_ports = started + cfg.port_interval_s
            if gpon_pool is not None and gpon_task is None and started >= next_optics:
                gpon_pool.set_profiles(gpon_profiles)
                gpon_task = asyncio.create_task(
                    _gather_onu_optics(gpon_pool, list(devices), cfg,
                                       gate=airtime))
                next_optics = started + cfg.gpon_interval_s
            if health_poller is not None and health_task is None and started >= next_health:
                health_task = asyncio.create_task(
                    _gather_snmp_health(health_poller, list(devices), cfg,
                                        list(snmp_profiles), gate=airtime))
                next_health = started + cfg.snmp_interval_s
        snmp_status: dict[int, dict] = {}
        ports: dict[int, list[dict]] | None = None
        if snmp_task is not None and snmp_task.done():
            try:
                ports, st = snmp_task.result()
                port_plan.record(port_scopes, ports, started)
                _merge_snmp_status(snmp_status, "ports", st)
            except Exception:
                log.exception("SNMP sweep failed; continuing")
            snmp_task = None
            # A sweep that OVERRAN its own clock must not re-fire the instant it
            # lands. `next_*` is stamped at LAUNCH, so once a walk takes longer
            # than its interval that deadline is already in the past and the
            # very next loop iteration starts another one — the box gets walked
            # back to back forever. That is the failure CLAUDE.md records as
            # "the polling caused the failure". A walk that cannot answer inside
            # its interval needs LESS polling, not continuous polling, so treat
            # the landing as the launch and give it a full interval. Untouched
            # for a sweep that finished in time, so the normal cadence does not
            # drift.
            next_ports = _rest_after(next_ports, started, cfg.port_interval_s)
        optics: dict[int, list[dict]] | None = None
        if gpon_task is not None and gpon_task.done():
            try:
                optics, st = gpon_task.result()
                _merge_snmp_status(snmp_status, "optics", st)
            except Exception:
                log.exception("GPON optics sweep failed; continuing")
            gpon_task = None
            next_optics = _rest_after(next_optics, started, cfg.gpon_interval_s)
        health: dict[int, dict] | None = None
        if health_task is not None and health_task.done():
            try:
                health, st = health_task.result()
                _merge_snmp_status(snmp_status, "health", st)
            except Exception:
                log.exception("SNMP health sweep failed; continuing")
            health_task = None
            next_health = _rest_after(next_health, started, cfg.snmp_interval_s)
        try:
            reported = await run_cycle_central_brain(
                prober, client, devices, canary_ip, cfg, ports=ports, optics=optics,
                health=health, snmp_status=snmp_status or None,
                walk_runner=walk_runner, proxy_tunnel=proxy_tunnel,
                live_tunnel=live_tunnel)
            status.write(PHASE_RUNNING, ok=reported, devices=len(devices),
                         error=None if reported else "last report to central failed")
        except Exception as exc:
            log.exception("central-brain cycle %d failed; continuing", cycle + 1)
            status.write(PHASE_RUNNING, ok=False, devices=len(devices), error=str(exc))
        if max_cycles is None:
            _send_heartbeat(client, cfg, len(devices))
        cycle += 1
        _maybe_dump_memory(cfg, cycle)
        print(f"cycle {cycle:>3} | reported {len(devices)} device(s) to central")
        if max_cycles is not None and cycle >= max_cycles:
            break
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(0.0, interval - elapsed))

    if proxy_tunnel is not None:
        await proxy_tunnel.aclose()
    if proxy_client is not None:
        close = getattr(proxy_client, "close", None)
        if close is not None:
            close()
    if live_tunnel is not None:
        await live_tunnel.aclose()
    if live_client is not None:
        close = getattr(live_client, "close", None)
        if close is not None:
            close()

def print_status(*, now=None) -> int:

    from wisp.runtime import edge_status
    path = edge_status.status_path(CONFIG.db_path)
    if not path.is_file():
        deb_path = Path("/etc/wisp/status.json")
        if deb_path.is_file():
            path = deb_path
    view = edge_status.read_status(path, now=now)
    print(f"{view.state}: {view.detail}")
    raw = view.raw or {}
    if raw:
        print(f"agent v{raw.get('version')} | org {raw.get('org_id')}"
              f" | node {raw.get('node_id')} -> {raw.get('central_url')}")
    print(f"(status file: {path})")
    return {edge_status.STATE_OK: 0, edge_status.STATE_STARTING: 1,
            edge_status.STATE_DEGRADED: 1}.get(view.state, 2)

def main() -> None:
    parser = argparse.ArgumentParser(description="Village WISP polling daemon")
    parser.add_argument("command", nargs="?", choices=["status"],
                        help="status: print probe health from status.json and exit"
                             " (0 healthy, 1 starting/degraded, 2 stale/error)")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between polls (overrides config)")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after N cycles (default: run forever)")
    parser.add_argument("--version", action="store_true",
                        help="print the build version and exit (the supervisor queries this)")
    args = parser.parse_args()

    if args.version:
        from wisp.version import VERSION
        print(VERSION)
        return

    if args.command == "status":
        raise SystemExit(print_status())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.getLogger("httpx").setLevel(logging.WARNING)

    guard = SingleInstance(f"{CONFIG.db_path}.central-brain.lock")
    try:
        guard.acquire()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        raise SystemExit(3)
    try:
        asyncio.run(run_forever_central_brain(
            interval=args.interval, max_cycles=args.cycles))
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        guard.release()

if __name__ == "__main__":
    main()
