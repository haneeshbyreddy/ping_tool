"""Layer 3 — Business Intelligence (read-only).

Turns poll_results + outages into the operator-facing views from plan.md §4:
the live status board, the daily digest (uptime %, power-vs-equipment split,
revenue lost, worst site, repeat offenders), and per-device uptime.

    PYTHONPATH=src python -m wisp.core.analytics status        # live board (default)
    PYTHONPATH=src python -m wisp.core.analytics digest [hrs]  # summary over last N hours
    PYTHONPATH=src python -m wisp.core.analytics devices [hrs] # per-device uptime
    PYTHONPATH=src python -m wisp.core.analytics offenders [hrs]

All timestamps are normalised to naive-UTC on read so the ISO8601 poll/outage
stamps and SQLite's `datetime('now')` ack stamps compare cleanly.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone

from wisp.config import CONFIG, Config
from wisp.database.client import connect
from wisp.core.state_machine import DEGRADED, DOWN, UNREACHABLE, UP


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(ts: str) -> datetime:
    """Tolerant parse → naive UTC. Handles 'T'/space separators and ±offset."""
    dt = datetime.fromisoformat(ts.replace(" ", "T"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --- shared queries ---------------------------------------------------------
def latest_states(conn) -> dict[int, str]:
    rows = conn.execute(
        "SELECT pr.device_id AS did, pr.state AS state FROM poll_results pr"
        " JOIN (SELECT device_id, MAX(id) mid FROM poll_results GROUP BY device_id) m"
        " ON pr.id = m.mid"
    ).fetchall()
    return {r["did"]: r["state"] for r in rows}


def _outages_in_window(conn, win_start: datetime, win_end: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT o.*, d.name, d.region, d.customer_count, d.base_revenue_impact,"
        " d.criticality FROM outages o JOIN devices d ON d.id = o.device_id"
    ).fetchall()
    out = []
    for r in rows:
        start = _parse(r["started_at"])
        end = _parse(r["resolved_at"]) if r["resolved_at"] else win_end
        if end >= win_start and start <= win_end:  # overlaps the window
            d = dict(r)
            d["_start"], d["_end"] = start, end
            out.append(d)
    return out


def _downtime_by_device(outages: list[dict], win_start, win_end,
                        only_down: bool = True) -> dict[int, float]:
    per: dict[int, float] = defaultdict(float)
    for o in outages:
        if only_down and o["final_state"] != DOWN:
            continue
        s = max(o["_start"], win_start)
        e = min(o["_end"], win_end)
        if e > s:
            per[o["device_id"]] += (e - s).total_seconds()
    return per


# --- views ------------------------------------------------------------------
def status_board(cfg: Config = CONFIG) -> None:
    with connect(cfg) as conn:
        states = latest_states(conn)
        total = conn.execute("SELECT COUNT(*) FROM devices WHERE is_active=1").fetchone()[0]
        active = conn.execute(
            "SELECT o.id, o.started_at, o.final_state, o.inferred_cause, o.acknowledged_by,"
            " d.name, d.region, d.customer_count, d.criticality FROM outages o"
            " JOIN devices d ON d.id = o.device_id WHERE o.resolved_at IS NULL"
        ).fetchall()
        uplink = conn.execute(
            "SELECT payload FROM alert_log WHERE payload LIKE '%UPLINK%'"
            " OR payload LIKE '%Uplink%' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    up = sum(1 for s in states.values() if s == UP)
    headline = "🟢" if up == total else ("🟠" if up > total * 0.7 else "🔴")
    print(f"{headline} {up}/{total} sites up")

    uplink_down = uplink is not None and "UPLINK_DOWN" in (uplink["payload"] or "")
    print(f"   uplink: {'🚨 DOWN' if uplink_down else '✅ ok'}")

    # active outages sorted by impact (customers × criticality), DOWN before UNREACHABLE
    now = _now()
    ranked = sorted(
        active,
        key=lambda r: (r["final_state"] != DOWN, -(r["customer_count"] * r["criticality"])),
    )
    unacked = sum(1 for r in active if r["final_state"] == DOWN and not r["acknowledged_by"])
    print(f"\nactive outages: {len(active)}  (unacknowledged: {unacked})")
    for r in ranked:
        dur = _fmt_dur((now - _parse(r["started_at"])).total_seconds())
        if r["final_state"] == UNREACHABLE:
            print(f"   ⛔ {r['name']} ({r['region']}) UNREACHABLE · {dur} · suppressed")
        else:
            cause = "⚡power" if (r["inferred_cause"] or "").startswith("Likely") else "🔧link"
            ack = f" · ✓{r['acknowledged_by']}" if r["acknowledged_by"] else " · UNACKED"
            print(f"   🔴 {r['name']} ({r['region']}) · {cause} · ~{r['customer_count']} cust"
                  f" · {dur}{ack}")

    watch = [d for d, s in states.items() if s == DEGRADED]
    if watch:
        with connect(cfg) as conn:
            names = conn.execute(
                f"SELECT name, region FROM devices WHERE id IN ({','.join('?'*len(watch))})",
                watch,
            ).fetchall()
        print("\n⚠️  degraded watch-list (early warning):")
        for n in names:
            print(f"   🟠 {n['name']} ({n['region']})")


def compute_digest(cfg: Config = CONFIG, hours: int = 24) -> dict:
    """Pure-ish metrics computation (returns a dict) so the numbers are testable
    independently of the printed layout."""
    win_end = _now()
    win_start = win_end.replace(microsecond=0) - _td(hours)
    with connect(cfg) as conn:
        active_devices = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE is_active=1").fetchone()[0]
        outages = _outages_in_window(conn, win_start, win_end)

    window_s = (win_end - win_start).total_seconds()
    down_by_dev = _downtime_by_device(outages, win_start, win_end, only_down=True)
    total_down = sum(down_by_dev.values())
    uptime_pct = 100.0 * (1 - total_down / (active_devices * window_s)) if active_devices else 100.0

    down_outages = [o for o in outages if o["final_state"] == DOWN]
    power = [o for o in down_outages if (o["inferred_cause"] or "").startswith("Likely")]
    revenue = sum(
        (min(o["_end"], win_end) - max(o["_start"], win_start)).total_seconds() / 3600
        * o["base_revenue_impact"] for o in down_outages
    )
    customers = sum({o["device_id"]: o["customer_count"] for o in down_outages}.values())
    worst = max(down_by_dev.items(), key=lambda kv: kv[1], default=None)
    worst_name = (next(o["name"] for o in down_outages if o["device_id"] == worst[0])
                  if worst else None)
    return {
        "hours": hours,
        "uptime_pct": uptime_pct,
        "outages": len(down_outages),
        "power": len(power),
        "equipment": len(down_outages) - len(power),
        "total_down_s": total_down,
        "worst": (worst_name, worst[1]) if worst else None,
        "customers": customers,
        "revenue": revenue,
        "offenders": _offender_counts(down_outages),
        "slowest_ack": _slowest_ack(down_outages),
    }


def daily_digest(cfg: Config = CONFIG, hours: int = 24) -> None:
    m = compute_digest(cfg, hours)
    print(f"📊 Network — last {hours}h")
    print(f"Uptime: {m['uptime_pct']:.2f}% overall")
    print(f"Outages: {m['outages']}  (⚡ {m['power']} power · 🔧 {m['equipment']} equipment)")
    print(f"Total downtime: {_fmt_dur(m['total_down_s'])}")
    if m["worst"]:
        print(f"Worst site: {m['worst'][0]} — {_fmt_dur(m['worst'][1])}")
    print(f"Customers impacted: ~{m['customers']}")
    print(f"Est. revenue lost: ≈ ₹{m['revenue']:.0f}")
    if m["offenders"]:
        top = ", ".join(f"{n} ({c}×)" for n, c in m["offenders"][:3])
        print(f"Repeat offenders: {top}")
    if m["slowest_ack"]:
        print(f"Slowest ack: {_fmt_dur(m['slowest_ack'][1])} ({m['slowest_ack'][0]})")


def device_uptime(cfg: Config = CONFIG, hours: int = 24) -> None:
    win_end = _now()
    win_start = win_end - _td(hours)
    with connect(cfg) as conn:
        devices = conn.execute(
            "SELECT id, name, region FROM devices WHERE is_active=1 ORDER BY id").fetchall()
        outages = _outages_in_window(conn, win_start, win_end)
    down = _downtime_by_device(outages, win_start, win_end, only_down=False)
    window_s = (win_end - win_start).total_seconds()
    print(f"per-device uptime (last {hours}h):")
    for d in devices:
        pct = 100.0 * (1 - down.get(d["id"], 0.0) / window_s)
        bar = "✅" if pct >= 99.9 else ("🟠" if pct >= 95 else "🔴")
        print(f"   {bar} {d['name']:<20} {pct:6.2f}%  (down {_fmt_dur(down.get(d['id'],0))})")


def offenders_report(cfg: Config = CONFIG, hours: int = 24) -> None:
    win_end = _now()
    win_start = win_end - _td(hours)
    with connect(cfg) as conn:
        outages = _outages_in_window(conn, win_start, win_end)
    ranked = _offender_counts([o for o in outages if o["final_state"] == DOWN])
    print(f"repeat offenders (last {hours}h):")
    if not ranked:
        print("   none 🎉")
    for name, count in ranked:
        print(f"   {count}×  {name}")


# --- helpers ----------------------------------------------------------------
def _td(hours: int):
    from datetime import timedelta
    return timedelta(hours=hours)


def _offender_counts(down_outages: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for o in down_outages:
        counts[o["name"]] += 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def _slowest_ack(down_outages: list[dict]) -> tuple[str, float] | None:
    best = None
    for o in down_outages:
        if o["acknowledged_at"]:
            secs = (_parse(o["acknowledged_at"]) - o["_start"]).total_seconds()
            if best is None or secs > best[1]:
                best = (o["region"], secs)
    return best


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    if cmd == "status":
        status_board()
    elif cmd == "digest":
        daily_digest(hours=hours)
    elif cmd == "devices":
        device_uptime(hours=hours)
    elif cmd == "offenders":
        offenders_report(hours=hours)
    else:
        print(f"unknown command '{cmd}'. Use: status | digest | devices | offenders")


if __name__ == "__main__":
    main()
