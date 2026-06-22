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
            "SELECT id, name, ip_address, device_type, region"
            " FROM devices WHERE is_active=1 ORDER BY id"
        ).fetchall()
        outages = _outages_in_window(conn, win_start, win_end)
        states = latest_states(conn)
    down = _downtime_by_device(outages, win_start, win_end, only_down=False)
    window_s = (win_end - win_start).total_seconds()

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
            "state": state,
            "state_label": _state_label(state),
            "uptime_pct": round(pct, 2),
            "down_label": _fmt_dur(down.get(d["id"], 0.0)),
        })
    return out


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
            conn.execute("DELETE FROM workers WHERE id=?", (worker_id,))
            conn.commit()
            return True
    write_with_retry(_do)
    return {"ok": True}


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

    ip = (ip or "").strip()
    try:
        version = ipaddress.ip_address(ip).version
    except ValueError:
        raise DeviceError(f"'{ip}' is not a valid IP address")

    binary = shutil.which("ping")
    if not binary:
        return {"reachable": None, "detail": "ping unavailable — reachability not checked",
                "rtt_ms": None, "ip": ip}

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

    if proc.returncode == 0:
        avg = re.search(r"=\s*[\d.]+/([\d.]+)/", proc.stdout or "")
        rtt = round(float(avg.group(1)), 1) if avg else None
        detail = f"host is up — {rtt} ms avg" if rtt is not None else "host is up"
        return {"reachable": True, "detail": detail, "rtt_ms": rtt, "ip": ip}
    return {"reachable": False, "detail": "no reply (100% packet loss)",
            "rtt_ms": None, "ip": ip}


def list_devices(cfg: Config = CONFIG) -> list[dict]:
    """Full device records (every editable column) + each node's child count, for
    the Nodes-page inventory editor and the parent-node dropdown."""
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT d.id, d.name, d.ip_address, d.device_type, d.region,"
            " d.is_active, d.parent_device_id, d.technician_phone,"
            " p.name AS parent_name,"
            " (SELECT COUNT(*) FROM devices c WHERE c.parent_device_id = d.id) AS child_count"
            " FROM devices d LEFT JOIN devices p ON p.id = d.parent_device_id"
            " WHERE d.is_active = 1 ORDER BY d.id"
        ).fetchall()
    return [dict(r) for r in rows]


def _clean_device_payload(data: dict, *, parents: dict[int, int | None],
                          device_id: int | None) -> dict:
    """Validate + normalise a device create/update payload. Raises DeviceError
    with a human message on the first problem found."""
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
        # walk the parent chain to reject cycles
        cur = parent_id
        seen = set()
        while cur is not None:
            if cur == device_id:
                raise DeviceError("that parent would create a topology loop")
            if cur in seen:
                break
            seen.add(cur)
            cur = parents.get(cur)

    return {
        "name": name, "ip_address": ip_address, "device_type": device_type,
        "region": region, "parent_device_id": parent_id,
        "technician_phone": technician_phone,
    }


def _parent_map(conn) -> dict[int, int | None]:
    return {r["id"]: r["parent_device_id"]
            for r in conn.execute("SELECT id, parent_device_id FROM devices")}


def create_device(data: dict, cfg: Config = CONFIG) -> int:
    """Insert a new device (UI 'Add node'). Returns the new id. Raises DeviceError
    on bad input."""
    with connect(cfg) as conn:
        clean = _clean_device_payload(data, parents=_parent_map(conn), device_id=None)

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
        clean = _clean_device_payload(data, parents=_parent_map(conn), device_id=device_id)

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
                # children first to satisfy foreign keys
                conn.execute("DELETE FROM escalations WHERE outage_id IN"
                             " (SELECT id FROM outages WHERE device_id=?)", (device_id,))
                conn.execute("DELETE FROM alert_log WHERE device_id=? OR outage_id IN"
                             " (SELECT id FROM outages WHERE device_id=?)",
                             (device_id, device_id))
                conn.execute("DELETE FROM outages WHERE device_id=?", (device_id,))
                conn.execute("DELETE FROM poll_results WHERE device_id=?", (device_id,))
                conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            return True
    write_with_retry(_do)
    return {"ok": True}
