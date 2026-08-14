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

# A port's identity is the box's OWN string, never a number we derived from it.
# `GigaEthernet0/5` and `TGigaEthernet0/5` are two sockets on one switch and a
# trailing digit collapses them into one — dropping the SFP+ the trunk fibre
# actually lands on. So `port_ref` is TEXT for every kind:
#   port      the walked interface name, exactly as the box reports/the operator types it
#   leg / in  the number as text, because there the number IS the fact (leg 3 of a 1:8)
#   pon       the number as text — a PON number is meaningful across the roster, the
#             ONU table and ponfault, so it stays a number and `pon_no` reads it back.
# NUMBERED_KINDS is what separates "the number is the fact" from "the string is".
NUMBERED_KINDS = ("pon", "leg", "in")

PORT_REF_MAX = 64


def port_key(ref) -> str:

    # THE ONE normalizer for port identity — case-insensitive, whitespace collapsed.
    # Refs are stored AS TYPED so a port displays the way the operator wrote it; two
    # spellings of one socket are reconciled here and nowhere else, the same discipline
    # `_norm_mac` keeps against `search_key`. Two notions of identity is how one port
    # becomes two.
    return " ".join(str(ref or "").split()).casefold()


def port_no(ref) -> int | None:

    # The number back out of a numbered kind's ref. None when it isn't one.
    try:
        return int(str(ref).strip())
    except (TypeError, ValueError):
        return None


def pon_no(kind: str | None, ref) -> int | None:
    return port_no(ref) if kind == "pon" else None


def port_label(kind: str | None, ref) -> str | None:

    if kind not in PORT_KINDS:
        return None
    text = str(ref).strip() if ref is not None else ""
    if kind == "port":
        # The interface name IS the label. "port gigabitEthernet 1/0/5" says it twice.
        return text or "port"
    if kind == "pon":
        return f"PON {text}" if text else "PON"
    if kind == "leg":
        return f"leg {text}" if text else "leg"
    return f"input {text}" if text and text != "1" else "input"


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


def pon_names(roster=(), interfaces=()) -> dict[str, str]:

    # WHAT THE BOX ITSELF CALLS EACH PON, keyed by the ref (the index, as text).
    #
    # A PON's REF stays the INDEX and nothing here changes that: it is what joins to
    # the roster's `pon_port`, what `pon_of_points` inherits down the plant chain, and
    # the one form C-Data's `EPON0/3` and the Syrotech build's bare `3` both reduce
    # to. But the index is OUR arithmetic and nobody has ever seen it — SRPL-OLT's
    # Ports tab, its Optical tab and the silkscreen on the box all say `EPON0/1`. So
    # a picker offering `PON 1` beside a `GE0/1` read straight off the walk names the
    # same eight sockets two ways in one menu, which is how it was reported: "I was
    # trying to correct SRPL-OLT port GE0/1 but in the closure the ports shown are
    # different." Half that menu was in the operator's vocabulary and half in ours.
    #
    # A source string that is ONLY the number is NOT a name — the Syrotech roster
    # writes a bare `3`, and there `PON 3` is the better label, so it is left alone.
    # The WALK wins over the roster: they almost always carry the same string, and
    # where they differ the interface name is the box naming its own socket.
    out: dict[str, str] = {}
    for source, parse in ((interfaces, pon_index_of_interface), (roster, pon_index)):
        for raw in source or ():
            text = str(raw or "").strip()
            idx = parse(text)
            if idx is None or text == str(idx):
                continue
            out.setdefault(str(idx), text)
    return out


def port_display(kind: str | None, ref, names: dict[str, str] | None = None
                 ) -> str | None:

    # THE name a port is printed under, everywhere it is printed. `port_label` stays
    # the CANONICAL form and remains the fallback: for the four OLTs on this fleet
    # that walk nothing, for a port somebody named by hand on an SNMP-silent box, and
    # for every kind but `pon` — a `port`'s ref is already the box's own string, and a
    # leg or an input is plastic nothing walks.
    if kind == "pon" and names:
        name = names.get(str(ref).strip() if ref is not None else "")
        if name:
            return name
    return port_label(kind, ref)


# Matched against the FIRST TOKEN of the interface name, as a family PREFIX — never as
# a substring of the whole string. An interface is `<family><slot>` optionally followed
# by the operator's description, and a bare-substring test refused real sockets for
# what their description said: `LAN-1 HILLCOLONY` and `LAN-2 PYLON COLONY` were both
# dropped because "co-LO-ny" contains "lo".
_VIRTUAL_IF = ("vlan", "loopback", "inloopback", "null", "bridge", "tunnel",
               "port-channel", "bond", "agg", "mgmt", "mng", "register", "default",
               "console", "stack")
