"""PON fault localization — pure math over onu_optics rows, no I/O.

The one genuinely differentiating read we get from an EPON/GPON OLT: every ONU
carries a ranging distance (optical path length from the OLT), and a dying ONU
tells us HOW it died — `dying_gasp` means the customer's power went out (the ONU
said goodbye on its capacitors), `los`/silent means the fiber went dark. Cross
those two facts over a whole PON and a mass drop classifies itself:

  * mostly dying-gasp  → a power outage in the neighborhood. Not a cut. Don't
    roll a splicing crew.
  * mostly LOS/silent  → a fiber event, and the cut sits BETWEEN the farthest
    still-online ONU short of the dark set and the nearest dark ONU:
    d_cut ∈ (cut_low_m, cut_high_m].

Ranging is optical path length — slack coils and drop loops inflate it by tens
of meters — so the answer is always presented as an interval, never a point.

Detection is stateless: `onu_optics.last_online_at` freezes when an ONU goes
dark, so "≥ N ONUs on one PON whose last_online_at is recent" IS the mass-drop
event, no history table needed. An OLT whose walk went stale is skipped
outright — when the OLT itself is down (or SNMP died), the outage machinery
owns the page and stale optics must not fabricate a second story.

This module never opens outages and never pages (SNMP-derived facts don't) —
callers render it, and any future heads-up alert lives with the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt

from wisp.central.inventory import PASSIVE_TYPES
from wisp.core.analytics import _parse


def _naive_utc(now: datetime) -> datetime:
    """core.analytics._parse yields NAIVE UTC — meet it there."""
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now

# `unknown` is deliberately NOT dark — a vendor decode gap must not read as an
# outage cohort. dying_gasp is dark (the ONU is gone) but classifies as power.
DARK_STATES = frozenset({"offline", "los", "dying_gasp"})

MIN_DARK = 3          # fewer than this is drops/CPE trouble, not a plant event
WINDOW_MIN = 30       # cohort = went dark within this many minutes
STALE_S = 900         # OLT walk older than this → skip the OLT entirely
SLACK_M = 80          # ranging slack: a passive this far past the interval still binds


@dataclass(frozen=True)
class PonFault:
    device_id: int
    device_name: str
    pon_port: str | None
    onus_total: int        # ONUs on this PON
    dark: int              # cohort size (recently dark)
    dying_gasp: int        # cohort members that announced a power loss
    since: str | None      # earliest cohort last_online_at
    kind: str              # "power" | "fiber"
    cut_low_m: int | None  # fiber only: cut is past this ranging distance…
    cut_high_m: int | None  # …and at or before this one
    suspect: str | None = None  # named passive whose route distance sits in the interval

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id, "device_name": self.device_name,
            "pon_port": self.pon_port, "onus_total": self.onus_total,
            "dark": self.dark, "dying_gasp": self.dying_gasp, "since": self.since,
            "kind": self.kind, "cut_low_m": self.cut_low_m,
            "cut_high_m": self.cut_high_m, "suspect": self.suspect,
        }


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return _parse(raw)
    except (ValueError, TypeError):
        return None


def _hav_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    d_lat = radians(b[0] - a[0])
    d_lng = radians(b[1] - a[1])
    h = (sin(d_lat / 2) ** 2
         + cos(radians(a[0])) * cos(radians(b[0])) * sin(d_lng / 2) ** 2)
    return 2 * r * asin(sqrt(h))


def passive_distances(devices: list[dict], routes: list[dict]) -> dict:
    """Route distance from each placed passive back to its powered head.

    Walks the parent chain (splitter → … → OLT) summing per-link geometry:
    drawn `link_routes` waypoints where the operator traced the cable, straight
    chords where not. An unplaced device anywhere on the chain aborts — a
    suspect must never be named off fabricated geometry. The passive's PON port
    inherits from the nearest ancestor when its own is blank (cascades).

    Returns {(head_device_id, pon_port): [{"id", "name", "dist_m"}, ...]}."""
    by_id = {d["id"]: d for d in devices}
    geom = {(r["child_id"], r["parent_id"]): r["waypoints"] for r in routes}
    out: dict[tuple[int, str], list[dict]] = {}
    for d in devices:
        if d.get("device_type") not in PASSIVE_TYPES:
            continue
        dist, port, cur, head = 0.0, d.get("pon_port"), d, None
        for _ in range(20):
            pid = cur.get("parent_device_id")
            parent = by_id.get(pid) if pid is not None else None
            if parent is None:
                break
            ends = (cur.get("lat"), cur.get("lng"), parent.get("lat"), parent.get("lng"))
            if any(v is None for v in ends):
                break
            pts = ([(parent["lat"], parent["lng"])]
                   + [(w[0], w[1]) for w in geom.get((cur["id"], pid), [])]
                   + [(cur["lat"], cur["lng"])])
            dist += sum(_hav_m(pts[i - 1], pts[i]) for i in range(1, len(pts)))
            if parent.get("device_type") not in PASSIVE_TYPES:
                head = parent
                break
            if port is None:
                port = parent.get("pon_port")
            cur = parent
        if head is None or port is None:
            continue
        out.setdefault((head["id"], port), []).append(
            {"id": d["id"], "name": d["name"], "dist_m": round(dist)})
    return out


def _bind_suspect(device_id: int, port: str | None, cut_low: int | None,
                  cut_high: int | None, passive_dists: dict | None) -> str | None:
    """Name the deepest passive whose route distance falls inside the cut
    interval (+ ranging slack) — 'suspect FDB-14', not 'somewhere out there'."""
    if not passive_dists or cut_high is None or port is None:
        return None
    cands = [c for c in passive_dists.get((device_id, port), [])
             if (cut_low or 0) < c["dist_m"] <= cut_high + SLACK_M]
    if not cands:
        return None
    return max(cands, key=lambda c: c["dist_m"])["name"]


def evaluate_olt(rows: list[dict], now: datetime, *,
                 min_dark: int = MIN_DARK,
                 window_min: int = WINDOW_MIN,
                 passive_dists: dict | None = None) -> list[PonFault]:
    """Faults for one OLT's ONU rows, grouped per PON port."""
    now = _naive_utc(now)
    horizon = now - timedelta(minutes=window_min)
    ports: dict[str | None, list[dict]] = {}
    for r in rows:
        ports.setdefault(r.get("pon_port"), []).append(r)

    faults: list[PonFault] = []
    for port, onus in ports.items():
        cohort = [r for r in onus
                  if r.get("state") in DARK_STATES
                  and (t := _ts(r.get("last_online_at"))) is not None
                  and t >= horizon]
        if len(cohort) < min_dark:
            continue
        gasps = sum(1 for r in cohort if r.get("state") == "dying_gasp")
        # majority dying-gasp = the neighborhood lost power, not the fiber
        kind = "power" if gasps * 2 >= len(cohort) else "fiber"

        cut_low = cut_high = None
        if kind == "fiber":
            dark_d = [r["distance_m"] for r in cohort if r.get("distance_m") is not None]
            if dark_d:
                cut_high = min(dark_d)
                survivors = [r["distance_m"] for r in onus
                             if r.get("state") == "online"
                             and r.get("distance_m") is not None
                             and r["distance_m"] < cut_high]
                cut_low = max(survivors) if survivors else 0

        since_ts = [t for r in cohort if (t := _ts(r.get("last_online_at")))]
        dev_id = cohort[0]["device_id"]
        faults.append(PonFault(
            device_id=dev_id,
            device_name=cohort[0].get("device_name") or f"#{dev_id}",
            pon_port=port, onus_total=len(onus), dark=len(cohort),
            dying_gasp=gasps,
            # +00:00 like every other server timestamp — a bare ISO string
            # parses as LOCAL time in the browser
            since=(min(since_ts).replace(tzinfo=timezone.utc).isoformat()
                   if since_ts else None),
            kind=kind, cut_low_m=cut_low, cut_high_m=cut_high,
            suspect=_bind_suspect(dev_id, port, cut_low, cut_high, passive_dists)))
    faults.sort(key=lambda f: (-f.dark, f.pon_port or ""))
    return faults


def evaluate_org(rows: list[dict], now: datetime, *,
                 min_dark: int = MIN_DARK,
                 window_min: int = WINDOW_MIN,
                 stale_s: int = STALE_S,
                 passive_dists: dict | None = None) -> list[PonFault]:
    """Faults across every OLT with a FRESH optics walk; stale OLTs are skipped
    (a down OLT freezes its rows — the ICMP outage already owns that page)."""
    now = _naive_utc(now)
    by_dev: dict[int, list[dict]] = {}
    for r in rows:
        by_dev.setdefault(r["device_id"], []).append(r)

    out: list[PonFault] = []
    for onus in by_dev.values():
        newest = max((t for r in onus if (t := _ts(r.get("updated_at")))),
                     default=None)
        if newest is None or (now - newest).total_seconds() > stale_s:
            continue
        out.extend(evaluate_olt(onus, now, min_dark=min_dark,
                                window_min=window_min,
                                passive_dists=passive_dists))
    out.sort(key=lambda f: (-f.dark, f.device_name, f.pon_port or ""))
    return out
