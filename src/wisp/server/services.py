"""Layer 3b — Dashboard data layer (JSON-shaped views over the live DB).

`core/analytics.py` prints operator-facing text; this module returns the same
kind of information as plain dicts/lists so the dashboard HTTP layer (`routes.py`)
can render it. Read functions are pure-ish (DB in, dict out) and unit-tested in
`tests/integration/test_api.py`; the write actions (acknowledge / assign /
post-mortem / device CRUD) go through `write_with_retry` like the rest of the system.

Everything here is read-mostly and stdlib-only (the JSON views need no extra
deps); only the daemon's real ICMP prober + ntfy notifier require the venv.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from wisp.core.analytics import (
    _downtime_by_device,
    _fmt_dur,
    _now,
    _outages_in_window,
    _parse,
    latest_states,
)
from wisp.config import CONFIG, Config
from wisp.database.client import connect, transaction, write_with_retry
from wisp.core.state_machine import (
    DEGRADED,
    DOWN,
    UNREACHABLE,
    UP,
)

# How recently an outage must have recovered to still be worth a post-mortem card.
POSTMORTEM_WINDOW_H = 24


# --- helpers ----------------------------------------------------------------

def _today() -> str:
    """Today's UTC calendar day, 'YYYY-MM-DD' — the same UTC-day convention the
    heatmap / nodes_down_on_day use, so attendance days line up with outage days."""
    return _now().date().isoformat()


def _state_label(state: str) -> str:
    return {
        UP: "Operational",
        DEGRADED: "Warning",
        DOWN: "Outage",
        UNREACHABLE: "Unreachable",
    }.get(state, state)


# --- shared payload coercion (device + worker CRUD validate the same way) ----
def _payload_str(data: dict, key: str, err: type[ValueError], *,
                 required: bool = False, default=None):
    """Trim a free-text field to a non-empty string or `default`. Raises `err`
    (DeviceError / WorkerError) with a human message when a required field is blank."""
    v = data.get(key)
    v = v.strip() if isinstance(v, str) else (None if v is None else str(v).strip())
    if required and not v:
        raise err(f"{key.replace('_', ' ')} is required")
    return v or default


# --- read views -------------------------------------------------------------
def system_summary(cfg: Config = CONFIG, hours: int = 24) -> dict:
    """Top-of-dashboard KPIs: overall health %, active/total nodes, live outages."""
    win_end = _now()
    win_start = win_end - timedelta(hours=hours)
    with connect(cfg) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE is_active=1").fetchone()[0]
        outages = _outages_in_window(conn, win_start, win_end)
        open_outages = conn.execute(
            "SELECT final_state FROM outages WHERE resolved_at IS NULL").fetchall()
        states = latest_states(conn)
        uplink = conn.execute(
            "SELECT payload FROM alert_log WHERE payload LIKE '%UPLINK%'"
            " OR payload LIKE '%Uplink%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_poll = conn.execute(
            "SELECT MAX(timestamp) AS t FROM poll_results").fetchone()["t"]

    window_s = (win_end - win_start).total_seconds()
    down_by_dev = _downtime_by_device(outages, win_start, win_end, only_down=True)
    total_down = sum(down_by_dev.values())
    health = 100.0 * (1 - total_down / (total * window_s)) if total else 100.0

    up = sum(1 for s in states.values() if s == UP)
    # devices that have never reported yet count as "up" for the headline ratio.
    up += max(0, total - len(states))
    live_outages = sum(1 for r in open_outages if r["final_state"] == DOWN)
    uplink_down = uplink is not None and "UPLINK_DOWN" in (uplink["payload"] or "")

    stale_after_s = cfg.stale_threshold_s()
    monitor_age_s = None
    monitor_stale = False
    if last_poll:
        monitor_age_s = max(0, int((win_end - _parse(last_poll)).total_seconds()))
        # Only flag stale once polling has actually started AND there's something to
        # poll — a fresh, empty install isn't a dead monitor. Mirrors the watchdog.
        monitor_stale = total > 0 and monitor_age_s > stale_after_s

    return {
        "system_health_pct": round(health, 2),
        "active_nodes": up,
        "total_nodes": total,
        "outages": live_outages,
        "uplink_down": uplink_down,
        "window_hours": hours,
        "last_poll": last_poll,
        "monitor_age_s": monitor_age_s,
        "monitor_stale": monitor_stale,
        "stale_after_s": stale_after_s,
    }


def triage_outages(cfg: Config = CONFIG) -> list[dict]:
    """The "Active Outage Triage" feed, newest-impact first.

    Three lifecycle buckets the dashboard colour-codes:
      * unassigned       — open, DOWN, nobody has acked
      * in_progress      — open, DOWN, acked (== a technician owns it)
      * pending_postmortem — recovered recently but no resolution logged yet
    UNREACHABLE outages are topology-suppressed and never paged, so they are
    excluded from triage.
    """
    now = _now()
    pm_cutoff = now - timedelta(hours=POSTMORTEM_WINDOW_H)
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT o.id, o.device_id, o.started_at, o.resolved_at, o.final_state,"
            " o.acknowledged_by, o.acknowledged_at, o.root_cause,"
            " o.resolution_notes, d.name, d.region"
            " FROM outages o JOIN devices d ON d.id = o.device_id"
            " WHERE o.final_state = ?"
            "   AND (o.resolved_at IS NULL"
            "        OR (o.root_cause IS NULL AND o.resolution_notes IS NULL))"
            " ORDER BY o.id DESC",
            (DOWN,),
        ).fetchall()
        topo = conn.execute(
            "SELECT id, name, parent_device_id FROM devices WHERE is_active=1").fetchall()
        backup_edges = conn.execute(
            "SELECT child_id, parent_id FROM device_links"
            " WHERE is_active=1 AND kind='backup'").fetchall()
        states = latest_states(conn)

        # Who was on duty (operators marked present) on each outage's *start* day —
        # surfaced on the triage card so the operator can see who was around when it
        # broke. One query over just the days actually in the feed.
        outage_days = {_parse(r["started_at"]).date().isoformat() for r in rows}
        on_duty_by_day: dict[str, list[str]] = {}
        if outage_days:
            placeholders = ",".join("?" * len(outage_days))
            for ar in conn.execute(
                "SELECT a.day, w.name FROM attendance a"
                " JOIN workers w ON w.id = a.worker_id"
                f" WHERE w.role='operator' AND a.day IN ({placeholders})"
                " ORDER BY w.name",
                tuple(outage_days),
            ):
                on_duty_by_day.setdefault(ar["day"], []).append(ar["name"])

    # Blast radius: a child knocked UNREACHABLE behind a DOWN parent is topology-
    # suppressed (it gets no card of its own), so the operator can't see who else is
    # affected. Attribute each UNREACHABLE node to its nearest DOWN ancestor (root
    # cause) and surface those names on that parent's card.
    # Every parent edge (primary + active backups), so attribution follows ALL paths:
    # a child is dragged UNREACHABLE only when its whole upstream is dead, and the blast
    # radius is credited to each nearest DOWN ancestor it sits behind.
    parents_of: dict[int, list[int]] = {}
    for r in topo:
        if r["parent_device_id"] is not None:
            parents_of.setdefault(r["id"], []).append(r["parent_device_id"])
    for e in backup_edges:
        parents_of.setdefault(e["child_id"], []).append(e["parent_id"])
    name_of = {r["id"]: r["name"] for r in topo}

    def _culprits(node_id: int) -> set[int]:
        """Nearest DOWN ancestor(s) of an UNREACHABLE node: walk UP through every parent
        that is itself UNREACHABLE; the first DOWN node on each path is a culprit. A
        healthy/degraded ancestor breaks that path's suppression chain. With a single
        parent this returns {the down parent}, exactly as before."""
        found: set[int] = set()
        seen: set[int] = set()
        stack = list(parents_of.get(node_id, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            st = states.get(cur)
            if st == DOWN:
                found.add(cur)
            elif st == UNREACHABLE:
                stack.extend(parents_of.get(cur, []))
        return found

    affected_by: dict[int, list[str]] = {}
    for nid, st in states.items():
        if st == UNREACHABLE:
            for culprit in _culprits(nid):
                affected_by.setdefault(culprit, []).append(name_of.get(nid, f"#{nid}"))

    items: list[dict] = []
    for r in rows:
        open_outage = r["resolved_at"] is None
        if open_outage:
            status = "in_progress" if r["acknowledged_by"] else "unassigned"
            ref = _parse(r["started_at"])
            duration_s = (now - ref).total_seconds()
        else:
            # recovered + undocumented -> post-mortem, but only while recent
            if _parse(r["resolved_at"]) < pm_cutoff:
                continue
            status = "pending_postmortem"
            duration_s = (_parse(r["resolved_at"]) - _parse(r["started_at"])).total_seconds()
        items.append({
            "id": r["id"],
            "device_id": r["device_id"],
            "name": r["name"],
            "region": r["region"],
            "status": status,
            "assigned_to": r["acknowledged_by"],
            "started_at": r["started_at"],
            "duration_s": int(duration_s),
            "duration_label": _fmt_dur(duration_s),
            # children dragged UNREACHABLE behind this DOWN node (only while open)
            "affected_children": sorted(affected_by.get(r["device_id"], []))
            if open_outage else [],
            # operators marked present on the day the outage began
            "on_duty": on_duty_by_day.get(
                _parse(r["started_at"]).date().isoformat(), []),
        })

    # impact-ranked: unassigned first, then in-progress, then post-mortem;
    # within a bucket, longest-running first.
    order = {"unassigned": 0, "in_progress": 1, "pending_postmortem": 2}
    items.sort(key=lambda i: (order.get(i["status"], 9), -i["duration_s"]))
    return items


def nodes_list(cfg: Config = CONFIG, hours: int = 24) -> list[dict]:
    """Per-device card data for the Nodes page: identity, live state, uptime%."""
    win_end = _now()
    win_start = win_end - timedelta(hours=hours)
    with connect(cfg) as conn:
        devices = conn.execute(
            "SELECT d.id, d.name, d.ip_address, d.device_type, d.region,"
            " d.parent_device_id, d.maintenance, d.snmp_enabled,"
            " pf.degraded AS perf_degraded,"
            " pf.metric AS perf_metric,"
            " pf.baseline_ms AS perf_baseline, pf.current_ms AS perf_current,"
            " pf.since AS perf_since,"
            " rd.on_backup AS on_backup, rd.primary_down_since AS backup_since"
            " FROM devices d LEFT JOIN device_perf pf ON pf.device_id = d.id"
            " LEFT JOIN device_redundancy rd ON rd.device_id = d.id"
            " WHERE d.is_active=1 ORDER BY d.id"
        ).fetchall()
        outages = _outages_in_window(conn, win_start, win_end)
        states = latest_states(conn)
        # Per-switch port health in one GROUP BY (don't pull raw rows): total discovered,
        # how many are monitored, and how many monitored ports are currently alarming. This
        # is what makes a switch's port trouble visible LIVE on the Nodes tree/map instead
        # of only inside the edit modal.
        port_rows = conn.execute(
            "SELECT device_id, COUNT(*) AS total,"
            " SUM(CASE WHEN monitored=1 THEN 1 ELSE 0 END) AS monitored,"
            " SUM(CASE WHEN monitored=1 AND alarm=1 THEN 1 ELSE 0 END) AS down,"
            " SUM(CASE WHEN monitored=1 AND bw_alarm=1 THEN 1 ELSE 0 END) AS bw_low"
            " FROM switch_ports GROUP BY device_id"
        ).fetchall()
    ports_by_device = {
        r["device_id"]: {"total": r["total"], "monitored": r["monitored"] or 0,
                         "down": r["down"] or 0, "bw_low": r["bw_low"] or 0}
        for r in port_rows
    }
    down = _downtime_by_device(outages, win_start, win_end, only_down=False)
    window_s = (win_end - win_start).total_seconds()

    # child counts over the active set, so the tree can show roll-up carets.
    child_count: dict[int, int] = {}
    for d in devices:
        p = d["parent_device_id"]
        if p is not None:
            child_count[p] = child_count.get(p, 0) + 1

    out: list[dict] = []
    for d in devices:
        state = states.get(d["id"], UP)
        pct = 100.0 * (1 - down.get(d["id"], 0.0) / window_s) if window_s else 100.0
        out.append({
            "id": d["id"],
            "name": d["name"],
            "ip": d["ip_address"],
            "type": d["device_type"],
            "region": d["region"],
            "parent_device_id": d["parent_device_id"],
            "child_count": child_count.get(d["id"], 0),
            # In maintenance: not being polled, so `state` is its last reading (stale).
            # The UI badges it so a paused node isn't mistaken for a live healthy one.
            "maintenance": bool(d["maintenance"]),
            "state": state,
            "state_label": _state_label(state),
            "uptime_pct": round(pct, 2),
            "down_label": _fmt_dur(down.get(d["id"], 0.0)),
            # Running on a backup path (primary uplink down, backup carrying): a soft
            # "redundancy gone" badge. Cleared if the node itself is hard DOWN.
            "on_backup": bool(d["on_backup"]) and state not in (DOWN, UNREACHABLE),
            "backup_since": d["backup_since"] if d["on_backup"] else None,
            # Soft "slow link" badge (vs the link's own baseline); None unless degraded.
            "perf": ({
                "metric": d["perf_metric"],
                "baseline_ms": d["perf_baseline"],
                "current_ms": d["perf_current"],
                "since": d["perf_since"],
            } if d["perf_degraded"] else None),
            # SNMP port health, surfaced live so a switch with a down monitored uplink
            # port no longer looks identical to a healthy one. None when the node has no
            # discovered ports (SNMP off / not yet walked).
            "snmp_enabled": bool(d["snmp_enabled"]),
            "ports": ports_by_device.get(d["id"]),
        })
    return out


def topology_graph(cfg: Config = CONFIG, hours: int = 24) -> dict:
    """The whole network as a node-link graph for the topology map, in one payload.

    Nodes reuse `nodes_list` (live state, uptime, on_backup/perf/maintenance badges, the
    port-health summary) so there's a single source of truth for state. Edges expose the
    THREE relationship models the indented tree can't draw on its own:

      * ``primary`` — the denormalised ping parent (`devices.parent_device_id`).
      * ``backup``  — redundant uplinks (`device_links`, kind='backup').
      * ``port``    — the physical "this switch port feeds that device" link
                      (`switch_ports.feeds_device_id`), carrying the port label + whether
                      it is currently alarming. This is the physical layer SNMP unlocked.

    Every edge is parent_id (upstream) -> child_id (downstream); edges to inactive nodes
    are dropped so the map never dangles."""
    nodes = nodes_list(cfg, hours)
    active = {n["id"] for n in nodes}
    edges: list[dict] = []
    for n in nodes:
        p = n["parent_device_id"]
        if p is not None and p in active:
            edges.append({"parent_id": p, "child_id": n["id"], "kind": "primary"})
    with connect(cfg) as conn:
        backups = conn.execute(
            "SELECT child_id, parent_id FROM device_links"
            " WHERE is_active=1 AND kind='backup'").fetchall()
        feeds = conn.execute(
            "SELECT device_id, feeds_device_id, if_index, if_name, if_alias,"
            " monitored, alarm FROM switch_ports WHERE feeds_device_id IS NOT NULL"
        ).fetchall()
    for b in backups:
        if b["child_id"] in active and b["parent_id"] in active:
            edges.append({"parent_id": b["parent_id"], "child_id": b["child_id"],
                          "kind": "backup"})
    for f in feeds:
        if f["device_id"] in active and f["feeds_device_id"] in active:
            base = f["if_name"] or f"if{f['if_index']}"
            label = f"{base} ({f['if_alias']})" if f["if_alias"] else base
            edges.append({
                "parent_id": f["device_id"], "child_id": f["feeds_device_id"],
                "kind": "port", "port_label": label,
                "monitored": bool(f["monitored"]),
                "down": bool(f["monitored"] and f["alarm"]),
            })
    return {"nodes": nodes, "edges": edges}


def network_heatmap(cfg: Config = CONFIG, days: int = 30) -> list[dict]:
    """One cell per calendar day (UTC) for the last `days` days: did any device
    suffer a DOWN outage that day? 'outage' | 'ok' | 'nodata'."""
    win_end = _now()
    win_start = (win_end - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    with connect(cfg) as conn:
        first_poll = conn.execute(
            "SELECT MIN(timestamp) AS t FROM poll_results").fetchone()["t"]
        outages = _outages_in_window(conn, win_start, win_end)
    first_dt = _parse(first_poll) if first_poll else None

    cells: list[dict] = []
    for i in range(days):
        day_start = (win_end - timedelta(days=days - 1 - i)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        has_data = first_dt is not None and first_dt < day_end
        had_outage = any(
            o["final_state"] == DOWN and o["_start"] < day_end and o["_end"] > day_start
            for o in outages
        )
        state = "outage" if had_outage else ("ok" if has_data else "nodata")
        cells.append({"date": day_start.date().isoformat(), "state": state})
    return cells


def nodes_down_on_day(cfg: Config = CONFIG, date_str: str = "") -> list[dict]:
    """Devices that suffered a DOWN outage on the given UTC calendar day, with how
    long they were down *that day* — powers the Nodes page heatmap drill-down.
    Empty list = nothing was down that day."""
    from datetime import datetime
    day_start = datetime.strptime(date_str, "%Y-%m-%d")  # naive UTC midnight
    # Don't count an ongoing outage past 'now' into the future (matters for today).
    clip_end = min(day_start + timedelta(days=1), _now())
    with connect(cfg) as conn:
        outages = _outages_in_window(conn, day_start, clip_end)
        meta = {r["id"]: r for r in conn.execute(
            "SELECT id, name, ip_address, device_type, region"
            " FROM devices WHERE is_active=1")}

    per: dict[int, dict] = {}
    for o in outages:
        if o["final_state"] != DOWN:
            continue
        s = max(o["_start"], day_start)
        e = min(o["_end"], clip_end)
        if e <= s:
            continue
        agg = per.setdefault(o["device_id"], {"down_s": 0.0})
        agg["down_s"] += (e - s).total_seconds()

    out: list[dict] = []
    for did, agg in per.items():
        m = meta.get(did)
        if not m:
            continue
        out.append({
            "id": did,
            "name": m["name"],
            "ip": m["ip_address"],
            "type": m["device_type"],
            "region": m["region"],
            "down_s": int(agg["down_s"]),
            "down_label": _fmt_dur(agg["down_s"]),
        })
    out.sort(key=lambda n: -n["down_s"])
    return out


def device_trend(cfg: Config = CONFIG, *, device_id: int = 0, hours: int = 168) -> list[dict]:
    """Hourly latency/loss/uptime series for one device (default last 7 days),
    oldest first — powers a per-device trend chart. Reads the compact `poll_rollups`
    tier, not raw polls, so it stays cheap over long windows. Hours with no rollup
    simply don't appear (gaps render as gaps); `uptime_pct` is the share of that
    hour's polls that were UP."""
    since = (_now() - timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0)
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT bucket, samples, latency_avg, latency_min, latency_max,"
            " loss_avg, down_polls, degraded_polls, up_polls"
            " FROM poll_rollups WHERE device_id = ? AND bucket >= ?"
            " ORDER BY bucket",
            (device_id, since.isoformat(timespec="seconds")),
        ).fetchall()
    series = []
    for r in rows:
        total = r["samples"] or 0
        series.append({
            "bucket": r["bucket"],
            "latency_avg": r["latency_avg"],
            "latency_min": r["latency_min"],
            "latency_max": r["latency_max"],
            "loss_avg": r["loss_avg"],
            "uptime_pct": round(100.0 * r["up_polls"] / total, 1) if total else None,
            "down_polls": r["down_polls"],
            "degraded_polls": r["degraded_polls"],
            "samples": total,
        })
    return series


