from __future__ import annotations

import re
from typing import Any, Hashable

FIBER_COUNTS = (1, 2, 4, 6, 8, 12, 24, 48, 96)

STRAND_COLORS: tuple[tuple[str, str], ...] = (
    ("blue",   "#0d6fd1"),
    ("orange", "#f07000"),
    ("green",  "#009a44"),
    ("brown",  "#8a5a2b"),
    ("slate",  "#8c9296"),
    ("white",  "#f5f5f2"),
    ("red",    "#e03c31"),
    ("black",  "#1c1c1e"),
    ("yellow", "#f2c100"),
    ("violet", "#8b4bb0"),
    ("rose",   "#f2a0b6"),
    ("aqua",   "#4ec3dd"),
)

TUBE_SIZE = len(STRAND_COLORS)

Point = Hashable

Fibre = tuple[int, int]

PORT_KINDS = ("pon", "leg", "in", "port")


def port_label(kind: str | None, no: int | None) -> str | None:

    if kind not in PORT_KINDS:
        return None
    if kind == "pon":
        return f"PON {no}" if no is not None else "PON"
    if kind == "leg":
        return f"leg {no}" if no is not None else "leg"
    if kind == "port":
        return f"port {no}" if no is not None else "port"
    return f"input {no}" if no and no > 1 else "input"


def port_bound(kind: str, *, split_ratio: int | None = None,
               split_inputs: int | None = None) -> int | None:


    if kind == "leg":
        return split_ratio
    if kind == "in":
        return split_inputs or 1
    return None


MAX_PON_INDEX = 64
_PON_LABEL_RE = re.compile(r"^[A-Za-z]*\s*(\d+(?:/\d+)*)\b")


def pon_index(label) -> int | None:


    s = str(label or "").strip()
    if not s or "ONU" in s.upper() or ":" in s:
        return None
    m = _PON_LABEL_RE.match(s)
    if not m:
        return None
    idx = int(m.group(1).rsplit("/", 1)[-1])
    return idx if 1 <= idx <= MAX_PON_INDEX else None


def pon_index_of_interface(name) -> int | None:


    s = str(name or "").strip()
    return pon_index(s) if "PON" in s.upper() else None


def pon_ports(roster=(), interfaces=(), recorded=()) -> list[int]:


    out = {i for i in (pon_index(x) for x in roster or ()) if i}
    out |= {i for i in (pon_index_of_interface(x) for x in interfaces or ()) if i}
    out |= {int(i) for i in recorded or () if i}
    return sorted(out)


_IF_PORT_RE = re.compile(r"(\d+)\s*$")
_VIRTUAL_IF = ("vlan", "loopback", "lo", "null", "bridge", "tunnel", "port-channel",
               "bond", "agg", "mgmt", "inloopback", "register")


def if_port_no(name) -> int | None:

    s = str(name or "").strip()
    if not s:
        return None
    low = s.lower()
    if any(v in low for v in _VIRTUAL_IF):
        return None
    m = _IF_PORT_RE.search(s)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 512 else None


def port_slots(device_type: str | None, *, split_ratio: int | None = None,
               split_inputs: int | None = None,
               pons: list[int] | None = None,
               ports: list[int] | None = None) -> list[tuple[str, int]]:


    if device_type == "splitter":
        return ([("in", n) for n in range(1, (split_inputs or 1) + 1)]
                + [("leg", n) for n in range(1, (split_ratio or 0) + 1)])
    if device_type == "OLT":
        return [("pon", n) for n in (pons or ())]
    if device_type in ENCLOSURE_TYPES:
        return []
    return [("port", n) for n in (ports or ())]


ENCLOSURE_TYPES = ("coupler", "closure", "fdb")


def port_kind_for(device_type: str | None) -> str | None:
    if device_type in ENCLOSURE_TYPES or not device_type:
        return None
    return {"splitter": "leg", "OLT": "pon"}.get(device_type, "port")


class FiberError(ValueError):
    pass


def strand_name(position: int) -> str:
    return STRAND_COLORS[(position - 1) % TUBE_SIZE][0]


def strand_hex(position: int) -> str:
    return STRAND_COLORS[(position - 1) % TUBE_SIZE][1]


def locate(core_no: int, cores: int | None = None) -> dict:


    tube = (core_no - 1) // TUBE_SIZE + 1
    within = (core_no - 1) % TUBE_SIZE + 1
    single = cores is not None and cores <= TUBE_SIZE
    return {
        "core_no": core_no,
        "fiber": within,
        "fiber_color": strand_name(within),
        "fiber_hex": strand_hex(within),
        "tube": None if single else tube,
        "tube_color": None if single else strand_name(tube),
        "tube_hex": None if single else strand_hex(tube),
    }


