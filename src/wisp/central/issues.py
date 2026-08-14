from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wisp.central import onuroster, ponfault
from wisp.central.api.common import olt_liveness
from wisp.core.analytics import _parse
from wisp.core.state_machine import DOWN_FAMILY

KINDS: tuple[str, ...] = (
    "device_down",
    "port_down",
    "camera_down",
    "probe_stale",
    "bandwidth",
    "onu_crit",
    "onu_warn",
    "dup_mac",
    "pon_fiber",
    "pon_power",
    "pon_capacity",
    "onu_offline",
)

KIND_LABELS: dict[str, str] = {
    "device_down": "Device not up",
    "port_down": "Port down",
    "camera_down": "Camera dark",
    "probe_stale": "Probe stale",
    "bandwidth": "Bandwidth alarm",
    "onu_crit": "Critical ONU",
    "onu_warn": "Warning ONU",
    "dup_mac": "Duplicate MAC",
    "pon_fiber": "Suspected fiber cut",
    "pon_power": "PON power drop",
    "pon_capacity": "PON at capacity",
    "onu_offline": "ONU offline",
}

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_KIND_RANK = {k: i for i, k in enumerate(KINDS)}


def _row(kind: str, severity: str, *, device_id: int | None, device_name: str,
         region: str | None, subject: str, detail: str,
         since: str | None) -> dict:
    return {"kind": kind, "kind_label": KIND_LABELS.get(kind, kind),
            "severity": severity, "device_id": device_id,
            "device_name": device_name, "region": region or None,
            "subject": subject, "detail": detail, "since": since or None}


def _stale(ts, cutoff: datetime) -> bool:
    if not ts:
        return True
    try:
        return _parse(str(ts)) < cutoff
    except (ValueError, TypeError):
        return True


def _fmt_dbm(value) -> str:
    return f"{float(value):.2f} dBm" if value is not None else "no reading"


def _devices(devs: list[dict], cutoff: datetime,
             live_nodes: set[str]) -> list[dict]:

    out = []
    for d in devs:
        node = d.get("assigned_node_id")
        if d.get("maintenance") or not node or node not in live_nodes:
            continue
        state = str(d.get("state") or "")
        stale = _stale(d.get("state_updated_at"), cutoff)
        if state == "UP" and not stale:
            continue
        if not state:
            detail, severity, sev_state = "never reported", "warning", "UNKNOWN"
        elif stale:
            detail = f"no recent report, last known {state.lower()}"
            severity, sev_state = "warning", "STALE"
        else:
            sev_state = state
            severity = "critical" if state in DOWN_FAMILY else "warning"
            loss = d.get("packet_loss")
            detail = (f"{state.lower()}"
                      + (f", {round(float(loss))}% loss" if loss else ""))
        out.append(_row(
            "device_down", severity, device_id=d["id"],
            device_name=d["name"], region=d.get("region"),
            subject=f"{d['name']} ({d.get('ip_address') or 'no IP'})",
            detail=f"{sev_state} · {detail}",
            since=d.get("outage_started_at") or d.get("state_updated_at")))
    return out


def _ports(store, org: str, down_ids: set[int]) -> list[dict]:
    out = []
    for p in store.down_ports(org):
        frozen = p["device_id"] in down_ids
        detail = "link down"
        if frozen:
            detail += " · switch unreachable, reading frozen"
        out.append(_row(
            "port_down", "info" if frozen else "critical",
            device_id=p["device_id"], device_name=p["switch_name"],
            region=p.get("region"), subject=p["label"], detail=detail,
            since=p.get("alarm_since")))
    return out


