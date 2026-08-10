"""A cable's own route: how long it is, where it is cut, and which way round it lies.

A cable is a linear asset with a surveyed route, and since 2026-08-09 it is a SEGMENT:
its two ends are fibre points, and the whole of `path` is the line between them. That
one change deleted most of what used to live here. There is no longer a stretch to
carve out of a longer route (`between`), and no lateral to stitch onto each end of it
(`span_path`) — the cable is drawn to the box because the operator drew it there.

What is left is the three things a segment still needs:

**SPLITTING.** Opening a sheath at a new closure is the commonest thing a crew does to
existing plant, and in this model it is what keeps the segment rule from being a tax:
`split` cuts the route at the tap, the caller makes two cables of it and splices every
core straight through. Nothing already recorded at either far end is disturbed. Without
this, tapping a street would mean redrawing it.

**ORIENTATION.** A cable's `path` is stored in the order somebody drew it, which has no
relationship to which end is `a`. Rather than storing a claim about that — and having
to keep it true through every retrace — `orient` answers it by measurement, choosing
the assignment that minimises the TOTAL of the two end stubs. A cable drawn backwards
therefore draws correctly, and re-tracing it cannot silently flip which pin its ends
connect to.

**LENGTH.** Crews order drum by the metre, so this is walked segment by segment with
the haversine and never as a projected chord: Mercator stretches with latitude, and a
number a crew buys cable against may not be approximate in a direction nobody can see.

Projections (`project`, `snap`) stay equirectangular, scaling longitude by cos(lat):
they run over a path a few hundred metres long, where the flat-earth error is far below
the accuracy of the pins themselves.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

#: A path needs two vertices to have a direction; one point is a place, not a route.
MIN_PATH_POINTS = 2


def _flat(lat: float, lng: float, lat0: float) -> tuple[float, float]:
    """Local planar coordinates, in degrees-of-latitude units."""
    return (lng * cos(radians(lat0)), lat)


def project(path: list, lat: float, lng: float) -> tuple[int, float]:
    """Where (lat, lng) falls on `path`, as (segment index, fraction along it).

    The segment index is that of the segment's FAR vertex, matching the SPA's
    `nearestOnPath` so the two halves of this product describe one point the
    same way.
    """
    lat0 = path[0][0]
    x, y = _flat(lat, lng, lat0)
    best, best_seg, best_t = None, 1, 0.0
    for i in range(1, len(path)):
        ax, ay = _flat(path[i - 1][0], path[i - 1][1], lat0)
        bx, by = _flat(path[i][0], path[i][1], lat0)
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        # A zero-length segment cannot be divided into — clamp to its start.
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / span)) if span > 0 else 0.0
        d = (x - (ax + dx * t)) ** 2 + (y - (ay + dy * t)) ** 2
        if best is None or d < best:
            best, best_seg, best_t = d, i, t
    return best_seg, best_t


def point_at(path: list, seg: int, t: float) -> list[float]:
    """The coordinate at (seg, t) — linear between the two vertices, which is
    what every renderer draws between them, so the point lands on the line."""
    a, b = path[seg - 1], path[seg]
    return [round(a[0] + (b[0] - a[0]) * t, 6), round(a[1] + (b[1] - a[1]) * t, 6)]


def snap(path: list, lat: float, lng: float) -> list[float] | None:
    """The point on `path` nearest (lat, lng).

    An operator clicks near a line, never on it. Snapping on the way in is what
    lets everything downstream treat a recorded point as being exactly on the
    glass, rather than each reader having to decide how near counts as on.
    """
    if not path or len(path) < MIN_PATH_POINTS:
        return None
    return point_at(path, *project(path, lat, lng))


def split(path: list, lat: float, lng: float) -> tuple[list, list] | None:
    """Cut `path` at the point nearest (lat, lng), into two complete routes.

    Both halves are COMPLETE — first vertex to last, the contract `org_cables.path`
    keeps — and both carry the cut point, because after the split that coordinate
    is a closure standing on both sheaths.

    Returns None when the cut would land on either extreme end, i.e. when one half
    would be a single point. That is not a failure to handle gracefully: splitting
    a cable at its own end produces no second cable, and the honest answer is to
    refuse rather than to write a degenerate row somebody then has to find.
    """
    if not path or len(path) < MIN_PATH_POINTS:
        return None
    seg, t = project(path, lat, lng)
    cut = point_at(path, seg, t)
    head = [[round(p[0], 6), round(p[1], 6)] for p in path[:seg]]
    if not head or head[-1] != cut:
        head.append(cut)
    tail = [[round(p[0], 6), round(p[1], 6)] for p in path[seg:]]
    if not tail or tail[0] != cut:
        tail.insert(0, cut)
    if len(head) < MIN_PATH_POINTS or len(tail) < MIN_PATH_POINTS:
        return None
    return head, tail


def orient(path: list, a: tuple | None, b: tuple | None) -> bool:
    """True when end `a` belongs to `path[0]` and end `b` to `path[-1]`.

    Measured rather than stored. A cable's vertices are in the order somebody drew
    them, which says nothing about which end the record calls `a` — and a claim
    stored about that would have to be kept true through every retrace, which is
    exactly the kind of derived-fact-with-two-homes this schema keeps refusing.

    The test is on the TOTAL of the two stubs, not on either one alone: a pin can
    easily be nearer the wrong end of the route (a street that doubles back), and
    deciding each end independently is how both stubs end up drawn to the same
    vertex with the cable crossing itself.

    An unplaced end abstains, so a cable with one pin still draws the right way
    round from the other. With neither placed the answer is arbitrary and `True`
    keeps it stable.
    """
    if not path or len(path) < MIN_PATH_POINTS:
        return True
    lat0 = path[0][0]
    head, tail = path[0], path[-1]

    def d2(p: tuple | None, q) -> float:
        if p is None:
            return 0.0
        px, py = _flat(p[0], p[1], lat0)
        qx, qy = _flat(q[0], q[1], lat0)
        return (px - qx) ** 2 + (py - qy) ** 2

    return d2(a, head) + d2(b, tail) <= d2(a, tail) + d2(b, head)


def _hav_m(a, b) -> float:
    r = 6371000.0
    d_lat = radians(b[0] - a[0])
    d_lng = radians(b[1] - a[1])
    h = (sin(d_lat / 2) ** 2
         + cos(radians(a[0])) * cos(radians(b[0])) * sin(d_lng / 2) ** 2)
    return 2 * r * asin(sqrt(h))


def length_m(path: list) -> float | None:
    """How much cable this route is, in metres, or None when it is untraced.

    Segment by segment on purpose. This is the number a crew orders drum against,
    and the difference between walking it and measuring the chord is the whole
    reason anybody traced the street.
    """
    if not path or len(path) < MIN_PATH_POINTS:
        return None
    return round(sum(_hav_m(path[i - 1], path[i]) for i in range(1, len(path))), 1)