_LOOPBACK_RE = re.compile(r"^lo\d*$")


def if_port_ref(name) -> str | None:

    # A physical interface's ref is its own name. Three things are refused, and each
    # is a way this record has rendered a lie before:
    #   VIRTUAL   a fibre cannot land on a VLAN — reading interfaces permissively is
    #             how `VLAN10` became somewhere to terminate a subscriber.
    #   A PON     it is already offered as `pon`, and one socket may not appear twice
    #             under two kinds.
    #   AN ONU    `EPON01ONU3` / `EPON0/1:3` are per-subscriber pseudo-interfaces, not
    #             sockets on the box. HLY-OLT-1 walks 200 interfaces of which 176 are
    #             these; offering them would bury the 16 real ports.
    s = str(name or "").strip()
    if not s or len(s) > PORT_REF_MAX:
        return None
    low = s.casefold()
    head = low.split()[0] if low.split() else low
    if head.startswith(_VIRTUAL_IF) or _LOOPBACK_RE.match(head):
        return None
    if "onu" in low or ":" in s:
        return None
    if pon_index_of_interface(s) is not None:
        return None
    return s


def port_slots(device_type: str | None, *, split_ratio: int | None = None,
               split_inputs: int | None = None,
               pons: list[int] | None = None,
               ports: list[str] | None = None) -> list[tuple[str, str]]:


    if device_type == "splitter":
        return ([("in", str(n)) for n in range(1, (split_inputs or 1) + 1)]
                + [("leg", str(n)) for n in range(1, (split_ratio or 0) + 1)])
    if device_type in ENCLOSURE_TYPES or not device_type:
        return []
    # PONs first where a box has both: it is what an OLT is mostly asked about, and
    # the uplink is one row among sixteen.
    kinds = port_kinds_for(device_type)
    out: list[tuple[str, str]] = []
    if "pon" in kinds:
        out += [("pon", str(n)) for n in (pons or ())]
    if "port" in kinds:
        out += [("port", r) for r in (ports or ())]
    return out


def interface_refs(interfaces=(), recorded=()) -> list[str]:

    # The walked names UNION whatever has been recorded by hand, deduped through the
    # ONE normalizer, walked spelling winning on a tie because the box's own string is
    # the authority. This is what bounds the free-text entry an SNMP-silent box needs:
    # drift is possible only on the FIRST entry for a port, and it self-corrects the
    # moment that port is in the list for somebody to pick.
    refs: dict[str, str] = {}
    for name in interfaces or ():
        ref = if_port_ref(name)
        if ref is not None:
            refs.setdefault(port_key(ref), ref)
    for ref in recorded or ():
        text = str(ref or "").strip()
        if text:
            refs.setdefault(port_key(text), text)
    return [refs[k] for k in sorted(refs)]


def port_live(kind: str, ref: str, walked: dict[str, str | None]) -> bool | None:

    # IS THERE LIGHT ON THIS PORT? `True` up, `False` down, `None` NOT MEASURED — and
    # the third is the one that matters. A splitter leg and an input are passive
    # plastic: nothing measures them, and painting one green would be a claim no
    # walk supports. A port on a box whose walk is stale or whose device is down is
    # `None` too — the caller blanks it there, because an unreachable box's stored
    # "up" is the same frozen reading the panels already refuse to render live.
    if kind not in ("port", "pon"):
        return None
    state = walked.get(port_key(ref))
    if state is None:
        return None
    return str(state).lower() == "up"


def walked_states(rows) -> dict[str, str | None]:

    # if_name -> oper_status, keyed by the SAME normalizer a port's identity uses, so
    # the status a port shows and the port a fibre lands on cannot come apart. A PON
    # is keyed by its index as text, which is what its ref is.
    out: dict[str, str | None] = {}
    for name, state in rows or ():
        pon = pon_index_of_interface(name)
        if pon is not None:
            out.setdefault(port_key(str(pon)), state)
            continue
        ref = if_port_ref(name)
        if ref is not None:
            out.setdefault(port_key(ref), state)
    return out


def slots_for(device_type: str | None, *, split_ratio: int | None = None,
              split_inputs: int | None = None, roster=(), interfaces=(),
              recorded: list[tuple[str, str]] | None = None
              ) -> list[tuple[str, str]]:

    # THE one derivation of "what ports does this box have", from the facts about that
    # box. `org_device_ports` (every box, for the pickers) and `point_fibre` (the box
    # you are standing at) both read it, so a port the panel offers and a port a picker
    # offers can never disagree.
    rec = recorded or []
    return port_slots(
        device_type, split_ratio=split_ratio, split_inputs=split_inputs,
        pons=pon_ports(roster=roster, interfaces=interfaces,
                       recorded=[port_no(r) for k, r in rec if k == "pon"]),
        ports=interface_refs(interfaces, [r for k, r in rec if k == "port"]))