def _cameras(store, org: str, down_ids: set[int]) -> list[dict]:
    out = []
    for c in store.dark_cameras(org):
        frozen = c["device_id"] in down_ids
        label = f"CH{int(c['channel_no']) + 1}"
        if c.get("name"):
            label += f" {c['name']}"
        detail = "no video"
        if c.get("ip_address"):
            detail += f" · {c['ip_address']}"
        if frozen:
            detail += " · NVR unreachable, reading frozen"
        out.append(_row(
            "camera_down", "info" if frozen else "critical",
            device_id=c["device_id"], device_name=c["nvr_name"],
            region=c.get("region"), subject=label, detail=detail,
            since=c.get("last_online_at")))
    return out


def _bandwidth(store, org: str) -> list[dict]:
    out = []
    for a in store.low_bandwidth_alarms(org):
        limit = a.get("threshold_mbps")
        out.append(_row(
            "bandwidth", "warning", device_id=a["device_id"],
            device_name=a["switch_name"], region=None, subject=a["label"],
            detail=(f"below floor ({a['direction']}): in {a['in_mbps']} / out "
                    f"{a['out_mbps']} Mbps, floor {limit} Mbps"),
            since=a.get("since")))
    for a in store.high_bandwidth_alarms(org):
        limit = a.get("max_mbps")
        out.append(_row(
            "bandwidth", "warning", device_id=a["device_id"],
            device_name=a["switch_name"], region=None, subject=a["label"],
            detail=(f"over ceiling ({a['direction']}): in {a['in_mbps']} / out "
                    f"{a['out_mbps']} Mbps, ceiling {limit} Mbps"),
            since=a.get("since")))
    return out


def _probes(nodes: list[dict], cutoff: datetime) -> list[dict]:
    out = []
    for n in nodes:
        if not n.get("registered") or n.get("revoked_at"):
            continue
        if not _stale(n.get("last_seen"), cutoff):
            continue
        seen = n.get("last_seen")
        out.append(_row(
            "probe_stale", "critical", device_id=None,
            device_name=n["node_id"], region=None,
            subject=n["node_id"],
            detail=("has never reported" if not seen
                    else "not reporting"
                          + (f" (last seen on v{n['version']})"
                             if n.get("version") else "")),
            since=seen))
    return out