def logs(cfg: Config = CONFIG, *, query: str = "", limit: int = 25,
         offset: int = 0) -> dict:
    """Historical (resolved) outages for the Logs table, newest first, with a
    free-text filter over device name / region / cause and simple pagination."""
    q = (query or "").strip().lower()
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT o.id, o.device_id, o.started_at, o.resolved_at, o.final_state,"
            " o.root_cause, o.resolution_notes, o.acknowledged_by,"
            " d.name, d.region"
            " FROM outages o JOIN devices d ON d.id = o.device_id"
            " WHERE o.resolved_at IS NOT NULL ORDER BY o.id DESC"
        ).fetchall()

    matched = []
    for r in rows:
        hay = f"{r['name']} {r['region']} {r['root_cause'] or ''}".lower()
        if q and q not in hay:
            continue
        dur = (_parse(r["resolved_at"]) - _parse(r["started_at"])).total_seconds()
        matched.append({
            "id": r["id"],
            "incident": f"INC-{r['id']:04d}",
            "timestamp": r["started_at"],
            "resolved_at": r["resolved_at"],
            "name": r["name"],
            "region": r["region"],
            "state": r["final_state"],
            "duration_s": int(dur),
            "duration_label": _fmt_dur(dur),
            "root_cause": r["root_cause"] or "—",
            "resolution_notes": r["resolution_notes"],
            "acknowledged_by": r["acknowledged_by"],
        })

    total = len(matched)
    page = matched[offset:offset + limit]
    return {"total": total, "limit": limit, "offset": offset, "entries": page}