ENCLOSURE_TYPES = ("coupler", "closure", "fdb")


def port_kinds_for(device_type: str | None) -> tuple[str, ...]:

    # A BOX HAS KINDS, PLURAL. An OLT has PONs AND the GE uplink the trunk lands on —
    # "GE0/5 INPUT" is walked, in the database, and used to be unnameable, so the only
    # ways to record an OLT's uplink were to lie (call it PON 9) or leave it blank.
    # `port` already means "a numbered interface this box walks" for every switch,
    # router and CPE, so reusing it costs no new vocabulary and no new bound rule.
    #
    # NOT an `uplink` kind: uplink is a ROLE, not a socket. Which GE port is the uplink
    # is decided by what somebody plugged in, changes on a re-patch, and nothing here
    # knows it — the first customer feed on GE0/8 would make the word a lie.
    if device_type in ENCLOSURE_TYPES or not device_type:
        return ()
    if device_type == "splitter":
        return ("leg",)
    if device_type == "OLT":
        return ("pon", "port")
    return ("port",)


def port_kind_for(device_type: str | None) -> str | None:
    # The FIRST kind — what a box is mostly asked about. Callers deciding what to
    # OFFER must use `port_kinds_for`; this stays for the single-answer callers.
    kinds = port_kinds_for(device_type)
    return kinds[0] if kinds else None


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
                  port: tuple[str, str | None] | None = None,
                  ports: dict[tuple[str, str], Any] | None = None) -> str | None:


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
    if port is not None and ports and port_slot(*port) in ports:
        return "port_taken"
    return None


def port_slot(kind: str | None, ref) -> tuple[str, str]:
    # The identity a port is held under: its kind plus the ONE normalized ref.
    return (str(kind or ""), port_key(ref))


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
                   ) -> dict[tuple[str, str], dict]:

    out: dict[tuple[str, str], dict] = {}
    for j in joints:
        if j["point"] != point or j.get("b_cable_id") is not None:
            continue
        kind = j.get("port_kind")
        if kind:
            out[port_slot(kind, j.get("port_ref"))] = j
    return out


def joint_survives(joint: dict, cables: dict[int, dict]) -> bool:

    # A JOINT SURVIVES IF BOTH ITS FIBRES STILL MEET AT A COMMON POINT.
    #
    # That is the whole rule for moving a cable end, and it is strictly more correct
    # than discarding every joint at an end that moved: merging two closures is
    # "move both ends", so the blanket rule destroyed exactly the splices the operator
    # was trying to keep. A joint to a THIRD cable left behind at the old point still
    # correctly dies — it is now a splice between fibres that no longer meet, which is
    # the one thing this record may not hold.
    point = joint["point"]
    a = cables.get(joint["a_cable_id"])
    if a is None or point not in cable_ends(a):
        return False
    if joint.get("b_cable_id") is None:
        return True
    b = cables.get(joint["b_cable_id"])
    return b is not None and point in cable_ends(b)


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


def carried_by(cables: list[dict], joints: list[dict], cable_id: int,
               cores: int) -> dict[int, list[dict]]:

    # WHAT EACH CORE CARRIES: the terminations a fibre reaches, following it across
    # every splice in both directions. A closure's schedule used to show core 1 with
    # nothing on it even where the fibre ran to a switch two closures away — the walk
    # already knew, it was simply never asked. Only cores that REACH something get an
    # entry: a core joined to nothing carries nothing, and saying so with a blank row
    # is the difference between "unrecorded" and "recorded as empty".
    out: dict[int, list[dict]] = {}
    for core in range(1, (cores or 0) + 1):
        result = trace(cables, joints, cable_id, core)
        if len(result["hops"]) < 2:
            # The fibre never leaves this cable. Its two ends are the cable's own
            # ends, which the panel already prints — reporting them as what the core
            # "carries" would turn "nobody has recorded this" into a finding.
            continue
        rows = []
        for e, p in zip(result["ends"], (result["points"][0],
                                         result["points"][-1])):
            if e:
                rows.append({"point": e["point"], "port_kind": e.get("port_kind"),
                             "port_ref": e.get("port_ref")})
            else:
                # It runs on and stops at a point with no equipment on it. WHERE it
                # gets to is worth saying; claiming it lands on something is not.
                rows.append({"point": p, "port_kind": None, "port_ref": None})
        out[core] = rows
    return out


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
        source = (at, pon_no("pon", j.get("port_ref")))
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
             roots: set[Point],
             rank: dict[Point, int] | None = None) -> dict[Point, Point]:

    # WHICH WAY THE LIGHT FLOWS. A flood from every gear point at once gets a
    # mid-span closure BACKWARDS: on the live fleet JC-3 sat two hops from SRPL-OLT
    # and three from the switch that actually feeds it, so its chain read "fed from
    # SRPL-OLT" — an OLT feeding its own uplink. `rank` is each root's declared
    # tree depth: shallower gear floods FIRST and the wave passes THROUGH deeper
    # gear (an OLT hands light onward to its splitters), so a deeper root seeds
    # only a component no shallower root reaches — which keeps badri_fiber right,
    # where OLT uplinks are not in the glass and each OLT must still source its own
    # island. With no rank every root is level 0 and this is the plain nearest-gear
    # flood it always was.
    adjacency: dict[Point, list[Point]] = {}
    for a, b in edges:
        if a == b:
            continue
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    for neighbours in adjacency.values():
        neighbours.sort()

    levels: dict[int, list[Point]] = {}
    for r in roots:
        if r in adjacency:
            levels.setdefault((rank or {}).get(r, 0), []).append(r)

    feed: dict[Point, Point] = {}
    seen: set[Point] = set()
    for level in sorted(levels):
        frontier = sorted(r for r in levels[level] if r not in seen)
        seen.update(frontier)
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


