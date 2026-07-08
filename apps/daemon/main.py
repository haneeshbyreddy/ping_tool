from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
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
from wisp.ingress.snmp import SnmpPoller, SnmpTarget, build_snmp_poller
from wisp.ingress.gpon import GponPollerPool

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

async def _gather_snmp_ports(
    snmp_poller: SnmpPoller, devices: list[dict], cfg: Config = CONFIG,
) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(max(1, cfg.snmp_max_inflight))

    async def one(d: dict) -> tuple[int, list[dict]] | None:
        target = SnmpTarget(ip=d["ip_address"], community=d.get("snmp_community") or "",
                            port=d.get("snmp_port") or 161, version=d.get("snmp_version") or "2c")
        async with sem:
            try:
                ports = await asyncio.wait_for(
                    snmp_poller.walk(target), timeout=cfg.snmp_walk_timeout_s or None)
            except asyncio.TimeoutError:
                log.warning("SNMP walk of device %s (%s) exceeded %.0fs cap; skipping",
                            d.get("id"), d["ip_address"], cfg.snmp_walk_timeout_s)
                return None
            except Exception:
                log.exception("SNMP walk failed for device %s (%s); continuing",
                              d.get("id"), d["ip_address"])
                return None
        return d["id"], [
            {"if_index": p.if_index, "if_name": p.if_name, "if_alias": p.if_alias,
             "admin_status": p.admin_status, "oper_status": p.oper_status,
             "last_change": p.last_change, "in_octets": p.in_octets,
             "out_octets": p.out_octets, "speed_bps": p.speed_bps}
            for p in ports
        ]

    pairs = await asyncio.gather(*(one(d) for d in devices if d.get("snmp_enabled")))
    return {dev_id: ports for dev_id, ports in (p for p in pairs if p)}

async def _gather_onu_optics(
    pool: GponPollerPool, devices: list[dict], cfg: Config = CONFIG,
) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(max(1, cfg.snmp_max_inflight))

    async def one(d: dict) -> tuple[int, list[dict]] | None:
        target = SnmpTarget(ip=d["ip_address"], community=d.get("snmp_community") or "",
                            port=d.get("snmp_port") or 161, version=d.get("snmp_version") or "2c")
        poller = pool.for_vendor(d.get("gpon_vendor"))
        async with sem:
            try:
                onus = await asyncio.wait_for(
                    poller.walk(target), timeout=cfg.snmp_walk_timeout_s or None)
            except asyncio.TimeoutError:
                log.warning("GPON walk of OLT %s (%s) exceeded %.0fs cap; skipping",
                            d.get("id"), d["ip_address"], cfg.snmp_walk_timeout_s)
                return None
            except Exception:
                log.exception("GPON walk failed for OLT %s (%s); continuing",
                              d.get("id"), d["ip_address"])
                return None
        return d["id"], [o.to_wire() for o in onus]

    eligible = [d for d in devices
                if d.get("snmp_enabled") and (d.get("device_type") or "").upper() == "OLT"]
    pairs = await asyncio.gather(*(one(d) for d in eligible))
    return {dev_id: onus for dev_id, onus in (p for p in pairs if p)}

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
) -> bool:
    prober.on_cycle_start()
    ts = _utc_now_iso()
    plan = _gentle_probe_plan(devices, canary_ip, cfg)
    results = await _gather_pings(
        prober, sorted(plan), plan, max_inflight=cfg.probe_max_inflight
    )
    if ports is None:
        ports = await _gather_snmp_ports(snmp_poller, devices, cfg) if snmp_poller else {}
    if optics is None:
        optics = await _gather_onu_optics(gpon_pool, devices, cfg) if gpon_pool else {}
    extra: dict = {}
    if ports:
        extra["ports"] = ports
    if optics:
        extra["optics"] = optics
    try:
        reply = client.report(_pings_payload(results), ts, **extra)
    except CentralClientError as exc:
        log.warning("central report failed: %s", exc)
        return False
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

    cli_interval = interval
    interval = cli_interval if cli_interval is not None else cfg.effective_interval(len(devices))
    status.set_interval(interval)
    print(
        f"central-brain mode: probing {len(devices)} device(s) for "
        f"{cfg.org_id}/{cfg.node_id} every {interval}s -> {cfg.central_url} "
        f"[prober={cfg.prober}] (Ctrl-C to stop)"
    )

    snmp_poller = build_snmp_poller(cfg) if cfg.snmp_interval_s > 0 else None
    next_snmp = 0.0
    snmp_task: asyncio.Task | None = None
    gpon_pool = GponPollerPool(cfg) if cfg.snmp_interval_s > 0 else None
    gpon_task: asyncio.Task | None = None

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        if max_cycles is None:
            try:
                topo = client.fetch_devices()
                devices = topo.get("devices") or devices
                canary_ip = topo.get("canary_ip") or canary_ip
            except CentralClientError as exc:
                log.warning("topology refresh failed, probing last-known set: %s", exc)
        started = asyncio.get_running_loop().time()
        if (max_cycles is None and started >= next_snmp
                and snmp_task is None and gpon_task is None):
            if snmp_poller is not None:
                snmp_task = asyncio.create_task(
                    _gather_snmp_ports(snmp_poller, list(devices), cfg))
            if gpon_pool is not None:
                gpon_task = asyncio.create_task(
                    _gather_onu_optics(gpon_pool, list(devices), cfg))
            next_snmp = started + cfg.snmp_interval_s
        ports: dict[int, list[dict]] | None = None
        if snmp_task is not None and snmp_task.done():
            try:
                ports = snmp_task.result()
            except Exception:
                log.exception("SNMP sweep failed; continuing")
            snmp_task = None
        optics: dict[int, list[dict]] | None = None
        if gpon_task is not None and gpon_task.done():
            try:
                optics = gpon_task.result()
            except Exception:
                log.exception("GPON optics sweep failed; continuing")
            gpon_task = None
        try:
            reported = await run_cycle_central_brain(
                prober, client, devices, canary_ip, cfg, ports=ports, optics=optics)
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

def main() -> None:
    parser = argparse.ArgumentParser(description="Village WISP polling daemon")
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