# --- write actions ----------------------------------------------------------
def assign_and_ack(outage_id: int, technician: str, cfg: Config = CONFIG) -> bool:
    """Assign a technician + acknowledge in one step (the dashboard's primary
    action). Acknowledging is what stops the escalation ladder, so this reuses the
    canonical path in notifiers."""
    from wisp.egress.notifiers import acknowledge_outage
    return acknowledge_outage(outage_id, technician, cfg)


def submit_postmortem(outage_id: int, root_cause: str, notes: str,
                      cfg: Config = CONFIG) -> bool:
    """Record the operator-confirmed root cause + resolution notes on a resolved
    outage. Only writes to an already-resolved outage (post-mortem comes after
    recovery)."""
    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE outages SET root_cause = ?, resolution_notes = ?"
                " WHERE id = ? AND resolved_at IS NOT NULL",
                (root_cause or None, notes or None, outage_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


DISMISSED_NOTE = "Dismissed — no post-mortem logged"


def dismiss_outage(outage_id: int, cfg: Config = CONFIG) -> bool:
    """Clear a recovered outage off the triage feed without writing a real
    post-mortem. Stamps a sentinel resolution so the row drops out of the
    pending-post-mortem bucket but stays in the downtime history (analytics are
    untouched). Only applies to an already-resolved, still-undocumented outage."""
    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE outages SET root_cause = ?, resolution_notes = ?"
                " WHERE id = ? AND resolved_at IS NOT NULL"
                "   AND root_cause IS NULL AND resolution_notes IS NULL",
                ("Dismissed", DISMISSED_NOTE, outage_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


# --- DB backup --------------------------------------------------------------
def create_backup(cfg: Config = CONFIG) -> bytes:
    """A consistent copy of the DB via SQLite `VACUUM INTO` (safe while WAL is live),
    so a lost wisp.db doesn't mean re-onboarding config + PIN + team (§8.15)."""
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dest = os.path.join(td, "backup.db")
        with connect(cfg) as conn:
            conn.execute("VACUUM INTO ?", (dest,))
        with open(dest, "rb") as fh:
            return fh.read()


def backup_filename() -> str:
    return f"wisp-backup-{datetime.now(timezone.utc):%Y%m%d}.db"


# --- team directory (workers; plan §8.5) ------------------------------------
WORKER_ROLES = ("owner", "operator", "tech")
# Channels are role-based (config.py ntfy_topic_*), so a worker is just an
# identity + role — no per-person routing key.
_WORKER_FIELDS = ("name", "role", "region", "is_active", "notes")


class WorkerError(ValueError):
    """A bad worker payload (validation), surfaced to the UI as a 422."""


class LastOwnerError(WorkerError):
    """Removing/deactivating the last active owner — surfaced as a 409 (conflict)."""


def list_workers(cfg: Config = CONFIG) -> list[dict]:
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT id, name, role, region,"
            " is_active, notes, created_at FROM workers ORDER BY"
            " CASE role WHEN 'owner' THEN 0 WHEN 'operator' THEN 1 ELSE 2 END, name"
        ).fetchall()
    return [dict(r) for r in rows]


def _clean_worker_payload(data: dict) -> dict:
    def _str(key, *, required=False):
        return _payload_str(data, key, WorkerError, required=required)

    name = _str("name", required=True)
    role = (data.get("role") or "tech").strip().lower()
    if role not in WORKER_ROLES:
        raise WorkerError(f"role must be one of: {', '.join(WORKER_ROLES)}")
    is_active_raw = data.get("is_active", 1)
    is_active = 0 if str(is_active_raw) in ("0", "false", "False", "") else 1
    return {
        "name": name, "role": role,
        "region": _str("region"), "is_active": is_active, "notes": _str("notes"),
    }


def _active_owner_ids(conn) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM workers WHERE role='owner' AND is_active=1")]


def _guard_last_owner(conn, worker_id: int, *, still_active_owner: bool) -> None:
    """Block an edit/delete that would remove the last active owner (which would
    orphan escalations). `still_active_owner` is whether the worker remains an
    active owner after the operation."""
    owners = _active_owner_ids(conn)
    if owners == [worker_id] and not still_active_owner:
        raise LastOwnerError(
            "can't remove the last active owner — assign another owner first")


def create_worker(data: dict, cfg: Config = CONFIG) -> int:
    clean = _clean_worker_payload(data)

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                f"INSERT INTO workers ({', '.join(_WORKER_FIELDS)})"
                f" VALUES ({', '.join('?' * len(_WORKER_FIELDS))})",
                tuple(clean[f] for f in _WORKER_FIELDS),
            )
            conn.commit()
            return cur.lastrowid
    return int(write_with_retry(_do) or 0)