def connected_spans(cables: list[dict],
                    through: set[Point] | None = None) -> dict[frozenset, dict]:


    # WHICH PAIRS THE GLASS JOINS, and for each, THE SHEATH WORTH POINTING AT.
    # The pairs half is what `undrawn` reads (a connection recorded through a closure
    # IS recorded — a closure is where a sheath is opened, not a place the light
    # stops). The cable half is what lets the MAP stand a dependency chord down and
    # move its rate chip onto the plant: a line has to be named before it can carry a
    # label, and the run `HALIYA-WAN-SW → JC1 → main → JC2 → SRPL-OLT` is three
    # cables, only one of which anybody would point at.
    #
    # THE BIGGEST SHEATH WINS, measured in CORES. The 6F trunk between two closures is
    # the object; the 1F tails either side are our own plumbing, 26 m long, and a rate
    # chip on one would be unreadable at any zoom you would read a trunk at. Ties
    # break on the lower id so the choice is stable across reloads rather than
    # following whatever order the walk happened to take.
    # THE WHOLE RUN comes back, not only the sheath worth labelling, because the map
    # DRAWS what it stands a chord down for: `main` alone would leave the two 1F tails
    # unlit and the line would stop dead at a closure. `label` is the biggest; `path`
    # is every cable the light crosses between the pair.
    hops = through or set()
    cores: dict[int, int] = {}
    adjacency: dict[Point, list[tuple[Point, int]]] = {}
    out: dict[frozenset, dict] = {}

    def bigger(a: int | None, b: int | None) -> int | None:
        if a is None or b is None:
            return a if b is None else b
        if cores.get(a, 0) != cores.get(b, 0):
            return a if cores.get(a, 0) > cores.get(b, 0) else b
        return min(a, b)

    def offer(key, label: int | None, path: tuple) -> None:
        cur = out.get(key)
        if cur is None:
            out[key] = {"label": label, "path": list(path)}
            return
        # Parallel runs between one pair: keep the one whose biggest sheath is biggest,
        # and keep THAT run's cables — a path spliced from two answers is not a path.
        if bigger(cur["label"], label) is label and label != cur["label"]:
            out[key] = {"label": label, "path": list(path)}

    for c in cables:
        a, b = cable_ends(c)
        if a is None or b is None or a == b:
            continue
        # `id` is read with a default because the PAIRS half of this answer must not
        # depend on the labelling half: a caller that only asks "are these joined?"
        # (`connected_points`, and so `undrawn`) may hand over cables carrying nothing
        # but their two ends, and gets `None` for the sheath rather than a KeyError.
        cid = c.get("id")
        if cid is not None:
            cores[cid] = c.get("cores") or 0
        adjacency.setdefault(a, []).append((b, cid))
        adjacency.setdefault(b, []).append((a, cid))
        offer(frozenset((a, b)), cid, () if cid is None else (cid,))
    for start in list(adjacency):
        if start in hops:
            continue
        seen, frontier = {start}, [(start, None, ())]
        while frontier:
            here, carried, walked = frontier.pop()
            for nxt, cable_id in adjacency.get(here, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                best = bigger(carried, cable_id)
                run = walked if cable_id is None else (*walked, cable_id)
                if nxt in hops:
                    frontier.append((nxt, best, run))
                else:
                    offer(frozenset((start, nxt)), best, run)
    return out


def connected_points(cables: list[dict],
                     through: set[Point] | None = None) -> set[frozenset]:
    return set(connected_spans(cables, through))


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
