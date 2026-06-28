"""Daemon runtime — the background polling worker.

One of the two decoupled runtimes (the other is apps/dashboard). Every
`poll_interval_s` it pings every active device (plus the canary and
power-reference nodes) concurrently, feeds the samples to the MonitorEngine,
persists the resulting states and outage changes, dispatches alerts, and sweeps
overdue escalations.

    python apps/daemon/main.py                      # real 60s cadence, forever
    python apps/daemon/main.py --interval 5 --cycles 3    # short run (smoke test)

Scheduling is a plain asyncio interval loop (no third-party deps); it is isolated
in `run_forever`, so swapping to APScheduler later is a one-spot change. Zero-
install: this entry point puts <repo>/src on sys.path.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- bootstrap: make the `wisp` package importable without installing ---
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG, Config  # noqa: E402
from wisp.database.client import connect, migrate, transaction, write_with_retry
from wisp.runtime.single_instance import AlreadyRunning, SingleInstance
from wisp.core.rollup import roll_up
from wisp.ingress.probers import PingResult, Prober, build_prober
from wisp.ingress.snmp import SnmpPoller, build_snmp_poller, load_snmp_targets
from wisp.egress.notifiers import AlertDispatcher, build_notifier
from wisp.egress.ports import PortMonitor
from wisp.core.state_machine import (
    DEGRADED,
    DOWN,
    DOWN_FAMILY,
    Event,
    UNREACHABLE,
    UP,
    MonitorEngine,
    apply_events,
    build_engine,
    load_device_meta,
)

# A device with no reading at all is treated as fully lost (matches the engine's
# missing-result default), so the confirmation pass never trips over a None.
_LOST = PingResult("", None, 100.0)


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
    so aggregation gear is probed gently — see MonitorEngine.probe_plan). A naive
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


def prune_old_polls(cfg: Config = CONFIG, *, now: datetime | None = None) -> int:
    """Delete raw poll samples older than `cfg.poll_retention_days` so a 24/7
    deployment reaches a steady-state DB size. Returns the number of rows removed.

    Retention <= 0 disables pruning (keep everything). Only `poll_results` is
    touched — the `outages` table is the permanent incident record and is left
    alone, so analytics/history survive. Deleted pages go to SQLite's freelist and
    are reused by future inserts, so the file stops growing without a VACUUM (run
    one manually if you need to reclaim disk after shrinking retention)."""
    days = cfg.poll_retention_days
    if days <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")

    def _do() -> int:
        with connect(cfg) as conn:
            cur = conn.execute(
                "DELETE FROM poll_results WHERE timestamp < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
    return int(write_with_retry(_do) or 0)


def _persist(rows: list[tuple], events: list[Event], ts: str, cfg: Config) -> None:
    def _do() -> None:
        with connect(cfg) as conn:
            with transaction(conn):
                conn.executemany(
                    "INSERT INTO poll_results (device_id, timestamp, latency_ms,"
                    " packet_loss, jitter_ms, state) VALUES (?,?,?,?,?,?)",
                    rows,
                )
                apply_events(conn, events, ts)
    write_with_retry(_do)


async def _confirm_down(
    prober: Prober,
    engine: MonitorEngine,
    plan: dict[str, int],
    results: dict[str, PingResult],
    states: dict[int, str],
    ts: str,
    cfg: Config,
) -> list[Event]:
    """Fast soft-state → hard-state confirmation.

    The main poll advanced every FSM by one sample. Any device that read 100% loss is
    *suspected* down but not yet confirmed (DOWN needs `down_consecutive` consecutive
    all-lost samples). Rather than wait a full poll interval for each remaining sample,
    re-probe **just the suspects** back-to-back every `retry_interval_s` until they
    either confirm DOWN or come back reachable (a blip — which clears the suspicion and
    never pages). Detection collapses from `down_consecutive × poll_interval` to a few
    seconds, and the healthy fleet is never re-probed.

    Mutates `states` (and `results`, so the persisted reading reflects the final probe)
    in place; returns any extra events (e.g. OutageOpened) the confirmation produced."""
    suspects = {
        dev_id for dev_id, st in states.items()
        if st not in DOWN_FAMILY
        and (results.get(engine.meta[dev_id].ip_address) or _LOST).packet_loss >= 100.0
    }
    # The main pass already contributed sample 1, so at most down_consecutive-1 more.
    attempts_left = max(0, cfg.down_consecutive - 1)
    extra_events: list[Event] = []

    while suspects and attempts_left > 0:
        await asyncio.sleep(cfg.retry_interval_s)
        ips = sorted({engine.meta[d].ip_address for d in suspects})
        # Single fast echo: a dead host burns one ICMP timeout, not the plan's 5, so
        # confirmation isn't dominated by timeouts. The hysteresis is the consecutive
        # sample COUNT (down_consecutive), not the pings-per-sample. `plan` is kept for
        # signature stability with the full-poll path.
        counts = {ip: 1 for ip in ips}
        retry = await _gather_pings(
            prober, ips, counts, max_inflight=cfg.probe_max_inflight
        )
        results.update(retry)  # carry the freshest reading into the persisted row
        outcome = engine.process_cycle(retry, ts, subset=suspects)
        extra_events.extend(outcome.events)
        states.update(outcome.states)
        # Keep retrying only those still all-lost and not yet confirmed down/unreachable.
        suspects = {
            d for d in suspects
            if outcome.states.get(d) not in DOWN_FAMILY
            and (retry.get(engine.meta[d].ip_address) or _LOST).packet_loss >= 100.0
        }
        attempts_left -= 1

    return extra_events


async def _confirm_up(
    prober: Prober,
    engine: MonitorEngine,
    plan: dict[str, int],
    results: dict[str, PingResult],
    states: dict[int, str],
    ts: str,
    cfg: Config,
) -> list[Event]:
    """Fast hard-state → recovery confirmation — the mirror image of `_confirm_down`.

    Recovery used to be the slow direction: a DOWN device was never re-probed between
    full polls, so it took `recover_consecutive × poll_interval` to clear. This applies
    the same back-to-back re-probe to the *up* direction: any device still in
    `DOWN_FAMILY` that reads reachable is *suspected* recovered but not yet confirmed
    (leaving DOWN needs `recover_consecutive` consecutive non-lost samples). Re-probe
    just those every `retry_interval_s` until they either confirm recovery (leave
    `DOWN_FAMILY` → OutageResolved, restore notice) or read 100% loss again (still down,
    no flap). So UP is now detected in seconds, symmetric with DOWN.

    Mutates `states`/`results` in place; returns any extra events the confirmation
    produced (e.g. OutageResolved)."""
    suspects = {
        dev_id for dev_id, st in states.items()
        if st in DOWN_FAMILY
        and (results.get(engine.meta[dev_id].ip_address) or _LOST).packet_loss < 100.0
    }
    # The triggering pass already contributed the first non-lost sample, so at most
    # recover_consecutive-1 more are needed to clear the hysteresis.
    attempts_left = max(0, cfg.recover_consecutive - 1)
    extra_events: list[Event] = []

    while suspects and attempts_left > 0:
        await asyncio.sleep(cfg.retry_interval_s)
        ips = sorted({engine.meta[d].ip_address for d in suspects})
        counts = {ip: 1 for ip in ips}   # single fast echo (see _confirm_down)
        retry = await _gather_pings(
            prober, ips, counts, max_inflight=cfg.probe_max_inflight
        )
        results.update(retry)  # carry the freshest reading into the persisted row
        outcome = engine.process_cycle(retry, ts, subset=suspects)
        extra_events.extend(outcome.events)
        states.update(outcome.states)
        # Keep retrying only those still down AND still reading reachable (a fresh 100%
        # loss aborts the recovery — the link is flapping, leave it DOWN).
        suspects = {
            d for d in suspects
            if outcome.states.get(d) in DOWN_FAMILY
            and (retry.get(engine.meta[d].ip_address) or _LOST).packet_loss < 100.0
        }
        attempts_left -= 1

    return extra_events


async def run_cycle(
    prober: Prober, engine: MonitorEngine, dispatcher: AlertDispatcher, cfg: Config = CONFIG
) -> list[Event]:
    """One poll cycle: ping, evaluate, persist, then dispatch alerts + sweep
    overdue escalations. Returns the events emitted."""
    prober.on_cycle_start()
    ts = _utc_now_iso()
    # Per-IP plan: aggregation gear gets fewer echoes (gentle), and the in-flight set
    # is bounded so a large fleet never exhausts file descriptors mid-cycle.
    plan = engine.probe_plan()
    results = await _gather_pings(
        prober, sorted(plan), plan, max_inflight=cfg.probe_max_inflight
    )

    result = engine.process_cycle(results, ts)
    states = dict(result.states)
    events = list(result.events)

    # Make the freeze visible: when the canary reads down and canary_freeze is on, the
    # engine skips ALL local transitions this cycle (no DOWN detection, no fast-confirm).
    # A flaky internet canary therefore silently stalls detection — log it loudly so the
    # operator can see why nothing is being detected (and consider a LAN canary).
    if result.canary_down:
        log.warning("canary %s unreachable — uplink treated as down; local detection "
                    "FROZEN this cycle (WISP_CANARY_FREEZE=1)", cfg.canary_ip)

    # Fast-confirm transitions in seconds instead of waiting whole poll intervals:
    # DOWN (soft→hard) and recovery (hard→up) both, so detection is symmetric.
    # Skipped when the uplink is frozen (no local transitions this cycle) or disabled.
    if cfg.retry_interval_s > 0 and not result.canary_down:
        events += await _confirm_down(prober, engine, plan, results, states, ts, cfg)
        events += await _confirm_up(prober, engine, plan, results, states, ts, cfg)

    rows = []
    for dev_id, state in states.items():
        res = results.get(engine.meta[dev_id].ip_address)
        latency = res.latency_ms if res else None
        loss = res.packet_loss if res else 100.0
        jitter = res.jitter_ms if res else None
        rows.append((dev_id, ts, latency, loss, jitter, state))

    _persist(rows, events, ts, cfg)                 # poll_results + outages first
    dispatcher.dispatch(events, ts)                 # then network sends + alert_log
    dispatcher.sweep(ts)                            # fire any overdue escalations
    # Soft per-link performance check (slow/jittery-but-up). Isolated so a perf-sweep
    # hiccup never sinks the cycle's core detection/alerting above.
    try:
        dispatcher.perf_sweep(ts)
    except Exception:
        log.exception("perf sweep failed; continuing")
    # On-backup signal (graph topology): persist the badge + page the operator on a
    # primary→backup failover edge. Uses the engine's full-pass redundancy map but the
    # FINAL states (post fast-confirm) so a node that confirmed hard DOWN never shows
    # on-backup. Isolated like the perf sweep — a hiccup never sinks the cycle.
    try:
        dispatcher.redundancy_sweep(result.redundancy, states, ts)
    except Exception:
        log.exception("redundancy sweep failed; continuing")
    return events


async def _between_cycle_watch(
    prober: Prober,
    engine: MonitorEngine,
    dispatcher: AlertDispatcher,
    sleep_for: float,
    cfg: Config,
) -> None:
    """Probe the whole fleet at retry_interval_s during the inter-poll gap.

    fast-confirm only fires AFTER a full poll, so a transition that happens mid-gap
    otherwise waits up to poll_interval_s before its first sample lands. This loop
    closes that gap in BOTH directions: every retry_interval_s it pings every device
    with a single echo (cheap), and the moment a healthy host reads 100% loss (or a
    DOWN host reads reachable) it runs the matching fast-confirm path — same
    hysteresis (down_consecutive / recover_consecutive), same canary guard. So a
    failure *or* a recovery anywhere in the poll cycle is reflected in seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + sleep_for
    plan = engine.probe_plan()
    # One ping per device: enough to detect a 100%-loss/reachable flip without the full
    # echo burst; the confirmation probes still use the per-IP plan.
    one_ping: dict[str, int] = {ip: 1 for ip in plan}

    while True:
        remaining = deadline - loop.time()
        if remaining <= cfg.retry_interval_s:
            await asyncio.sleep(max(0.0, remaining))
            return
        await asyncio.sleep(cfg.retry_interval_s)

        # Probe EVERY device — DOWN ones too, so recovery is caught mid-gap (not just
        # at the next full poll). The DOWN set is usually small, so this is cheap.
        all_ids = set(engine.fsm)
        ips = sorted({engine.meta[d].ip_address for d in all_ids} | {cfg.canary_ip})
        counts = {ip: one_ping.get(ip, 1) for ip in ips}
        try:
            probe_res = await _gather_pings(
                prober, ips, counts, max_inflight=cfg.probe_max_inflight
            )
        except RuntimeError:
            log.exception("between-cycle watch: ICMP error, skipping tick")
            continue

        canary_down = (probe_res.get(cfg.canary_ip) or _LOST).packet_loss >= 100.0
        if canary_down and cfg.canary_freeze:
            continue

        def _loss(d: int) -> float:
            return (probe_res.get(engine.meta[d].ip_address) or _LOST).packet_loss

        # Healthy host suddenly all-lost → down suspect; DOWN host now reachable → up.
        down_suspects = {d for d in all_ids
                         if engine.fsm[d].state not in DOWN_FAMILY and _loss(d) >= 100.0}
        up_suspects = {d for d in all_ids
                       if engine.fsm[d].state in DOWN_FAMILY and _loss(d) < 100.0}
        suspects = down_suspects | up_suspects
        if not suspects:
            continue

        ts = _utc_now_iso()
        # Feed sample 1 through the FSM (subset mode), then confirm each direction.
        outcome = engine.process_cycle(probe_res, ts, subset=suspects)
        states = dict(outcome.states)
        events = list(outcome.events)
        events += await _confirm_down(prober, engine, plan, probe_res, states, ts, cfg)
        events += await _confirm_up(prober, engine, plan, probe_res, states, ts, cfg)

        rows = [
            (dev_id, ts,
             (probe_res.get(engine.meta[dev_id].ip_address) or _LOST).latency_ms,
             (probe_res.get(engine.meta[dev_id].ip_address) or _LOST).packet_loss,
             (probe_res.get(engine.meta[dev_id].ip_address) or _LOST).jitter_ms,
             st)
            for dev_id, st in states.items()
        ]
        _persist(rows, events, ts, cfg)
        if events:
            dispatcher.dispatch(events, ts)
            dispatcher.sweep(ts)