def update_worker(worker_id: int, data: dict, cfg: Config = CONFIG) -> bool:
    clean = _clean_worker_payload(data)
    with connect(cfg) as conn:
        exists = conn.execute(
            "SELECT 1 FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not exists:
            return False
        still_owner = clean["role"] == "owner" and clean["is_active"] == 1
        _guard_last_owner(conn, worker_id, still_active_owner=still_owner)

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                f"UPDATE workers SET {', '.join(f + '=?' for f in _WORKER_FIELDS)}"
                " WHERE id=?",
                tuple(clean[f] for f in _WORKER_FIELDS) + (worker_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


def test_channel(target, cfg: Config = CONFIG) -> dict:
    """Send a fixed "✅ WISP test alert" to one of the three role channels
    (owner / operator / tech) through the *current* notifier — the go-live check
    that the channel works before a real outage needs it. Returns
    {ok, detail, channel, recipient, role}; the network send is OUTSIDE any DB txn,
    matching the dispatcher."""
    from wisp.egress.notifiers import build_notifier, role_topic
    role = str(target or "tech").strip().lower()
    if role not in WORKER_ROLES:
        raise WorkerError(f"channel must be one of: {', '.join(WORKER_ROLES)}")
    recipient = role_topic(role, cfg)
    notifier = build_notifier(cfg)
    res = notifier.send(recipient, "✅ WISP test alert",
                        "This is a test alert from the HANSA dashboard.", 3)
    detail = res.detail or (
        f"delivered to {notifier.channel} ({recipient})" if res.ok
        else "send failed")
    return {"ok": res.ok, "detail": detail, "channel": notifier.channel,
            "recipient": recipient, "role": role}


def delete_worker(worker_id: int, cfg: Config = CONFIG) -> dict:
    with connect(cfg) as conn:
        row = conn.execute("SELECT 1 FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "worker not found"}
        _guard_last_owner(conn, worker_id, still_active_owner=False)

    def _do():
        with connect(cfg) as conn:
            # attendance REFERENCES workers(id); clear it first or foreign_keys=ON
            # rejects the worker DELETE (same rule as delete_device's FK tables).
            conn.execute("DELETE FROM attendance WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM workers WHERE id=?", (worker_id,))
            conn.commit()
            return True
    write_with_retry(_do)
    return {"ok": True}


# --- attendance (daily operator roster; who showed up, by date) --------------
# A "daily present toggle": one row per operator per UTC day they were present.
# Surfaced on the Team page (today's roster + a recent-days grid) and on triage
# cards ("who was on duty that day"). Operators only — the field staff a village
# WISP rosters; owner/tech are excluded from the toggle (see set_attendance).

def _valid_day(day: str) -> str:
    """Coerce/validate a 'YYYY-MM-DD' day, defaulting to today (UTC)."""
    day = (day or "").strip() or _today()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise WorkerError("day must be YYYY-MM-DD")
    return day


def list_operators(cfg: Config = CONFIG) -> list[dict]:
    """Active operators — the people whose attendance the roster tracks."""
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT id, name, region FROM workers"
            " WHERE role='operator' AND is_active=1 ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def attendance_overview(cfg: Config = CONFIG, days: int = 14) -> dict:
    """Team-page roster view: active operators + a recent-day presence grid.

    Returns {today, days:[oldest…today], operators:[{id, name, present_today,
    present_days:[…]}]}. `present_days` is the subset of `days` the operator was
    marked present, so the UI can paint a per-operator timeline."""
    days = max(1, min(int(days or 14), 60))
    end = _now().date()
    window = [(end - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    today, win_start, win_set = window[-1], window[0], set(window)
    with connect(cfg) as conn:
        ops = conn.execute(
            "SELECT id, name FROM workers WHERE role='operator' AND is_active=1"
            " ORDER BY name").fetchall()
        att = conn.execute(
            "SELECT worker_id, day FROM attendance WHERE day >= ?",
            (win_start,)).fetchall()
    present: dict[int, set[str]] = {}
    for r in att:
        present.setdefault(r["worker_id"], set()).add(r["day"])
    operators = [{
        "id": o["id"],
        "name": o["name"],
        "present_today": today in present.get(o["id"], set()),
        "present_days": sorted(d for d in present.get(o["id"], set()) if d in win_set),
    } for o in ops]
    return {"today": today, "days": window, "operators": operators}


def set_attendance(worker_id: int, present: bool, day: str = "",
                   cfg: Config = CONFIG) -> dict:
    """Mark/unmark one operator present for a day (default today). Idempotent:
    present=True does INSERT OR IGNORE, present=False DELETEs the row. Attendance is
    operators-only — a non-operator worker is a 422 (WorkerError)."""
    day = _valid_day(day)
    with connect(cfg) as conn:
        w = conn.execute(
            "SELECT role FROM workers WHERE id=?", (worker_id,)).fetchone()
    if not w:
        return {"ok": False, "reason": "worker not found"}
    if w["role"] != "operator":
        raise WorkerError("attendance is tracked for operators only")

    def _do():
        with connect(cfg) as conn:
            if present:
                conn.execute(
                    "INSERT OR IGNORE INTO attendance (worker_id, day) VALUES (?, ?)",
                    (worker_id, day))
            else:
                conn.execute(
                    "DELETE FROM attendance WHERE worker_id=? AND day=?",
                    (worker_id, day))
            conn.commit()
            return True
    write_with_retry(_do)
    return {"ok": True, "worker_id": worker_id, "day": day, "present": bool(present)}


# --- device inventory management (config from the UI) -----------------------
DEVICE_TYPES = ("core", "tower", "relay", "sector", "backhaul")
_DEVICE_FIELDS = ("name", "ip_address", "device_type", "region",
                  "parent_device_id", "technician_phone")


class DeviceError(ValueError):
    """A bad device payload (validation), surfaced to the UI as a 422."""


# Snappy reachability probe for the add-node flow (kept small so the UI doesn't
# hang on the spinner). Tuned for "is this a real, currently-up host?", not the
# daemon's steady-state monitoring.
_REACH_COUNT = 2
_REACH_TIMEOUT_S = 1


def check_reachable(ip: str, cfg: Config = CONFIG) -> dict:
    """Quick ICMP reachability check for the UI before a node is saved — catches a
    typo'd / wrong address that would otherwise sit there looking like a permanent
    outage. Shells out to the system `ping` so the stdlib-only dashboard needs no
    icmplib (the daemon's prober). Returns {reachable, detail, rtt_ms, ip}:

      * reachable True  — host answered
      * reachable False — no reply (could be a typo, or a real host that's down now;
                          the UI offers "add anyway")
      * reachable None  — couldn't probe (no `ping` binary); never blocks the add

    Raises DeviceError on a malformed IP (same 422 the create path would give)."""
    import re
    import shutil
    import subprocess
    import sys

    ip = (ip or "").strip()
    try:
        version = ipaddress.ip_address(ip).version
    except ValueError:
        raise DeviceError(f"'{ip}' is not a valid IP address")

    binary = shutil.which("ping")
    if not binary:
        return {"reachable": None, "detail": "ping unavailable — reachability not checked",
                "rtt_ms": None, "ip": ip}

    # Ping flags differ by OS: Linux uses "-c count / -W seconds"; Windows ping uses
    # "-n count / -w milliseconds". Sending Linux flags to Windows ping makes every
    # check read "no reply", which silently blocks adding ANY device.
    if sys.platform.startswith("win"):
        cmd = [binary, "-n", str(_REACH_COUNT), "-w", str(_REACH_TIMEOUT_S * 1000)]
    else:
        cmd = [binary, "-c", str(_REACH_COUNT), "-W", str(_REACH_TIMEOUT_S)]
    if version == 6:
        cmd.append("-6")
    cmd.append(ip)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_REACH_COUNT * _REACH_TIMEOUT_S + 3,
        )
    except subprocess.TimeoutExpired:
        return {"reachable": False, "detail": "no reply (timed out)", "rtt_ms": None, "ip": ip}
    except OSError as exc:
        return {"reachable": None, "detail": f"couldn't run ping: {exc}",
                "rtt_ms": None, "ip": ip}

    out = proc.stdout or ""
    # Windows ping can exit 0 while reporting "Destination host unreachable" / "100%
    # loss" for a non-answering host, so don't trust the exit code alone there.
    answered = proc.returncode == 0 and "unreachable" not in out.lower() \
        and "100% loss" not in out.lower()
    if answered:
        # RTT line differs by OS: Linux "min/avg/max = .../12.3/...", Windows "Average = 12ms".
        avg = re.search(r"=\s*[\d.]+/([\d.]+)/", out) or re.search(r"Average\s*=\s*(\d+)\s*ms", out)
        rtt = round(float(avg.group(1)), 1) if avg else None
        detail = f"host is up - {rtt} ms avg" if rtt is not None else "host is up"
        return {"reachable": True, "detail": detail, "rtt_ms": rtt, "ip": ip}
    return {"reachable": False, "detail": "no reply (100% packet loss)",
            "rtt_ms": None, "ip": ip}


def list_devices(cfg: Config = CONFIG) -> list[dict]:
    """Full device records (every editable column) + each node's child count + its
    BACKUP parent edges, for the Nodes-page inventory editor and the parent-node
    dropdown."""
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT d.id, d.name, d.ip_address, d.device_type, d.region,"
            " d.is_active, d.maintenance, d.parent_device_id, d.technician_phone,"
            " d.snmp_enabled, d.snmp_version, d.snmp_community, d.snmp_port,"
            " p.name AS parent_name,"
            " (SELECT COUNT(*) FROM devices c WHERE c.parent_device_id = d.id) AS child_count,"
            " (SELECT COUNT(*) FROM switch_ports sp WHERE sp.device_id = d.id) AS port_count"
            " FROM devices d LEFT JOIN devices p ON p.id = d.parent_device_id"
            " WHERE d.is_active = 1 ORDER BY d.id"
        ).fetchall()
        edges = conn.execute(
            "SELECT l.child_id, l.parent_id, p.name AS parent_name"
            " FROM device_links l JOIN devices p ON p.id = l.parent_id"
            " WHERE l.is_active=1 AND l.kind='backup' AND p.is_active=1"
            " ORDER BY p.name"
        ).fetchall()
    backups_by_child: dict[int, list[dict]] = {}
    for e in edges:
        backups_by_child.setdefault(e["child_id"], []).append(
            {"id": e["parent_id"], "name": e["parent_name"]})
    out = []
    for r in rows:
        d = dict(r)
        d["backup_parents"] = backups_by_child.get(r["id"], [])
        out.append(d)
    return out


def add_backup_link(child_id: int, parent_id: int, cfg: Config = CONFIG) -> dict:
    """Add a BACKUP parent edge (child runs a redundant uplink to `parent_id`). Validated
    like the primary parent: both nodes must exist and be active, can't be the same node
    or the existing primary, can't duplicate an edge, and must not close a topology loop
    over the FULL edge set. Returns {ok}; raises DeviceError (422) on a bad edge."""
    with connect(cfg) as conn:
        meta = {r["id"]: r for r in conn.execute(
            "SELECT id, parent_device_id FROM devices WHERE is_active=1")}
        if child_id not in meta:
            raise DeviceError("node not found")
        if parent_id not in meta:
            raise DeviceError("backup parent does not exist")
        if parent_id == child_id:
            raise DeviceError("a node can't be its own backup parent")
        if meta[child_id]["parent_device_id"] == parent_id:
            raise DeviceError("that node is already the primary parent")
        parents = {cid: r["parent_device_id"] for cid, r in meta.items()}
        backups = _backup_map(conn)
        if parent_id in backups.get(child_id, set()):
            raise DeviceError("that backup link already exists")
        # cycle check: walk UP from the proposed parent over the existing edge set;
        # reaching the child means the new edge would close a loop.
        edges_of = _combined_edges(parents, backups)
        stack, seen = [parent_id], set()
        while stack:
            cur = stack.pop()
            if cur == child_id:
                raise DeviceError("that backup link would create a topology loop")
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges_of.get(cur, ()))

    def _do():
        with connect(cfg) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO device_links (child_id, parent_id, kind)"
                " VALUES (?,?,'backup')", (child_id, parent_id))
            conn.commit()
            return True
    write_with_retry(_do)
    return {"ok": True}


def remove_backup_link(child_id: int, parent_id: int, cfg: Config = CONFIG) -> dict:
    """Drop a BACKUP parent edge. Returns {ok, reason}."""
    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "DELETE FROM device_links WHERE child_id=? AND parent_id=? AND kind='backup'",
                (child_id, parent_id))
            conn.commit()
            return cur.rowcount > 0
    ok = bool(write_with_retry(_do))
    return {"ok": ok} if ok else {"ok": False, "reason": "backup link not found"}


