"""The fibre plant: cables between couplers, and what each core is joined to.

**A CABLE IS A SEGMENT BETWEEN TWO FIBRE POINTS**, and that is the correction this
module was given on 2026-08-09, by the ISPs themselves. They described their own plant
in one sentence — *fibre runs between two couplers, and at a coupler you join cable to
cable, or take a core out to a device on a single fibre* — plus two facts that follow
from it: **any core may carry anything, including a customer line**, and therefore **a
customer point is a coupler too** (which is what makes a lane of daisy-chained houses
possible: core 1 into this one, cores 2-4 onward to the next three).

The model that came before could not say any of it. A `run` was *(cable, core, device
A, device B)*, so glass could only be recorded between two BOXES; the closure where the
sheath is actually opened was a derived projection (`org_cable_taps`); and a cable had
no ends of its own. Three tables and two geometry contracts were spent reconstructing a
graph that was never stored. The operators agreed to abandon the recorded cable and lay
it again rather than carry both models, which is what made replacing it possible.

**A FIBRE POINT is anywhere glass lands or is joined** — a coupler, an FDB, a closure,
a splitter, an OLT, or a customer. Deliberately NOT a table: passive plant already
lives in `org_devices` and subscribers already live in `onu_places`, and a third
registry of places would be the thing this codebase refuses everywhere else. A point is
just an opaque hashable key here; the store makes them (`("device", id)` /
`("onu", mac)`) and this module never looks inside one. That is what keeps the walks
below pure and testable against plain tuples.

Three things fall out, and every one of them is a deletion:

**A RUN NO LONGER EXISTS.** A cable has two ends, so core N of it runs end to end BY
DEFINITION. There is nothing left to record and nothing to disagree about.

**THE DOUBLE BOOKING IS UNREPRESENTABLE.** `core_path` existed to catch two unrelated
runs both written down as core 7 of one cable — the error that sends a splicer to cut a
live customer. A core of a segment has exactly two ends and cannot be two disconnected
runs, so the whole split/fork/loop checker is gone rather than merely passing.

**THE IMPLICIT-CONTINUITY RULE IS GONE WITH IT.** "Two sections of one cable on one
core meeting at a box are continuous by definition" was the load-bearing sentence of
the old model, and the reason a splice restating it had to be refused. Two sections of
one cable can no longer meet: opening a sheath mid-span SPLITS it into two cables and
splices every core straight through, which is what the crew physically does. One fact,
one home, and now the home is a row somebody can see.

What survives untouched is the standard itself — `FIBER_COUNTS`, the TIA-598-D
sequence, and the tube arithmetic — because that is about glass, not about our model of
it. Mirrored in `web/src/lib/fiber.ts` (the SPA has to render swatches without asking
central, and central has to validate without asking a browser); `unit/test_fiber.py`
reads the TS source and fails if the two drift, the same way the theme allowlist and
the map-detail defaults are pinned.

Standing is unchanged and is what makes this whole surface safe to hand to an operator
mid-survey: a cable is not a device, has no state, no FSM and no outage, is absent from
`org_device_topology`, and is read by no alerting shell. Recording fibre can never
re-page a fleet.
"""
from __future__ import annotations

from typing import Any, Hashable

# Cable sizes an access network is actually built from. CLOSED, like every other
# vocabulary here (SPLIT_RATIOS, PALETTE, DEVICE_TYPES): the count feeds strand
# validation and the tube arithmetic, so a free-form "17F" would produce a fibre
# position that exists in no cable anyone can buy. Widening is a one-line edit
# plus the same line in fiber.ts.
#
# **1 ARRIVED 2026-08-09 AND IT IS NOT A ROUNDING OF "SMALL".** A single fibre out
# of a closure into an OLT's PON port is the commonest tail in this plant, and
# leaving it out made that connection UNRECORDABLE: the operator could not lay the
# tail (no such count), and could not terminate the trunk core at the OLT either,
# because a strand may only be joined where its own cable is opened — correct
# physics, and between the two there was no way to say the thing at all.
FIBER_COUNTS = (1, 2, 4, 6, 8, 12, 24, 48, 96)

# TIA-598-D, the twelve-colour sequence. Order IS the standard — index 0 is fibre
# 1 — so this tuple must never be sorted, deduped or "tidied". The hexes are the
# conventional renderings of the jacket colours, chosen to stay recognisable on
# both themes' card surfaces; WHITE and BLACK are the two that need a ring drawn
# around them rather than a lighter/darker hex, or a white strand disappears on
# light mode and a black one on dark. That ring lives in CSS, not here.
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

#: how many fibres share one buffer tube — the whole reason the sequence repeats
TUBE_SIZE = len(STRAND_COLORS)

#: A fibre point: opaque here on purpose. See the module docstring.
Point = Hashable

#: One end of one strand — the thing a joint actually joins.
Fibre = tuple[int, int]


class FiberError(ValueError):
    pass


