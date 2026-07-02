"""Daemon runtime — the edge's thin probe loop (see CLAUDE.md).

The edge no longer runs a local FSM, alerting, or dashboard: it only learns what
to ping from central (`GET /edge/devices`) and reports the raw per-IP samples
(`POST /report`). All detection and alerting happens on central
(`central/engine.py` + `central/dispatch.py`). This file has exactly one mode —
central-brain — behind `WISP_CENTRAL_URL` + `WISP_CENTRAL_BRAIN=1`
(`Config.central_brain_enabled()`).

    python apps/daemon/main.py                      # forever, reporting to central
    python apps/daemon/main.py --interval 5 --cycles 3    # short run (smoke test)

Zero-install: this entry point puts <repo>/src on sys.path.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- bootstrap: make the `wisp` package importable without installing ---
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG, Config  # noqa: E402
from wisp.runtime.central_client import (
    CentralBrainClient,
    CentralClientError,
    build_central_client,
)
from wisp.runtime.single_instance import AlreadyRunning, SingleInstance
from wisp.ingress.probers import PingResult, Prober, build_prober
from wisp.ingress.snmp import SnmpPoller, SnmpTarget, build_snmp_poller

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
    """Ping every IP, bounding how many probes are in flight at once.

    `count` is either a uniform ping count or a per-IP map (the daemon passes a map
    so aggregation gear is probed gently — see `_gentle_probe_plan`). A naive
    `gather` over every IP opens one socket each in a single tick; past the process
    FD limit the kernel refuses new sockets and every excess probe reads 100% loss —
    a fake mass outage. A semaphore caps the concurrent set so a 10k-device fleet
    clears within the poll window on a few hundred FDs. `max_inflight` falsy =
    unbounded (legacy behaviour; fine for tiny sets)."""
    limit = max_inflight or len(ips) or 1
    sem = asyncio.Semaphore(limit)

    async def one(ip: str) -> tuple[str, PingResult]:
        n = count[ip] if isinstance(count, dict) else count
        async with sem:
            try:
                return ip, await prober.ping(ip, n)
            except RuntimeError:
                # A config/permission failure (icmplib missing, ping group off) is NOT
                # a down host — masking it as 100% loss makes a broken monitor look
                # like a total outage (and trips the canary freeze). Abort the cycle.
                raise
            except Exception:
                # A genuine per-host probe error must never sink the cycle.
                return ip, PingResult(ip, None, 100.0)

    pairs = await asyncio.gather(*(one(ip) for ip in ips))
    return dict(pairs)


def _gentle_probe_plan(devices: list[dict], canary_ip: str, cfg: Config) -> dict[str, int]:
    """Per-IP ping count, mirroring `MonitorEngine.probe_plan()`'s gentle-infra rule
    (any node that is somebody's parent gets fewer echoes so a switch/tower's ICMP
    rate-limiter doesn't read as phantom loss) — computed client-side from the
    central-supplied topology since there's no local engine to ask. Only sees PRIMARY
    parents: `GET /edge/devices` (`store.org_device_topology`) doesn't carry backup edges
    (`org_device_links`), so a device that's a parent ONLY on a backup path (never a
    primary parent of anything) doesn't get the gentle cadence here — a known small gap,
    not central's `probe_plan()` itself (which does see both), and not urgent since a
    backup-path device is typically also a primary parent of something else already."""
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
    """Walk every snmp-enabled device's IF-MIB (mirrors the old single-box daemon's
    `snmp_cycle`), returning {device_id: [port dict, ...]} ready to attach to
    `POST /report`'s `ports` key. Each switch is isolated in its own try/except — a
    dead/blocked switch (or a broken pysnmp) must never sink the ICMP cycle, same
    discipline as `_gather_pings`' per-host guard. `devices` is central's topology
    (`GET /edge/devices`), which now carries each device's SNMP config alongside its
    IP/parent — the edge has no local DB of its own to read credentials from."""
    ports_by_device: dict[int, list[dict]] = {}
    for d in devices:
        if not d.get("snmp_enabled"):
            continue
        target = SnmpTarget(ip=d["ip_address"], community=d.get("snmp_community") or "",
                            port=d.get("snmp_port") or 161, version=d.get("snmp_version") or "2c")
        try:
            ports = await snmp_poller.walk(target)
        except Exception:
            log.exception("SNMP walk failed for device %s (%s); continuing",
                          d.get("id"), d["ip_address"])
            continue
        ports_by_device[d["id"]] = [
            {"if_index": p.if_index, "if_name": p.if_name, "if_alias": p.if_alias,
             "admin_status": p.admin_status, "oper_status": p.oper_status,
             "last_change": p.last_change, "in_octets": p.in_octets,
             "out_octets": p.out_octets, "speed_bps": p.speed_bps}
            for p in ports
        ]
    return ports_by_device


async def _follow_recheck(
    prober: Prober, client: CentralBrainClient, reply: dict, cfg: Config,
) -> None:
    """The fast-confirm round trip: while central's reply carries a `recheck` hint
    (from `central/engine.py:compute_recheck`), re-probe JUST those suspect IPs with a
    single fast echo — mirrors the standalone daemon's old `_confirm_down`/`_confirm_up`
    re-probing suspects every `retry_interval_s`, except the FSM advancing them lives on
    central now, not here. `compute_recheck` guarantees this terminates on its own (a
    suspect leaves the hint the moment it confirms or clears), but a fixed round cap
    guards against a central-side bug ever wedging the probe loop."""
    rounds = 0
    cap = max(cfg.down_consecutive, cfg.recover_consecutive) + 2   # generous safety margin
    recheck = reply.get("recheck")
    while recheck and rounds < cap:
        interval = recheck.get("interval_s") or cfg.retry_interval_s
        ips = sorted(set(recheck.get("down_ips") or []) | set(recheck.get("up_ips") or []))
        if interval <= 0 or not ips:
            return
        await asyncio.sleep(interval)
        counts = {ip: 1 for ip in ips}   # single fast echo — see _gather_pings' docstring
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


async def run_cycle_central_brain(
    prober: Prober, client: CentralBrainClient, devices: list[dict], canary_ip: str,
    cfg: Config = CONFIG, *, snmp_poller: SnmpPoller | None = None,
) -> None:
    """One central-brain cycle: probe this org's topology (+ canary), report the raw
    results, then follow any fast-confirm hint central sends back. All detection/alerting
    happens on central — this function has no opinion on UP/DOWN, it just samples,
    ships, and (if asked) re-samples the suspects a few seconds later.

    `snmp_poller`, when passed (only on cycles due for the SNMP task's own slow
    cadence — see `run_forever_central_brain`), also walks every snmp-enabled
    device's IF-MIB and attaches the haul to this SAME "full" report under `ports`,
    so central's port-folding (`central/ports.py`) runs off the same cycle's outages."""
    prober.on_cycle_start()
    ts = _utc_now_iso()
    plan = _gentle_probe_plan(devices, canary_ip, cfg)
    results = await _gather_pings(
        prober, sorted(plan), plan, max_inflight=cfg.probe_max_inflight
    )
    ports = await _gather_snmp_ports(snmp_poller, devices, cfg) if snmp_poller else {}
    try:
        reply = (client.report(_pings_payload(results), ts, ports=ports) if ports
                else client.report(_pings_payload(results), ts))
    except CentralClientError as exc:
        # A WAN cut or a dead central must never crash the probe loop — just skip this
        # report and try again next cycle (mirrors the old shipper's own isolation).
        log.warning("central report failed: %s", exc)
        return
    if cfg.retry_interval_s > 0:
        await _follow_recheck(prober, client, reply, cfg)


async def run_forever_central_brain(
    cfg: Config = CONFIG, *, interval: float | None = None, max_cycles: int | None = None,
) -> None:
    client = build_central_client(cfg)
    prober = build_prober(cfg)
    preflight = getattr(prober, "preflight", None)
    if preflight is not None:
        try:
            await preflight()
        except RuntimeError as exc:
            log.error("prober preflight failed — refusing to start: %s", exc)
            raise SystemExit(2)

    try:
        topo = client.fetch_devices()
    except CentralClientError as exc:
        log.error("could not fetch the device list from central — refusing to start: %s", exc)
        raise SystemExit(2)
    devices = topo.get("devices") or []
    canary_ip = topo.get("canary_ip") or cfg.canary_ip

    cli_interval = interval
    interval = cli_interval if cli_interval is not None else cfg.effective_interval(len(devices))
    print(
        f"central-brain mode: probing {len(devices)} device(s) for "
        f"{cfg.org_id}/{cfg.node_id} every {interval}s -> {cfg.central_url} "
        f"[prober={cfg.prober}] (Ctrl-C to stop)"
    )

    # SNMP port status runs on its own slow cadence (ports don't flap like radio
    # links) — built once (lazy pysnmp import happens inside .walk(), so this is safe
    # even without pysnmp installed as long as no cycle actually needs it), tracked
    # against the loop clock like the standalone daemon's own `next_snmp`. Skipped
    # entirely for finite `--cycles` runs, same determinism rule as the topology
    # refresh above.
    snmp_poller = build_snmp_poller(cfg) if cfg.snmp_interval_s > 0 else None
    next_snmp = 0.0   # due on the very first eligible cycle

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        # Topology hot-reload: re-fetch each cycle (like the standalone daemon's own
        # device-set reload) so a central-side add/remove/reparent applies without a
        # restart. A hiccup keeps the last-known set rather than probing nothing.
        if max_cycles is None:
            try:
                topo = client.fetch_devices()
                devices = topo.get("devices") or devices
                canary_ip = topo.get("canary_ip") or canary_ip
            except CentralClientError as exc:
                log.warning("topology refresh failed, probing last-known set: %s", exc)
        started = asyncio.get_running_loop().time()
        due_snmp = (snmp_poller is not None and max_cycles is None and started >= next_snmp)
        try:
            await run_cycle_central_brain(
                prober, client, devices, canary_ip, cfg,
                snmp_poller=snmp_poller if due_snmp else None)
        except Exception:
            log.exception("central-brain cycle %d failed; continuing", cycle + 1)
        if due_snmp:
            next_snmp = started + cfg.snmp_interval_s
        cycle += 1
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
    # Windows consoles / redirected pipes default to a legacy code page (cp1252/cp437)
    # that can't encode the dashes & arrows in our log/print lines. Force UTF-8 so a
    # stray glyph (e.g. a unicode device name) never crashes the polling loop.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every central POST

    # One logical poller per org/node: two pollers racing the same target
    # double-*report*, which is wasteful and confusing even though central's ingest is
    # idempotent per outage. An OS advisory lock refuses the second start (auto-released
    # by the kernel on exit/crash — no stale pidfile to reap). Central-brain mode makes
    # no local DB writes at all, so there's no schema to migrate — the lock file is the
    # only thing that needs a directory, and SingleInstance creates its own parent dir.
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