def _clean_device_payload(data: dict, *, parents: dict[int, int | None],
                          device_id: int | None,
                          backups: dict[int, set[int]] | None = None) -> dict:
    """Validate + normalise a device create/update payload. Raises DeviceError
    with a human message on the first problem found. `backups` (child -> parent ids,
    from device_links) lets the cycle check span the FULL edge set, not just the
    primary chain."""
    def _str(key, *, required=False, default=None):
        return _payload_str(data, key, DeviceError, required=required, default=default)

    name = _str("name", required=True)
    ip_address = _str("ip_address", required=True)
    # Reject anything that isn't a real IPv4/IPv6 address — a typo'd address would
    # ping-fail forever and look like a permanent outage, so don't accept the node.
    try:
        ipaddress.ip_address(str(ip_address))
    except ValueError:
        raise DeviceError(f"'{ip_address}' is not a valid IP address")
    device_type = _str("device_type")
    if device_type and device_type not in DEVICE_TYPES:
        raise DeviceError(f"device type must be one of: {', '.join(DEVICE_TYPES)}")
    region = _str("region")
    technician_phone = _str("technician_phone")

    parent_raw = data.get("parent_device_id")
    parent_id = None
    if parent_raw not in (None, "", "null"):
        try:
            parent_id = int(parent_raw)
        except (TypeError, ValueError):
            raise DeviceError("parent node is invalid")
        if parent_id not in parents:
            raise DeviceError("parent node does not exist")
        if parent_id == device_id:
            raise DeviceError("a node can't be its own parent")
        # Reject cycles over the FULL edge set (primary + backup links), not just the
        # primary chain: walk UP from the proposed parent following every parent edge;
        # if we can reach this device, the new edge would close a topology loop.
        edges_of = _combined_edges(parents, backups)
        stack = [parent_id]
        seen: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur == device_id:
                raise DeviceError("that parent would create a topology loop")
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges_of.get(cur, ()))

    return {
        "name": name, "ip_address": ip_address, "device_type": device_type,
        "region": region, "parent_device_id": parent_id,
        "technician_phone": technician_phone,
    }