async def snmp_cycle(poller: SnmpPoller, port_monitor: PortMonitor,
                     cfg: Config = CONFIG) -> None:
    """One SNMP pass: walk every snmp-enabled switch's ifTable and fold/alert monitored
    port-downs. Each switch is isolated in its own try/except — a dead or blocked switch
    (or a broken pysnmp) must NEVER sink the ICMP cycle. Runs on its own slow cadence
    (WISP_SNMP_INTERVAL_S) since ports don't flap like radio links."""
    for device_id, target in load_snmp_targets(cfg):
        try:
            ports = await poller.walk(target)
        except Exception:
            log.exception("SNMP walk failed for device %d (%s); continuing",
                          device_id, target.ip)
            continue
        try:
            for ev in port_monitor.sync_device(device_id, ports, _utc_now_iso()):
                fold = (f" (folded into device {ev.folded_into}'s outage)"
                        if ev.folded_into else "")
                log.info("SNMP port %s %s on device %d%s",
                         ev.port_label, ev.kind, device_id, fold)
        except Exception:
            log.exception("SNMP port sync failed for device %d; continuing", device_id)


def _print_cycle(cycle: int, states: dict[int, str]) -> None:
    vals = list(states.values())
    print(
        f"cycle {cycle:>3} | UP {vals.count(UP)} DEGRADED {vals.count(DEGRADED)} "
        f"DOWN {vals.count(DOWN)} UNREACHABLE {vals.count(UNREACHABLE)}  (of {len(vals)})"
    )