def strand_name(position: int) -> str:
    """The standard colour name for a 1-based position WITHIN a tube (1..12)."""
    return STRAND_COLORS[(position - 1) % TUBE_SIZE][0]


def strand_hex(position: int) -> str:
    return STRAND_COLORS[(position - 1) % TUBE_SIZE][1]


def locate(core_no: int, cores: int | None = None) -> dict:
    """Where strand `core_no` physically is, as a crew would be told to find it.

    Returns the tube (1-based, with its own colour from the same sequence) and the
    fibre's position inside that tube. A cable of 12 or fewer has ONE tube, so the
    tube half is reported as None rather than as "tube 1" — saying "the blue tube"
    to somebody holding a cable that has no tubes to choose between is noise, and
    the rule this codebase keeps everywhere is that an absent fact and a present
    one must not render alike.

    `cores` is advisory: it decides only whether tubes are worth naming. A strand
    number past the cable's own count is not repaired here — `clean_core_no` is
    where that is refused, on the write path, once.
    """
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
    """One line a human can act on: "red (7 of 12)" / "blue fibre, green tube"."""
    loc = locate(core_no, cores)
    if loc["tube"] is None:
        return f"{loc['fiber_color']} fibre" + (f" ({core_no} of {cores})" if cores else "")
    return (f"{loc['fiber_color']} fibre in the {loc['tube_color']} tube"
            f" (core {core_no}" + (f" of {cores})" if cores else ")"))


def clean_fiber_count(raw) -> int | None:
    """The cable's fibre count, or None when nobody has recorded one.

    Absent is a first-class answer: most cable on a fresh install has never been
    surveyed, and a guessed count would be arithmetic nobody can act on — the same
    refusal `_split_ratio` makes about a splitter nobody has measured.
    """
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, str):
        # tolerate the way it is written on a drum tag: "12F", "12 F", "12core"
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
    """Which strand this is, bounded BY THE CABLE it runs in.

    Refusing core 30 of a 12F is the point: a strand number that exists in no
    cable would print a tube and a colour with full confidence, and somebody would
    open a closure looking for it. The count is therefore required first — a
    strand with no cable to be a strand OF is not a fact, it is half of one.
    """
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


def clean_cable_name(raw) -> str:
    """A cable's name. Required — a sheath nobody can refer to is not an object.

    The name is how a cable is spoken about ("the Haliya trunk"), so unlike every
    other field here it has no honest absent state. Everything else about it
    degrades to unrecorded.

    Splitting a cable mid-span deliberately gives BOTH halves the same name: it is
    one drum, and the two segments are told apart by their ends, which is how a
    crew talks about them.
    """
    name = str(raw).strip() if raw is not None else ""
    if not name:
        raise FiberError("cable name is required")
    if len(name) > CABLE_NAME_MAX:
        raise FiberError(f"cable name must be {CABLE_NAME_MAX} characters or fewer")
    return name


def cable_ends(cable: dict) -> tuple[Point, Point]:
    return (cable["a_point"], cable["b_point"])


def other_end(cable: dict, point: Point) -> Point:
    """The far end of `cable` from `point`. Callers have already checked it lands."""
    a, b = cable_ends(cable)
    return b if point == a else a


def joint_refusal(a: Fibre, b: Fibre | None, point: Point,
                  cables: dict[int, dict], taken: dict[Fibre, Any]) -> str | None:
    """Why these fibres may NOT be joined at this point, or None if they may.

    `b` is None for a TERMINATION — core X of cable A taken out to the equipment
    standing at this point. That is the same kind of statement as a splice (this
    fibre ends here, in this way) and it consumes the fibre end identically, which
    is why one table and one refusal cover both.

    Everything refused is physically impossible rather than merely odd:

      * `absent` — a fibre can only be joined where it is OPEN, so the point has
        to be one of the cable's own two ends. A strand passing a closure it was
        never cut at is not available to be spliced there.
      * `self` — a fibre joined to itself is not a joint.
      * `taken` — ONE fibre joins exactly ONE fibre. Enforced on the write so an
        operator finds out while looking at the tray rather than as a fault chip
        later; `taken` maps a fibre to what it is ALREADY joined to here, so the
        message can name the occupant instead of refusing blankly.

    Note what is deliberately NOT refused. Two DIFFERENT cables on the same core
    number is ordinary — a 12F spliced straight through to another 12F is twelve
    such joints. And the same cable on two different cores at one point is a
    U-turn: rare, buildable, and reported by `trace` as a loop if it ever matters.
    Refusing what merely looks strange is how a tool blocks real plant.
    """
    cable_a = cables.get(a[0])
    if cable_a is None or point not in cable_ends(cable_a):
        return "absent"
    if b is not None:
        cable_b = cables.get(b[0])
        if cable_b is None or point not in cable_ends(cable_b):
            return "absent"
        if a == b:
            return "self"
    if a in taken or (b is not None and b in taken):
        return "taken"
    return None