def _parent_map(conn) -> dict[int, int | None]:
    return {r["id"]: r["parent_device_id"]
            for r in conn.execute("SELECT id, parent_device_id FROM devices")}


def _backup_map(conn) -> dict[int, set[int]]:
    """child_id -> set of BACKUP parent ids (active device_links edges)."""
    out: dict[int, set[int]] = {}
    for r in conn.execute(
        "SELECT child_id, parent_id FROM device_links WHERE is_active=1 AND kind='backup'"
    ):
        out.setdefault(r["child_id"], set()).add(r["parent_id"])
    return out


def _combined_edges(parents: dict[int, int | None],
                    backups: dict[int, set[int]] | None) -> dict[int, set[int]]:
    """child_id -> all parent ids (primary + backups), for DAG cycle detection."""
    edges: dict[int, set[int]] = {}
    for cid, pid in parents.items():
        s: set[int] = set()
        if pid is not None:
            s.add(pid)
        if backups:
            s |= backups.get(cid, set())
        edges[cid] = s
    return edges


def create_device(data: dict, cfg: Config = CONFIG) -> int:
    """Insert a new device (UI 'Add node'). Returns the new id. Raises DeviceError
    on bad input."""
    with connect(cfg) as conn:
        clean = _clean_device_payload(data, parents=_parent_map(conn),
                                      device_id=None, backups=_backup_map(conn))

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                f"INSERT INTO devices ({', '.join(_DEVICE_FIELDS)}, is_active)"
                f" VALUES ({', '.join('?' * len(_DEVICE_FIELDS))}, 1)",
                tuple(clean[f] for f in _DEVICE_FIELDS),
            )
            conn.commit()
            return cur.lastrowid
    return int(write_with_retry(_do) or 0)