def describe(core_no: int, cores: int | None = None) -> str:
    loc = locate(core_no, cores)
    if loc["tube"] is None:
        return f"{loc['fiber_color']} fibre" + (f" ({core_no} of {cores})" if cores else "")
    return (f"{loc['fiber_color']} fibre in the {loc['tube_color']} tube"
            f" (core {core_no}" + (f" of {cores})" if cores else ")"))


def clean_fiber_count(raw) -> int | None:

    if raw in (None, "", "null"):
        return None
    if isinstance(raw, str):
        raw = raw.strip().lower().removesuffix("core").removesuffix("f").strip() or "0"
    try:
        cores = int(raw)
    except (TypeError, ValueError):
        raise FiberError("fibre count is invalid")
    if cores not in FIBER_COUNTS:
        raise FiberError("fibre count must be one of: "
                         + ", ".join(f"{c}F" for c in FIBER_COUNTS))
    return cores


def clean_core_no(raw, cores: int | None) -> int | None:

    if raw in (None, "", "null"):
        return None
    try:
        core = int(raw)
    except (TypeError, ValueError):
        raise FiberError("core number is invalid")
    if cores is None:
        raise FiberError("record the cable's fibre count before naming a core")
    if not (1 <= core <= cores):
        raise FiberError(f"core number must be between 1 and {cores}")
    return core


CABLE_NAME_MAX = 64


def clean_cable_name(raw, *, required: bool = True) -> str:


    name = str(raw).strip() if raw is not None else ""
    if not name and required:
        raise FiberError("cable name is required")
    if len(name) > CABLE_NAME_MAX:
        raise FiberError(f"cable name must be {CABLE_NAME_MAX} characters or fewer")
    return name


def is_plumbing(cable: dict) -> bool:


    return (not (cable.get("name") or "").strip()
            and (cable.get("cores") or 1) <= 1
            and not cable.get("path"))


def cable_ends(cable: dict) -> tuple[Point, Point]:
    return (cable["a_point"], cable["b_point"])


def other_end(cable: dict, point: Point) -> Point:
    a, b = cable_ends(cable)
    return b if point == a else a


def joint_refusal(a: Fibre, b: Fibre | None, point: Point,
                  cables: dict[int, dict], taken: dict[Fibre, Any],
                  port: tuple[str, int | None] | None = None,
                  ports: dict[tuple[str, int | None], Any] | None = None) -> str | None:


    cable_a = cables.get(a[0])
    if cable_a is None or point not in cable_ends(cable_a):
        return "absent"
    if b is not None:
        cable_b = cables.get(b[0])
        if cable_b is None or point not in cable_ends(cable_b):
            return "absent"
        if a == b:
            return "self"
        if port is not None:
            return "port_splice"
    if a in taken or (b is not None and b in taken):
        return "taken"
    if port is not None and ports and port in ports:
        return "port_taken"
    return None


JOINT_REFUSAL_TEXT = {
    "absent": "Both fibres have to end at this point — a strand can only be"
              " joined where the cable is opened.",
    "self": "A fibre cannot be joined to itself.",
    "taken": "That fibre is already joined to another one here. One fibre joins"
             " exactly one fibre.",
    "port_taken": "Another fibre already lands on that port. One port takes"
                  " exactly one fibre.",
    "port_splice": "A port belongs to a fibre taken into the box, not to a"
                   " splice between two cables.",
}


def ports_taken_at(joints: list[dict], point: Point
                   ) -> dict[tuple[str, int | None], dict]:

    out: dict[tuple[str, int | None], dict] = {}
    for j in joints:
        if j["point"] != point or j.get("b_cable_id") is not None:
            continue
        kind = j.get("port_kind")
        if kind:
            out[(kind, j.get("port_no"))] = j
    return out


def continuity(joints: list[dict]) -> dict[tuple[int, int, Point], list[Fibre]]:


    out: dict[tuple[int, int, Point], list[Fibre]] = {}
    for j in joints:
        if j.get("b_cable_id") is None:
            continue
        a: Fibre = (j["a_cable_id"], j["a_core_no"])
        b: Fibre = (j["b_cable_id"], j["b_core_no"])
        point = j["point"]
        out.setdefault((a[0], a[1], point), []).append(b)
        out.setdefault((b[0], b[1], point), []).append(a)
    return out


def terminations(joints: list[dict]) -> dict[tuple[int, int, Point], dict]:
    return {(j["a_cable_id"], j["a_core_no"], j["point"]): j
            for j in joints if j.get("b_cable_id") is None}


def taken_at(joints: list[dict], point: Point) -> dict[Fibre, dict]:
    out: dict[Fibre, dict] = {}
    for j in joints:
        if j["point"] != point:
            continue
        out[(j["a_cable_id"], j["a_core_no"])] = j
        if j.get("b_cable_id") is not None:
            out[(j["b_cable_id"], j["b_core_no"])] = j
    return out


