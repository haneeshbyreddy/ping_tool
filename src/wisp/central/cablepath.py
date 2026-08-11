from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

MIN_PATH_POINTS = 2


def _flat(lat: float, lng: float, lat0: float) -> tuple[float, float]:
    return (lng * cos(radians(lat0)), lat)


def project(path: list, lat: float, lng: float) -> tuple[int, float]:

    lat0 = path[0][0]
    x, y = _flat(lat, lng, lat0)
    best, best_seg, best_t = None, 1, 0.0
    for i in range(1, len(path)):
        ax, ay = _flat(path[i - 1][0], path[i - 1][1], lat0)
        bx, by = _flat(path[i][0], path[i][1], lat0)
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / span)) if span > 0 else 0.0
        d = (x - (ax + dx * t)) ** 2 + (y - (ay + dy * t)) ** 2
        if best is None or d < best:
            best, best_seg, best_t = d, i, t
    return best_seg, best_t


def point_at(path: list, seg: int, t: float) -> list[float]:
    a, b = path[seg - 1], path[seg]
    return [round(a[0] + (b[0] - a[0]) * t, 6), round(a[1] + (b[1] - a[1]) * t, 6)]


def snap(path: list, lat: float, lng: float) -> list[float] | None:

    if not path or len(path) < MIN_PATH_POINTS:
        return None
    return point_at(path, *project(path, lat, lng))


def split(path: list, lat: float, lng: float) -> tuple[list, list] | None:


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

    if not path or len(path) < MIN_PATH_POINTS:
        return None
    return round(sum(_hav_m(path[i - 1], path[i]) for i in range(1, len(path))), 1)
