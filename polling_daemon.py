"""The polling daemon: every `poll_interval_s` it pings every active device (plus
the canary and power-reference nodes) concurrently, feeds the samples to the
MonitorEngine, persists the resulting states and outage changes, and surfaces
events.

Phase 3 wires in the real state machine. Notification dispatch (turning these
events into ntfy/Telegram messages) is Phase 5 — for now events are printed in
the alert format we designed, so the behavior is visible.

Scheduling is a plain asyncio interval loop (no third-party deps); it is isolated
in `run_forever`, so swapping to APScheduler later is a one-spot change.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from config import CONFIG, Config
from db import connect, migrate, transaction, write_with_retry
from probers import PingResult, Prober, build_prober
from notifiers import AlertDispatcher, build_notifier
from state_machine import (
    DEGRADED,
    DOWN,
    Event,
    UNREACHABLE,
    UP,
    MonitorEngine,
    apply_events,
    build_engine,
)


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
    interval = cfg.poll_interval_s if interval is None else interval
    prober = build_prober(cfg)
    engine = build_engine(cfg)
    dispatcher = AlertDispatcher(engine, build_notifier(cfg), cfg)
    print(
        f"monitoring {len(engine.meta)} devices every {interval}s "
        f"[prober={cfg.prober}, notifier={cfg.notifier}] (Ctrl-C to stop)"
    )
    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        started = asyncio.get_event_loop().time()
        await run_cycle(prober, engine, dispatcher, cfg)
        cycle += 1
        _print_cycle(cycle, {dev_id: fsm.state for dev_id, fsm in engine.fsm.items()})
        if max_cycles is not None and cycle >= max_cycles:
            break
        elapsed = asyncio.get_event_loop().time() - started
        await asyncio.sleep(max(0.0, interval - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser(description="Village WISP polling daemon")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between polls (overrides config; e.g. 1 for demo)")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after N cycles (default: run forever)")
    args = parser.parse_args()

    migrate()
    try:
        asyncio.run(run_forever(interval=args.interval, max_cycles=args.cycles))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
