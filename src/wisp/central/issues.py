"""One flat list of everything currently wrong in an org — the ISSUE view.

Every Home KPI tile already drills into the Network tree, filtered to the
devices behind the number. That answers "which boxes are involved", which is the
wrong question for a shift handover or a report: a switch with four dark ports is
ONE row there and four problems in reality, and an OLT with sixty weak ONUs is
one row hiding sixty. This module names the problems themselves — one row per
port, per ONU, per PON, per probe — so the count on the tile and the length of
the list are the same number.

Read-side ONLY. It composes the same store reads and the same liveness/freshness
gates the tiles and paging shells already use (deliberately re-deriving nothing
of its own: a second opinion about whether an ONU is critical is a second
product), and it can neither write state nor page anybody.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wisp.central import onuroster, ponfault
from wisp.central.api.common import olt_liveness
from wisp.core.analytics import _parse
from wisp.core.state_machine import DOWN_FAMILY

# The closed vocabulary. Each kind is exactly one KPI tile's worth of trouble, so
# a tile can hand its own kind over as a filter and the two can never drift into
# describing different sets. Adding a tile means adding a kind here.
KINDS: tuple[str, ...] = (
    "device_down",
    "port_down",
    "probe_stale",
    "bandwidth",
    "onu_crit",
    "onu_warn",
    "dup_mac",
    # fiber and power are SEPARATE kinds, not one "PON fault": the Home tile
    # counts suspected cuts only (a power drop is recorded and deliberately never
    # paged — see CLAUDE.md), and one kind covering both would make the chip's
    # count disagree with the tile it was opened from.
    "pon_fiber",
    "pon_power",
    "pon_capacity",
    "onu_offline",
)

KIND_LABELS: dict[str, str] = {
    "device_down": "Device not up",
    "port_down": "Port down",
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

# Ranks the list: an unreachable core switch has to sit above a weak ONU
# whatever alphabetical order says.
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_KIND_RANK = {k: i for i, k in enumerate(KINDS)}


# EVERY timestamp lives in `since` and NOWHERE in `detail`. A stamp reaches a
# human raw in exactly two places — a WhatsApp page and this list's PDF — and both
# have to render it in the operator's zone, which is only possible if there is one
# field to render (api/outages.py:issues_pdf does it through notifiers._wa_time).
# A stamp interpolated into a detail sentence would ship UTC beside a localised
# column, which is the 5h30m bug this project already paid for once.
def _row(kind: str, severity: str, *, device_id: int | None, device_name: str,
         region: str | None, subject: str, detail: str,
         since: str | None) -> dict:
    return {"kind": kind, "kind_label": KIND_LABELS.get(kind, kind),
            "severity": severity, "device_id": device_id,
            "device_name": device_name, "region": region or None,
            "subject": subject, "detail": detail, "since": since or None}


def _stale(ts, cutoff: datetime) -> bool:
    """Older than the cutoff (or unparseable/absent) — the same "we have not
    heard recently" test the dashboard's `isStale` applies to the same columns."""
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
    """Monitored devices that are not plainly UP — the "Devices online" tile.

    "Monitored" means exactly what the tile means: assigned to a probe that is
    REGISTERED and not revoked. Maintenance and unassigned devices are excluded
    for the same reason the tile excludes them — neither is a fault, and a muted
    row in a fault list is how a list stops being read."""
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
            # A silent probe is its own issue row (probe_stale); here it means
            # this device's state is a memory, so say that instead of asserting
            # the remembered state as current.
            detail = f"no recent report — last known {state.lower()}"
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
            detail=f"{sev_state} — {detail}",
            since=d.get("outage_started_at") or d.get("state_updated_at")))
    return out


