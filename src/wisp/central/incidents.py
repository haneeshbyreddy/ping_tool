"""Incident shape classification — power outage vs upstream failure, pure math.

Central holds two graphs nobody else has together: the TOPOLOGY (who feeds
whom) and the GEOGRAPHY (map pins). Crossing them classifies a simultaneous
multi-device outage the way a veteran NOC eye does:

  * one topological root down, children dark behind it  → "upstream" — the FSM
    already suppresses the victims; the story is that one device/fiber.
  * SEVERAL independent branches down at once, packed into a small geographic
    circle → "power" — different feeds don't share fiber, but they do share an
    electricity-board feeder. Don't roll a splicing crew at 2am for the DISCOM.

This is an ANNOTATION, never a mute: it explains alarms on the dashboard and
must not suppress, reroute, or replace any page ("trust the alarm" is
absolute). Pure function over device rows — no I/O, injectable clock, unit
tested with synthetic fleets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt

from wisp.core.analytics import _parse

DOWN_STATES = frozenset({"DOWN", "UNREACHABLE"})

WINDOW_MIN = 15      # devices falling within this of each other = one wave
RADIUS_KM = 3.0      # power verdict needs the wave packed inside this circle
MIN_DEVICES = 3      # fewer is device trouble, not an area event


@dataclass(frozen=True)
class Incident:
    kind: str                 # "power" | "upstream"
    device_ids: tuple[int, ...]
    branches: int             # independent down roots in the wave
    since: str | None         # earliest outage start in the wave (+00:00)
    center: tuple[float, float] | None   # placed members' centroid
    radius_km: float | None   # max member distance from the centroid
    root_name: str | None     # upstream only: the device that owns the story

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "device_ids": list(self.device_ids),
            "count": len(self.device_ids), "branches": self.branches,
            "since": self.since,
            "center": list(self.center) if self.center else None,
            "radius_km": self.radius_km, "root_name": self.root_name,
        }


def _km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    r = 6371.0
    d_lat = radians(b_lat - a_lat)
    d_lng = radians(b_lng - a_lng)
    h = (sin(d_lat / 2) ** 2
         + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(d_lng / 2) ** 2)
    return 2 * r * asin(sqrt(h))


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return _parse(raw)
    except (ValueError, TypeError):
        return None


def evaluate(rows: list[dict], now: datetime, *,
             window_min: int = WINDOW_MIN,
             radius_km: float = RADIUS_KM,
             min_devices: int = MIN_DEVICES) -> list[Incident]:
    """Classify the open outage waves in one org's device rows.

    `rows` are `list_org_devices()` dicts — state, outage_started_at,
    parent_device_id, lat/lng. Returns newest wave first; waves that are
    neither clearly upstream nor clearly power are omitted (no verdict beats a
    wrong one on a NOC wall)."""
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)

    downs = []
    for r in rows:
        if r.get("state") not in DOWN_STATES:
            continue
        started = _ts(r.get("outage_started_at"))
        if started is None or started > now:
            continue
        downs.append((started, r))
    if len(downs) < min_devices:
        return []
    downs.sort(key=lambda p: p[0])
    all_down_ids = {r["id"] for _, r in downs}

    # waves: consecutive outage starts separated by more than the window split
    waves: list[list[tuple[datetime, dict]]] = [[downs[0]]]
    for pair in downs[1:]:
        if (pair[0] - waves[-1][-1][0]) > timedelta(minutes=window_min):
            waves.append([pair])
        else:
            waves[-1].append(pair)

    incidents: list[Incident] = []
    for wave in reversed(waves):            # newest first
        if len(wave) < min_devices:
            continue
        members = [r for _, r in wave]
        # a root is a down device whose feed is NOT down — count independent
        # branches against the full down set, not just this wave (an
        # UNREACHABLE child of an older outage is still a victim, not a root)
        roots = [r for r in members
                 if r.get("parent_device_id") not in all_down_ids]
        branches = max(1, len(roots))
        since = wave[0][0].replace(tzinfo=timezone.utc).isoformat()

        placed = [r for r in members
                  if r.get("lat") is not None and r.get("lng") is not None]
        center = radius = None
        if placed:
            center = (sum(r["lat"] for r in placed) / len(placed),
                      sum(r["lng"] for r in placed) / len(placed))
            radius = max(_km(center[0], center[1], r["lat"], r["lng"])
                         for r in placed)

        if branches == 1:
            root = roots[0] if roots else members[0]
            incidents.append(Incident(
                kind="upstream", device_ids=tuple(r["id"] for r in members),
                branches=1, since=since, center=center,
                radius_km=round(radius, 2) if radius is not None else None,
                root_name=root.get("name")))
        elif (branches >= 2 and len(placed) >= min_devices
              and radius is not None and radius <= radius_km):
            incidents.append(Incident(
                kind="power", device_ids=tuple(r["id"] for r in members),
                branches=branches, since=since, center=center,
                radius_km=round(radius, 2), root_name=None))
        # scattered multi-branch waves get no verdict — stay silent
    return incidents
