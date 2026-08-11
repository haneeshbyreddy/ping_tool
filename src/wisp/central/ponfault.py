from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt

from wisp.central.inventory import PASSIVE_TYPES
from wisp.central.onuroster import _norm_mac
from wisp.core.analytics import _parse


def _naive_utc(now: datetime) -> datetime:
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now

DARK_STATES = frozenset({"offline", "los", "dying_gasp"})

MIN_DARK = 3
WINDOW_MIN = 30
STALE_S = 900
SLACK_M = 80

EVIDENCE = ("witness", "dying_gasp", "silence")


@dataclass(frozen=True)
class PonFault:
    device_id: int
    device_name: str
    pon_port: str | None
    onus_total: int
    dark: int
    dying_gasp: int
    since: str | None
    kind: str
    cut_low_m: int | None
    cut_high_m: int | None
    suspect: str | None = None
    evidence: str = "silence"
    witness_dark: int = 0
    witness_alive: int = 0

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id, "device_name": self.device_name,
            "pon_port": self.pon_port, "onus_total": self.onus_total,
            "dark": self.dark, "dying_gasp": self.dying_gasp, "since": self.since,
            "kind": self.kind, "cut_low_m": self.cut_low_m,
            "cut_high_m": self.cut_high_m, "suspect": self.suspect,
            "evidence": self.evidence, "witness_dark": self.witness_dark,
            "witness_alive": self.witness_alive,
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
    if not passive_dists or cut_high is None or port is None:
        return None
    cands = [c for c in passive_dists.get((device_id, port), [])
             if (cut_low or 0) < c["dist_m"] <= cut_high + SLACK_M]
    if not cands:
        return None
    return max(cands, key=lambda c: c["dist_m"])["name"]


def _reaches_past(alive: list[dict], cohort: list[dict]) -> bool:


    dark_d = [r["distance_m"] for r in cohort if r.get("distance_m") is not None]
    alive_d = [r["distance_m"] for r in alive if r.get("distance_m") is not None]
    if not dark_d or not alive_d:
        return bool(alive)
    return max(alive_d) >= min(dark_d)


def _witness_verdict(onus: list[dict], cohort: list[dict],
                     witness_macs: set[str]) -> tuple[str | None, int, int]:

    if not witness_macs:
        return None, 0, 0
    witnesses = [r for r in onus if _norm_mac(r.get("serial")) in witness_macs]
    if not witnesses:
        return None, 0, 0
    in_cohort = {r.get("onu_key") for r in cohort}
    dark = [w for w in witnesses
            if w.get("onu_key") in in_cohort and w.get("state") != "dying_gasp"]
    alive = [w for w in witnesses if w.get("state") == "online"]
    if dark:
        return "fiber", len(dark), len(alive)
    if alive and _reaches_past(alive, cohort):
        return "power", 0, len(alive)
    return None, 0, len(alive)


def evaluate_olt(rows: list[dict], now: datetime, *,
                 min_dark: int = MIN_DARK,
                 window_min: int = WINDOW_MIN,
                 passive_dists: dict | None = None,
                 witness_macs: set[str] | None = None) -> list[PonFault]:
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
        kind = "power" if gasps * 2 >= len(cohort) else "fiber"
        evidence = "dying_gasp" if gasps else "silence"

        w_kind, w_dark, w_alive = _witness_verdict(onus, cohort, witness_macs or set())
        if w_kind is not None:
            kind, evidence = w_kind, "witness"

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
            since=(min(since_ts).replace(tzinfo=timezone.utc).isoformat()
                   if since_ts else None),
            kind=kind, cut_low_m=cut_low, cut_high_m=cut_high,
            suspect=_bind_suspect(dev_id, port, cut_low, cut_high, passive_dists),
            evidence=evidence, witness_dark=w_dark, witness_alive=w_alive))
    faults.sort(key=lambda f: (-f.dark, f.pon_port or ""))
    return faults


def evaluate_org(rows: list[dict], now: datetime, *,
                 min_dark: int = MIN_DARK,
                 window_min: int = WINDOW_MIN,
                 stale_s: int = STALE_S,
                 passive_dists: dict | None = None,
                 witness_macs: set[str] | None = None) -> list[PonFault]:
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
                                passive_dists=passive_dists,
                                witness_macs=witness_macs))
    out.sort(key=lambda f: (-f.dark, f.device_name, f.pon_port or ""))
    return out