def update_device(device_id: int, data: dict, cfg: Config = CONFIG) -> bool:
    """Edit an existing device. Raises DeviceError on bad input."""
    with connect(cfg) as conn:
        exists = conn.execute(
            "SELECT 1 FROM devices WHERE id=? AND is_active=1", (device_id,)).fetchone()
        if not exists:
            return False
        clean = _clean_device_payload(data, parents=_parent_map(conn),
                                      device_id=device_id, backups=_backup_map(conn))

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                f"UPDATE devices SET {', '.join(f + '=?' for f in _DEVICE_FIELDS)}"
                " WHERE id=? AND is_active=1",
                tuple(clean[f] for f in _DEVICE_FIELDS) + (device_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


def set_maintenance(device_id: int, on: bool, cfg: Config = CONFIG) -> bool:
    """Toggle a node's maintenance flag. In maintenance the daemon stops pinging the
    node entirely (load_device_meta excludes maintenance=1) and so pages no one for
    it; the device-set reload applies the change in-process within a poll cycle. The
    node stays in the inventory and on the dashboard (badged) so it's clear it's
    intentionally paused, not silently gone. Returns False if the node doesn't exist."""
    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE devices SET maintenance=? WHERE id=? AND is_active=1",
                (1 if on else 0, device_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


# --- SNMP port status (Phase 9 Part B) --------------------------------------
SNMP_VERSIONS = ("2c",)   # room for '3' later; v3 auth/priv is out of scope for now


def set_snmp_config(device_id: int, data: dict, cfg: Config = CONFIG) -> bool:
    """Set a device's SNMP config (enable + community + version + port). Kept separate
    from the device CRUD full-replace so editing it never disturbs name/IP/topology.
    Returns False if the node doesn't exist. Raises DeviceError on bad input."""
    enabled = 0 if str(data.get("snmp_enabled", 0)) in ("0", "false", "False", "", "None") else 1
    version = (str(data.get("snmp_version") or "2c")).strip().lower()
    if version not in SNMP_VERSIONS:
        raise DeviceError(f"SNMP version must be one of: {', '.join(SNMP_VERSIONS)}")
    community = _payload_str(data, "snmp_community", DeviceError)
    if enabled and not community:
        raise DeviceError("an SNMP community is required to enable SNMP")
    try:
        port = int(data.get("snmp_port") or 161)
    except (TypeError, ValueError):
        raise DeviceError("SNMP port must be a number")
    if not (1 <= port <= 65535):
        raise DeviceError("SNMP port must be 1–65535")

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE devices SET snmp_enabled=?, snmp_version=?, snmp_community=?,"
                " snmp_port=? WHERE id=? AND is_active=1",
                (enabled, version, community, port, device_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


def list_switch_ports(device_id: int, cfg: Config = CONFIG) -> list[dict]:
    """Discovered ports for one switch (the dashboard SNMP panel): live oper/admin
    status, the monitor flag, and which downstream device each port feeds."""
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT sp.id, sp.if_index, sp.if_name, sp.if_alias, sp.admin_status,"
            " sp.oper_status, sp.monitored, sp.feeds_device_id, sp.alarm, sp.alarm_since,"
            " sp.in_bps, sp.out_bps, sp.if_speed_bps, sp.bw_threshold_mbps,"
            " sp.bw_direction, sp.bw_alarm, sp.bw_alarm_since,"
            " sp.updated_at, f.name AS feeds_name"
            " FROM switch_ports sp LEFT JOIN devices f ON f.id = sp.feeds_device_id"
            " WHERE sp.device_id=? ORDER BY sp.if_index", (device_id,),
        ).fetchall()
    return [{
        "id": r["id"],
        "if_index": r["if_index"],
        "if_name": r["if_name"],
        "if_alias": r["if_alias"],
        "admin_status": r["admin_status"],
        "oper_status": r["oper_status"],
        "monitored": bool(r["monitored"]),
        "feeds_device_id": r["feeds_device_id"],
        "feeds_name": r["feeds_name"],
        "alarm": bool(r["alarm"]),
        "alarm_since": r["alarm_since"],
        # Live bandwidth (current throughput from the last counter delta), in Mbps for the
        # UI, plus the link's negotiated capacity and the operator-set low-bw threshold.
        "in_mbps": round(r["in_bps"] / 1e6, 3) if r["in_bps"] is not None else None,
        "out_mbps": round(r["out_bps"] / 1e6, 3) if r["out_bps"] is not None else None,
        "link_mbps": round(r["if_speed_bps"] / 1e6, 1) if r["if_speed_bps"] else None,
        "bw_threshold_mbps": r["bw_threshold_mbps"],
        "bw_direction": r["bw_direction"] or "either",
        "bw_alarm": bool(r["bw_alarm"]),
        "bw_alarm_since": r["bw_alarm_since"],
        "updated_at": r["updated_at"],
    } for r in rows]


def set_port_monitored(port_id: int, monitored: bool, cfg: Config = CONFIG) -> bool:
    """Flag/unflag one port for alarming. Toggling re-arms detection from scratch
    (resets the flap-suppression streak + clears any standing alarm), so an un-monitored
    port never lingers in alarm and a freshly-monitored one isn't instantly down."""
    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET monitored=?, down_streak=0, alarm=0,"
                " alarm_since=NULL WHERE id=?", (1 if monitored else 0, port_id))
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


def set_port_feeds(port_id: int, feeds_device_id, cfg: Config = CONFIG) -> bool:
    """Map a port to the downstream device it feeds (or clear with None) — the bridge
    that lets a monitored port-down fold into that device's outage. Raises DeviceError
    if the target device doesn't exist or is the switch itself."""
    fid = None
    if feeds_device_id not in (None, "", "null", 0, "0"):
        try:
            fid = int(feeds_device_id)
        except (TypeError, ValueError):
            raise DeviceError("fed device is invalid")
    with connect(cfg) as conn:
        port = conn.execute(
            "SELECT device_id FROM switch_ports WHERE id=?", (port_id,)).fetchone()
        if not port:
            return False
        if fid is not None:
            if not conn.execute("SELECT 1 FROM devices WHERE id=? AND is_active=1",
                                (fid,)).fetchone():
                raise DeviceError("fed device does not exist")
            if fid == port["device_id"]:
                raise DeviceError("a port can't feed its own switch")

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET feeds_device_id=? WHERE id=?", (fid, port_id))
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


BW_DIRECTIONS = ("in", "out", "either", "total")


def set_port_bandwidth(port_id: int, threshold_mbps, direction=None,
                       cfg: Config = CONFIG) -> bool:
    """Assign (or clear) a port's low-bandwidth alarm threshold + watched direction. A
    blank/None threshold clears the bandwidth alarm entirely; otherwise the daemon pages
    when the port's throughput stays below `threshold_mbps` (in the chosen direction) for
    `WISP_SNMP_BW_CONSECUTIVE` walks. Setting/changing it re-arms detection (resets the
    bw streak + clears any standing alarm) so a new floor never inherits a stale alarm —
    same discipline as set_port_monitored. Raises DeviceError on bad input."""
    thr = None
    if threshold_mbps not in (None, "", "null", "None"):
        try:
            thr = float(threshold_mbps)
        except (TypeError, ValueError):
            raise DeviceError("bandwidth threshold must be a number (Mbps)")
        if thr < 0:
            raise DeviceError("bandwidth threshold can't be negative")
    direction = (str(direction).strip().lower() if direction else "either")
    if direction not in BW_DIRECTIONS:
        raise DeviceError(f"bandwidth direction must be one of: {', '.join(BW_DIRECTIONS)}")

    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET bw_threshold_mbps=?, bw_direction=?,"
                " bw_low_streak=0, bw_alarm=0, bw_alarm_since=NULL WHERE id=?",
                (thr, direction, port_id))
            conn.commit()
            return cur.rowcount > 0
    return bool(write_with_retry(_do))