#: Why a joint was refused, as a sentence. Mirrored in `web/src/lib/fiber.ts`.
JOINT_REFUSAL_TEXT = {
    "absent": "Both fibres have to end at this point — a strand can only be"
              " joined where the cable is opened.",
    "self": "A fibre cannot be joined to itself.",
    "taken": "That fibre is already joined to another one here. One fibre joins"
             " exactly one fibre.",
}


def continuity(joints: list[dict]) -> dict[tuple[int, int, Point], list[Fibre]]:
    """What each fibre continues as, keyed by the END it continues from.

    The key is `(cable, core, point)` because continuity is a fact about an END,
    not about a strand: the same core of the same sheath is routinely spliced to
    something different at each of its two closures.

    ONE SOURCE now, which is the whole gain over the model this replaces. There
    used to be a second, implicit rule — two sections of one cable on one core
    meeting at a box are continuous by definition — and keeping the two from
    disagreeing was most of the old module's weight. A sheath opened mid-span is
    now two cables spliced straight through, so the fact is written down.

    Terminations are NOT continuity and are excluded here: a fibre taken out to
    equipment is where the walk stops, which is exactly what `trace` needs it to
    do. `terminations()` reports them separately.

    The value is a LIST rather than one fibre precisely so a fork survives as far
    as the trace, which reports it. Collapsing it here — picking one — is how a
    tool draws a confident line down whichever branch it happened to sort first.
    """
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
    """Every fibre taken out to the equipment standing at its own point."""
    return {(j["a_cable_id"], j["a_core_no"], j["point"]): j
            for j in joints if j.get("b_cable_id") is None}


def taken_at(joints: list[dict], point: Point) -> dict[Fibre, dict]:
    """Which fibres are already joined at `point`, and to what — for `joint_refusal`."""
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
    """The whole optical path one strand makes, across sheaths and joints.

    This is the question somebody holding a light source is asking, and it is the
    one thing no single row can answer: a real fibre leaves the OLT on the trunk,
    is cut at a closure, continues on a DIFFERENT strand of a DIFFERENT sheath to
    the splitter, and may pass two more closures after that.

    Walked from the given fibre in BOTH directions until the glass ends, so it
    does not matter which segment of a long path the operator happened to click.

    `fault` is `fork` when some end continues into more than one fibre, and `loop`
    when the walk returns to a fibre it has already used. Both stop the walk AT
    that point and report it: what comes back is the part that is unambiguous,
    plus `fault_at` naming where certainty ran out. Returning a guess past a fork
    is the failure this module is built against — a splicer following a
    confidently drawn line to the wrong closure.
    """
    by_id = {c["id"]: c for c in cables}
    start = by_id.get(cable_id)
    if start is None:
        return {"ok": False, "fault": "missing", "fault_at": None,
                "hops": [], "points": [], "ends": []}
    joins = continuity(joints)
    ends = terminations(joints)

    def walk(entered: Point) -> tuple[list[dict], str | None, Point | None]:
        """Outward from the start fibre, leaving by the end that is NOT `entered`."""
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
    # Two half-walks that both START at the same fibre, so the backward one is
    # reversed and its first hop (the start itself) dropped before stitching.
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
        # What the two extreme ends are taken out to, when anything. This is the
        # half of the answer a hop list cannot carry: "…and it lands on SPL-4".
        "ends": [ends.get((h["cable_id"], h["core_no"], p))
                 for h, p in ((hops[0], points[0]), (hops[-1], points[-1]))]
        if hops else [],
    }


def feed_map(edges: list[tuple[Point, Point]],
             roots: set[Point]) -> dict[Point, Point]:
    """Which point feeds which, derived from the glass rather than declared.

    A cable is UNDIRECTED — one sheath is one row whichever end the operator was
    standing at — so nothing on it says which way the light goes. That is not a
    gap: direction is a fact about the network's SHAPE, not about a piece of
    fibre, and storing it would be a second copy of something already implied.
    Walking outward from the gear recovers it, and recovers it correctly even
    when the operator records a cascade back to front.

    `roots` is the gear — the points light originates from as far as plant is
    concerned. They are never GIVEN a feed here, only ever used as one: an OLT's
    or a switch's upstream is `parent_device_id`, the explicit monitoring
    dependency that decides what pages, and a fibre record must never be able to
    move it. This answers the other question — the PLANT feed, which is what the
    cumulative split, the PON a splitter sits on and branch-fault localization all
    walk.

    Breadth-first, so a point's feed is its neighbour on the SHORTEST path back to
    gear. Ties break on the lower key purely to be deterministic: two equally
    short paths mean the record describes a ring, and a ring has no upstream to be
    right about — picking stably beats picking cleverly, because a chain that
    reshuffles between two reads is worse than one that is arbitrary but fixed.

    Anything the walk never reaches simply has no feed, and that is the honest
    answer: two couplers joined to each other and to nothing else genuinely do not
    say which of them is upstream.
    """
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