def trace(cables: list[dict], joints: list[dict],
          cable_id: int, core_no: int) -> dict:


    by_id = {c["id"]: c for c in cables}
    start = by_id.get(cable_id)
    if start is None:
        return {"ok": False, "fault": "missing", "fault_at": None,
                "hops": [], "points": [], "ends": []}
    joins = continuity(joints)
    ends = terminations(joints)

    def walk(entered: Point) -> tuple[list[dict], str | None, Point | None]:
        hops: list[dict] = []
        used: set[Fibre] = {(cable_id, core_no)}
        fibre: Fibre = (cable_id, core_no)
        while True:
            cable = by_id[fibre[0]]
            leaving = other_end(cable, entered)
            hops.append({"cable_id": fibre[0], "core_no": fibre[1],
                         "from_point": entered, "to_point": leaving})
            nxt = [n for n in joins.get((fibre[0], fibre[1], leaving), [])
                   if n[0] in by_id]
            if not nxt:
                return hops, None, None
            if len(set(nxt)) > 1:
                return hops, "fork", leaving
            if nxt[0] in used:
                return hops, "loop", leaving
            used.add(nxt[0])
            fibre, entered = nxt[0], leaving

    a_end, b_end = cable_ends(start)
    forward, f_fault, f_at = walk(a_end)
    backward, b_fault, b_at = walk(b_end)
    hops = [{**h, "from_point": h["to_point"], "to_point": h["from_point"]}
            for h in reversed(backward[1:])] + forward
    points = ([hops[0]["from_point"]] + [h["to_point"] for h in hops]) if hops else []
    fault = b_fault or f_fault
    return {
        "ok": fault is None,
        "fault": fault,
        "fault_at": b_at if b_fault else f_at,
        "hops": hops,
        "points": points,
        "ends": [ends.get((h["cable_id"], h["core_no"], p))
                 for h, p in ((hops[0], points[0]), (hops[-1], points[-1]))]
        if hops else [],
    }


def pon_of_points(cables: list[dict], joints: list[dict]
                  ) -> dict[Point, tuple[Point, int | None] | None]:


    by_id = {c["id"]: c for c in cables}
    joins = continuity(joints)
    ends = terminations(joints)
    out: dict[Point, tuple[Point, int | None] | None] = {}

    def arrive(point: Point, source: tuple[Point, int | None]) -> None:
        if point in out and out[point] != source:
            out[point] = None
        elif point not in out:
            out[point] = source

    for (cable_id, core_no, at), j in ends.items():
        if j.get("port_kind") != "pon" or cable_id not in by_id:
            continue
        source = (at, j.get("port_no"))
        fibre: Fibre = (cable_id, core_no)
        entered = at
        used: set[Fibre] = {fibre}
        while True:
            leaving = other_end(by_id[fibre[0]], entered)
            far = ends.get((fibre[0], fibre[1], leaving))
            if far is not None:
                arrive(leaving, source)
                break
            nxt = [n for n in joins.get((fibre[0], fibre[1], leaving), [])
                   if n[0] in by_id]
            if len(set(nxt)) != 1 or nxt[0] in used:
                break
            used.add(nxt[0])
            fibre, entered = nxt[0], leaving
    return out


def feed_map(edges: list[tuple[Point, Point]],
             roots: set[Point]) -> dict[Point, Point]:


    adjacency: dict[Point, list[Point]] = {}
    for a, b in edges:
        if a == b:
            continue
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    for neighbours in adjacency.values():
        neighbours.sort()

    feed: dict[Point, Point] = {}
    seen = set(roots)
    frontier = sorted(r for r in roots if r in adjacency)
    while frontier:
        nxt: list[Point] = []
        for point in frontier:
            for other in adjacency[point]:
                if other in seen:
                    continue
                seen.add(other)
                feed[other] = point
                nxt.append(other)
        frontier = sorted(nxt)
    return feed


def connected_points(cables: list[dict],
                     through: set[Point] | None = None) -> set[frozenset]:


    hops = through or set()
    adjacency: dict[Point, set[Point]] = {}
    out: set[frozenset] = set()
    for c in cables:
        a, b = cable_ends(c)
        if a is None or b is None or a == b:
            continue
        out.add(frozenset((a, b)))
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    for start in list(adjacency):
        if start in hops:
            continue
        seen, frontier = {start}, [start]
        while frontier:
            here = frontier.pop()
            for nxt in adjacency.get(here, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                if nxt in hops:
                    frontier.append(nxt)
                else:
                    out.add(frozenset((start, nxt)))
    return out


def undrawn(declared: list[tuple[Point, Point]],
            cables: list[dict],
            through: set[Point] | None = None) -> list[tuple[Point, Point]]:


    have = connected_points(cables, through)
    out, seen = [], set()
    for a, b in declared:
        if a is None or b is None or a == b:
            continue
        pair = frozenset((a, b))
        if pair in have or pair in seen:
            continue
        seen.add(pair)
        out.append((a, b))
    return out
