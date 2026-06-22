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
from wisp.ingress.probers import PingResult, Prober, build_prober
from wisp.egress.notifiers import AlertDispatcher, build_notifier
from wisp.core.state_machine import (
    DEGRADED,
    DOWN,
    Event,
    UNREACHABLE,
    UP,
    MonitorEngine,
    apply_events,
    build_engine,
    load_device_meta,
)


log = logging.getLogger("wisp.daemon")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _gather_pings(prober: Prober, ips: list[str], count: int) -> dict[str, PingResult]:
    async def one(ip: str) -> tuple[str, PingResult]:
        try:
            return ip, await prober.ping(ip, count)
        except Exception:
            # One probe blowing up must never sink the cycle.
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
                    " packet_loss, state) VALUES (?,?,?,?,?)",
                    rows,
                )
                apply_events(conn, events, ts)
    write_with_retry(_do)


async def run_cycle(
    prober: Prober, engine: MonitorEngine, dispatcher: AlertDispatcher, cfg: Config = CONFIG
) -> list[Event]:
    """One poll cycle: ping, evaluate, persist, then dispatch alerts + sweep
    overdue escalations. Returns the events emitted."""
    prober.on_cycle_start()
    ts = _utc_now_iso()
    results = await _gather_pings(prober, sorted(engine.required_ips()), cfg.pings_per_poll)

    result = engine.process_cycle(results, ts)

    rows = []
    for dev_id, state in result.states.items():
        res = results.get(engine.meta[dev_id].ip_address)
        latency = res.latency_ms if res else None
        loss = res.packet_loss if res else 100.0
        rows.append((dev_id, ts, latency, loss, state))

    _persist(rows, result.events, ts, cfg)          # poll_results + outages first
    dispatcher.dispatch(result.events, ts)          # then network sends + alert_log
    dispatcher.sweep(ts)                             # fire any overdue escalations
    return result.events


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
    interval = cfg.poll_interval_s if interval is None else interval
    devices = load_device_meta(cfg)
    prober = build_prober(cfg)
    engine = build_engine(cfg)
    dispatcher = AlertDispatcher(engine, build_notifier(cfg), cfg)
    print(
        f"monitoring {len(engine.meta)} devices every {interval}s "
        f"[prober={cfg.prober}, notifier={cfg.notifier}] (Ctrl-C to stop)"
    )

    # Retention sweep: prune old poll samples once a day so an always-on deployment
    # holds a steady-state DB size. Run once at startup, then every PRUNE_EVERY_S.
    # Guarded like everything else in this loop — a failed prune is logged, never
    # fatal. Skipped for finite --cycles runs (smoke tests stay deterministic).
    PRUNE_EVERY_S = 24 * 3600
    next_prune = asyncio.get_running_loop().time()

    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        if max_cycles is None and asyncio.get_running_loop().time() >= next_prune:
            try:
                removed = prune_old_polls(cfg)
                if removed:
                    print(f"retention: pruned {removed} poll sample(s) older than "
                          f"{cfg.poll_retention_days}d")
            except Exception:
                log.exception("retention sweep failed; continuing")
            next_prune = asyncio.get_running_loop().time() + PRUNE_EVERY_S
        # Device-set hot reload: rebuild the engine in-process when the active device
        # set changes (UI add/remove). build_engine rehydrates each FSM from the last
        # poll, so a rebuild never re-pages an open outage. (Skipped for finite runs.)
        # A transient DB hiccup here must not kill the monitor — keep the old engine.
        if max_cycles is None:
            try:
                current = load_device_meta(cfg)
                if current != devices:
                    print(f"device set changed ({len(current)} devices) — rebuilding monitor")
                    devices = current
                    engine = build_engine(cfg)
                    dispatcher = AlertDispatcher(engine, build_notifier(cfg), cfg)
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
        await asyncio.sleep(max(0.0, interval - elapsed))


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
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every ntfy POST
    migrate()
    try:
        asyncio.run(run_forever(interval=args.interval, max_cycles=args.cycles))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