def delete_device(device_id: int, cfg: Config = CONFIG) -> dict:
    """Remove a device and its monitoring history (polls/outages/alerts). Blocked
    if it still has child nodes — reassign or remove those first, so topology never
    dangles. Returns {ok, reason}."""
    with connect(cfg) as conn:
        row = conn.execute(
            "SELECT name FROM devices WHERE id=? AND is_active=1", (device_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "node not found"}
        children = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE parent_device_id=? AND is_active=1",
            (device_id,)).fetchone()[0]
    if children:
        return {"ok": False, "reason": f"node has {children} child node(s); reassign them first"}

    def _do():
        with connect(cfg) as conn:
            with transaction(conn):
                # Delete every child row that REFERENCES devices(id) before the device
                # itself, or foreign_keys=ON rejects the final DELETE. Any table added
                # later with a devices FK (poll_rollups @0007, device_perf @0008) must be
                # listed here too — that omission is exactly what broke node deletion.
                conn.execute("DELETE FROM escalations WHERE outage_id IN"
                             " (SELECT id FROM outages WHERE device_id=?)", (device_id,))
                conn.execute("DELETE FROM alert_log WHERE device_id=? OR outage_id IN"
                             " (SELECT id FROM outages WHERE device_id=?)",
                             (device_id, device_id))
                conn.execute("DELETE FROM outages WHERE device_id=?", (device_id,))
                conn.execute("DELETE FROM poll_results WHERE device_id=?", (device_id,))
                conn.execute("DELETE FROM poll_rollups WHERE device_id=?", (device_id,))
                conn.execute("DELETE FROM device_perf WHERE device_id=?", (device_id,))
                # device_links REFERENCES devices(id) in BOTH columns — clear edges where
                # this node is the child OR a (backup) parent before the device row.
                conn.execute("DELETE FROM device_links WHERE child_id=? OR parent_id=?",
                             (device_id, device_id))
                conn.execute("DELETE FROM device_redundancy WHERE device_id=?", (device_id,))
                # switch_ports REFERENCES devices(id) in BOTH device_id and
                # feeds_device_id — clear a port whether this node is the switch OR the
                # downstream device it feeds.
                conn.execute("DELETE FROM switch_ports WHERE device_id=? OR feeds_device_id=?",
                             (device_id, device_id))
                conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            return True
    write_with_retry(_do)
    return {"ok": True}