def _optical(store, cfg, org: str, devs: list[dict], now: datetime,
             down_ids: set[int], stale_ids: set[int]) -> list[dict]:
    rows = store.org_onu_rows(org)
    if not rows:
        return []
    seen_rows = [r for r in rows if r["device_id"] not in stale_ids]
    live_rows = [r for r in seen_rows if r["device_id"] not in down_ids]
    roster = onuroster.current_roster(seen_rows, now)
    region_of = {d["id"]: d.get("region") for d in devs}
    out: list[dict] = []

    def _onu_subject(r: dict) -> str:
        who = onuroster.display_name(r)
        port = r.get("pon_port")
        return f"{who} on {port}" if port else str(who)

    for r in roster:
        online = str(r.get("state") or "") == "online"
        dev_id = r["device_id"]
        name = r.get("device_name") or f"#{dev_id}"
        if online and dev_id not in down_ids:
            sev = r.get("severity")
            if sev in ("crit", "warn"):
                out.append(_row(
                    "onu_crit" if sev == "crit" else "onu_warn",
                    "critical" if sev == "crit" else "warning",
                    device_id=dev_id, device_name=name, region=region_of.get(dev_id),
                    subject=_onu_subject(r),
                    detail=f"Rx {_fmt_dbm(r.get('rx_dbm'))}",
                    since=r.get("updated_at")))
        else:
            out.append(_row(
                "onu_offline", "info", device_id=dev_id, device_name=name,
                region=region_of.get(dev_id), subject=_onu_subject(r),
                detail=(f"{r.get('state') or 'unknown'}"
                        + (" (OLT unreachable)" if dev_id in down_ids else "")),
                since=r.get("last_online_at")))

    dists = ponfault.passive_distances(devs, store.list_link_routes(org))
    for f in ponfault.evaluate_org(live_rows, now, passive_dists=dists,
                                   witness_macs=store.onu_place_macs(org)):
        parts = []
        if f.cut_low_m is not None and f.cut_high_m is not None:
            parts.append(f"cut {f.cut_low_m}–{f.cut_high_m} m ranging")
        if f.suspect:
            parts.append(f"near {f.suspect}")
        parts.append(f"{f.dark} of {f.onus_total} ONUs dark")
        if f.dying_gasp:
            parts.append(f"{f.dying_gasp} dying-gasp")
        if f.evidence == "witness":
            parts.append(f"{f.witness_dark} power-backed reference ONU dark"
                         if f.witness_dark
                         else f"{f.witness_alive} power-backed reference ONU still up")
        out.append(_row(
            "pon_fiber" if f.kind == "fiber" else "pon_power",
            "critical" if f.kind == "fiber" else "warning",
            device_id=f.device_id, device_name=f.device_name,
            region=region_of.get(f.device_id), subject=f.pon_port or "unknown PON",
            detail=f"{f.kind}: " + ", ".join(parts),
            since=f.since))

    for d in onuroster.duplicate_macs(live_rows, now):
        if d.online_members < 2:
            continue
        first = d.members[0]
        slots = ", ".join(
            f"{m.get('device_name')} {m.get('pon_port') or '?'}/{m.get('onu_id')}"
            for m in d.members)
        out.append(_row(
            "dup_mac", "critical", device_id=first.get("device_id"),
            device_name=first.get("device_name") or "—",
            region=region_of.get(first.get("device_id")), subject=d.mac,
            detail=f"{d.online_members} slots online at once: {slots}",
            since=None))

    default_cap = cfg.onu_pon_limit
    limits = {d["id"]: (int(d["onu_pon_limit"]) if d.get("onu_pon_limit") is not None
                        else default_cap) for d in devs}
    for c in onuroster.capacity_faults(
            live_rows, now, lambda dev_id: limits.get(dev_id, default_cap)):
        out.append(_row(
            "pon_capacity", "warning", device_id=c.device_id,
            device_name=c.device_name, region=region_of.get(c.device_id),
            subject=c.pon_port,
            detail=f"{c.onus} ONUs against a limit of {c.limit}", since=None))
    return out


def collect(store, cfg, org: str, kinds: list[str] | None = None,
            now: datetime | None = None) -> list[dict]:

    now = now or datetime.now(timezone.utc)
    want = set(kinds) & set(KINDS) if kinds else set(KINDS)
    devs = store.list_org_devices(org)
    down_ids, stale_ids = olt_liveness(devs, now, cfg.central_node_stale_s)
    cutoff = now.replace(tzinfo=None) - timedelta(seconds=cfg.central_node_stale_s)

    out: list[dict] = []
    nodes = (store.list_node_tokens(org)
             if want & {"device_down", "probe_stale"} else [])
    live_nodes = {n["node_id"] for n in nodes
                  if n.get("registered") and not n.get("revoked_at")}
    if "device_down" in want:
        out += _devices(devs, cutoff, live_nodes)
    if "port_down" in want:
        out += _ports(store, org, down_ids)
    if "camera_down" in want:
        out += _cameras(store, org, down_ids)
    if "bandwidth" in want:
        out += _bandwidth(store, org)
    if "probe_stale" in want:
        out += _probes(nodes, cutoff)
    optical = {"onu_crit", "onu_warn", "onu_offline", "dup_mac", "pon_fiber",
               "pon_power", "pon_capacity"}
    if want & optical:
        out += [r for r in _optical(store, cfg, org, devs, now, down_ids, stale_ids)
                if r["kind"] in want]
    out.sort(key=lambda r: (SEVERITY_RANK.get(r["severity"], 9),
                            _KIND_RANK.get(r["kind"], 99),
                            r["device_name"].lower(), r["subject"].lower()))
    return out


def counts(rows: list[dict]) -> dict[str, int]:
    out = {k: 0 for k in KINDS}
    for r in rows:
        out[r["kind"]] = out.get(r["kind"], 0) + 1
    return out