def _ports(store, org: str, down_ids: set[int]) -> list[dict]:
    out = []
    for p in store.down_ports(org):
        frozen = p["device_id"] in down_ids
        detail = "link down"
        if frozen:
            # The honesty rule from CLAUDE.md: readings on an unreachable box are
            # frozen. The row stays (the tile counts it) but must not be read as
            # a live, separate fault.
            detail += " — switch unreachable, reading frozen"
        out.append(_row(
            "port_down", "info" if frozen else "critical",
            device_id=p["device_id"], device_name=p["switch_name"],
            region=p.get("region"), subject=p["label"], detail=detail,
            since=p.get("alarm_since")))
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
    """Registered probes that have gone quiet — the "Stale probes" tile. Read off
    `list_node_tokens` (what the dashboard lists), so a revoked probe or a bare
    heartbeat identity nobody registered never appears as a fault."""
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
    """The optical plane's six tiles, over the SAME freshest-walk-per-OLT view
    `pon_summary` builds — stale OLTs dropped whole, a confirmed-down OLT's ONUs
    counted as offline rather than graded. Reading optics from a second view is
    how a strip and its drill-down end up describing different walks."""
    rows = store.org_onu_rows(org)
    if not rows:
        return []
    seen_rows = [r for r in rows if r["device_id"] not in stale_ids]
    live_rows = [r for r in seen_rows if r["device_id"] not in down_ids]
    roster = onuroster.current_roster(seen_rows, now)
    region_of = {d["id"]: d.get("region") for d in devs}
    out: list[dict] = []

    def _onu_subject(r: dict) -> str:
        # display_name puts the OPERATOR's name first: an issue row a tech reads
        # at a site has to name the subscriber the way the field record does
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
            # Offline is normal in volume on a residential PON, so it is INFO —
            # it belongs in the list the tile opens, not above a dark port.
            out.append(_row(
                "onu_offline", "info", device_id=dev_id, device_name=name,
                region=region_of.get(dev_id), subject=_onu_subject(r),
                detail=(f"{r.get('state') or 'unknown'}"
                        + (" (OLT unreachable)" if dev_id in down_ids else "")),
                since=r.get("last_online_at")))

    dists = ponfault.passive_distances(devs, store.list_link_routes(org))
    for f in ponfault.evaluate_org(live_rows, now, passive_dists=dists,
                                   witness_macs=store.onu_place_macs(org)):
        # WHERE first, cohort size last. This is the longest detail string the
        # list produces, so it is the one most likely to be cut off in a printed
        # report — and the actionable half is the bracket and the named passive
        # (that's what a splicing crew drives to), not the ONU count.
        parts = []
        if f.cut_low_m is not None and f.cut_high_m is not None:
            parts.append(f"cut {f.cut_low_m}–{f.cut_high_m} m ranging")
        if f.suspect:
            parts.append(f"near {f.suspect}")
        parts.append(f"{f.dark} of {f.onus_total} ONUs dark")
        if f.dying_gasp:
            parts.append(f"{f.dying_gasp} dying-gasp")
        # Say WHY, not just what. On a build that reports neither gasp nor LOS a
        # "fiber" verdict is this module's assumption; a reference ONU makes it a
        # finding, and the reader is deciding whether to roll a crew.
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
            continue  # dead-member dups are reg-table history, never an alarm
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
    """Every open issue in `org`, most severe first.

    `kinds` filters to a subset (unknown names ignored — a filter is a view, so a
    stale bookmark should show a wide list, never an error). Sorted by severity,
    then kind, then device: stable enough that the same fleet exports the same
    report twice."""
    now = now or datetime.now(timezone.utc)
    want = set(kinds) & set(KINDS) if kinds else set(KINDS)
    devs = store.list_org_devices(org)
    down_ids, stale_ids = olt_liveness(devs, now, cfg.central_node_stale_s)
    # ICMP staleness, in the dashboard's own terms — devices and probes are judged
    # against the same clock the tiles use so their counts match.
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
    """Per-kind totals for the filter chips, computed over the UNFILTERED list so
    a chip can say how many rows it would show before you click it."""
    out = {k: 0 for k in KINDS}
    for r in rows:
        out[r["kind"]] = out.get(r["kind"], 0) + 1
    return out