async def run_forever(
    cfg: Config = CONFIG,
    *,
    interval: float | None = None,
    max_cycles: int | None = None,
) -> None:
    # Engine, prober, cadence, and notifier are built once from the env/default config
    # plus the current device set. The device set is re-read each cycle so a UI add/
    # remove is picked up in-process (no restart); config tunables are env-var + restart
    # (see config.py — there is no DB settings layer).
    # A CLI --interval wins; otherwise the cadence is derived from the fleet size
    # (adaptive mode lets a small fleet poll faster — see Config.effective_interval).
    cli_interval = interval
    devices = load_device_meta(cfg)
    interval = cli_interval if cli_interval is not None else cfg.effective_interval(len(devices))
    prober = build_prober(cfg)
    # Verify the box can actually send ICMP before we trust a single reading. Without
    # this, a missing icmplib / disabled ping group makes every host (and the canary)
    # read 100% loss — a broken monitor that looks like a total outage. Fail loud.
    preflight = getattr(prober, "preflight", None)
    if preflight is not None:
        try:
            await preflight()
        except RuntimeError as exc:
            log.error("prober preflight failed — refusing to start: %s", exc)
            raise SystemExit(2)
    engine = build_engine(cfg)
    dispatcher = AlertDispatcher(engine, build_notifier(cfg), cfg)
    # SNMP port-status sibling ingress (graph topology Part B). Built once; targets are
    # re-read each SNMP pass so a UI enable/disable applies without a restart. The
    # PortMonitor only needs a notifier + cfg, so it survives a device-set rebuild.
    snmp_poller = build_snmp_poller(cfg)
    port_monitor = PortMonitor(build_notifier(cfg), cfg)
    print(
        f"monitoring {len(engine.meta)} devices every {interval}s "
        f"[prober={cfg.prober}, notifier={cfg.notifier}] (Ctrl-C to stop)"
    )

    # Retention sweep: prune old poll samples once a day so an always-on deployment
    # holds a steady-state DB size. Run once at startup, then every PRUNE_EVERY_S.
    # Guarded like everything else in this loop — a failed prune is logged, never
    # fatal. Skipped for finite --cycles runs (smoke tests stay deterministic).
    PRUNE_EVERY_S = 24 * 3600
    # Fold raw polls into compact hourly rollups once an hour, so trend charts read
    # ~1/(polls-per-hour) the rows and raw retention can be short without losing
    # history. Guarded like the prune — a failed fold is logged, never fatal — and
    # skipped for finite --cycles runs to keep smoke tests deterministic.
    ROLLUP_EVERY_S = 3600
    loop_clock = asyncio.get_running_loop().time
    next_prune = loop_clock()
    next_rollup = loop_clock()
    # SNMP port walk: its own slow cadence, isolated like the prune/rollup guards (a
    # broken walk never kills the monitor) and skipped for finite --cycles smoke runs.
    next_snmp = loop_clock()

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        if max_cycles is None and loop_clock() >= next_prune:
            try:
                removed = prune_old_polls(cfg)
                if removed:
                    print(f"retention: pruned {removed} poll sample(s) older than "
                          f"{cfg.poll_retention_days}d")
            except Exception:
                log.exception("retention sweep failed; continuing")
            next_prune = loop_clock() + PRUNE_EVERY_S
        if max_cycles is None and loop_clock() >= next_rollup:
            try:
                rolled = roll_up(cfg)
                if rolled:
                    print(f"rollup: folded {rolled} device-hour(s) into poll_rollups")
            except Exception:
                log.exception("hourly rollup failed; continuing")
            next_rollup = loop_clock() + ROLLUP_EVERY_S
        if (max_cycles is None and cfg.snmp_interval_s > 0
                and loop_clock() >= next_snmp):
            try:
                await snmp_cycle(snmp_poller, port_monitor, cfg)
            except Exception:
                log.exception("SNMP cycle failed; continuing")
            next_snmp = loop_clock() + cfg.snmp_interval_s
        # Device-set hot reload: rebuild the engine in-process when the active device
        # set changes (UI add/remove). build_engine rehydrates each FSM from the last
        # poll, so a rebuild never re-pages an open outage. (Skipped for finite runs.)
        # A transient DB hiccup here must not kill the monitor — keep the old engine.
        if max_cycles is None:
            try:
                current = load_device_meta(cfg)
                if current != devices:
                    print(f"device set changed ({len(current)} devices) - rebuilding monitor")
                    devices = current
                    engine = build_engine(cfg)
                    dispatcher = AlertDispatcher(engine, build_notifier(cfg), cfg)
                    # Fleet size may have crossed the adaptive threshold — retune cadence.
                    if cli_interval is None:
                        new_interval = cfg.effective_interval(len(current))
                        if new_interval != interval:
                            print(f"poll cadence -> {new_interval}s (fleet {len(current)} devices)")
                            interval = new_interval
            except Exception:
                log.exception("device-set reload failed; keeping current monitor")
        started = asyncio.get_running_loop().time()
        # The watcher must be the hardest thing in the system to kill: one bad cycle
        # (DB lock, a probe library blowing up, a bug) is logged and skipped, never fatal.
        try:
            await run_cycle(prober, engine, dispatcher, cfg)
        except Exception:
            log.exception("poll cycle %d failed; continuing to next cycle", cycle + 1)
        cycle += 1
        _print_cycle(cycle, {dev_id: fsm.state for dev_id, fsm in engine.fsm.items()})
        if max_cycles is not None and cycle >= max_cycles:
            break
        elapsed = asyncio.get_running_loop().time() - started
        sleep_for = max(0.0, interval - elapsed)
        # Between-cycle watch: probe non-DOWN devices at retry_interval_s so a
        # device that fails mid-gap is caught in seconds, not at the next full
        # poll. Disabled for finite --cycles runs (smoke tests) and when
        # retry_interval_s == 0 (fast-confirm also disabled in that case).
        if max_cycles is None and cfg.retry_interval_s > 0 and sleep_for > cfg.retry_interval_s:
            try:
                await _between_cycle_watch(prober, engine, dispatcher, sleep_for, cfg)
            except Exception:
                log.exception("between-cycle watch failed; sleeping instead")
                await asyncio.sleep(sleep_for)
        else:
            await asyncio.sleep(sleep_for)


def main() -> None:
    parser = argparse.ArgumentParser(description="Village WISP polling daemon")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between polls (overrides config)")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after N cycles (default: run forever)")
    args = parser.parse_args()

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
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every ntfy POST
    migrate()
    # One logical poller per DB: each daemon has its own in-memory FSM, so a second
    # one against the same DB independently confirms every outage and double-pages.
    # An OS advisory lock next to the DB refuses the second start (auto-released by the
    # kernel on exit/crash — no stale pidfile to reap).
    guard = SingleInstance(f"{CONFIG.db_path}.lock")
    try:
        guard.acquire()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        raise SystemExit(3)
    try:
        asyncio.run(run_forever(interval=args.interval, max_cycles=args.cycles))
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        guard.release()


if __name__ == "__main__":
    main()
