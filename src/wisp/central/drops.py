"""Subscriber drops: what hangs off each splitter, and which branch went dark.

An access network is not OLT -> ONU. It is::

    OLT PON --feeder--> splitter (1:2/1:4/1:8) --> [splitter] --> drop --> ONU

and an ISP hangs a customer off whichever splitter is nearest, so a subscriber
can sit two or three passives below its OLT. The splitter chain has always been
in `org_devices` (passives with parent chains and drawn `link_routes`); what was
missing was the LAST hop — which box a given subscriber's drop comes out of.
`onu_drops` records it and this module is the pure math over it.

Two things fall out, and the second is the reason the first is worth the typing:

  1. **Load.** A splitter with a recorded ratio and recorded subscribers can say
     "1:8, six recorded". Note what it may NOT say: that two legs are free. A leg
     nobody wrote down is UNKNOWN, not spare — the same distinction the rest of
     this codebase keeps between "nothing is wrong" and "nothing is measured".
     Over-subscription is the one capacity claim that survives incomplete
     records, because more recorded drops than legs is provable either way.

  2. **Branch localization.** When every recorded subscriber below one passive is
     dark while a sibling branch is still lit, the break is in the span feeding
     that passive. That is a SEGMENT on the map — two pins and the cable between
     them — where PON ranging gives an interval that runs ~39% short on the
     C-Data fleet (its `distance_m` is EPON time quanta, not metres). Topology
     beats a mis-scaled distance, and it needs no vendor support at all.

Pure like `ponfault` and `onuroster`: dicts in, dataclasses out, no I/O. Nothing
here opens an outage or pages anybody, and no shell imports it for that — this
is a read-side derivation the dashboard renders, and keeping it structurally
incapable of paging is what lets it be as opinionated as it is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from wisp.central.onuroster import _norm_mac, display_name

# How far below its OWN splitter's median an ONU has to sit before the reading is
# worth pointing at. Subscribers sharing a splitter see the same feeder and the
# same split loss, so they differ only by drop length and connectors — at
# 0.25 dB/km a 3 dB gap would be twelve kilometres of drop cable. In practice it
# is a bad splice, a tight bend or a dirty connector on that one drop, which is a
# job for one van rather than a fibre crew. Compared against SIBLINGS on purpose:
# an absolute budget would need the OLT's launch power, which no vendor here
# reports, and a modelled number would be a guess wearing a decimal point.
OUTLIER_DB = 3.0

# A single dark subscriber is a subscriber problem. Two is the floor at which
# "the span above them" becomes the simpler explanation than two coincidences.
MIN_BRANCH_DARK = 2

_DARK_STATES = ("offline", "los", "dying_gasp")


@dataclass(frozen=True)
class Drop:
    """One recorded subscriber, resolved against the current roster."""
    mac: str
    passive_id: int          # the box the drop comes off
    olt_id: int | None       # None when the MAC is in no current roster
    pon_port: str | None
    onu_id: int | None
    # what to CALL this subscriber — `onuroster.display_name`, so the operator's
    # own name (typed in the field, `onu_places.label`) wins over the string the
    # OLT reports, exactly as it does on the Optical tab and in search
    name: str | None
    state: str | None
    rx_dbm: float | None
    severity: str | None
    witness: bool = False    # also a placed reference ONU (power-backed)

    @property
    def matched(self) -> bool:
        """False = recorded here but absent from every current roster: an RMA'd
        box, or a MAC typed against the wrong sticker. Reported, never hidden —
        a drop that quietly stopped counting is the failure this list must not
        conceal (the same rule reference points keep)."""
        return self.olt_id is not None

    @property
    def dark(self) -> bool:
        return self.matched and (self.state or "") in _DARK_STATES

    @property
    def online(self) -> bool:
        return self.state == "online"


@dataclass(frozen=True)
class SplitterLoad:
    """What one passive box is carrying, from its own recorded drops only."""
    passive_id: int
    recorded: int = 0
    online: int = 0
    dark: int = 0
    orphans: int = 0          # recorded MACs no roster knows
    crit: int = 0
    warn: int = 0
    rx_seen: int = 0
    rx_median: float | None = None
    rx_worst: float | None = None
    outliers: int = 0         # OUTLIER_DB or more below this box's own median
    olt_id: int | None = None
    pon_ports: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"passive_id": self.passive_id, "recorded": self.recorded,
                "online": self.online, "dark": self.dark,
                "orphans": self.orphans, "crit": self.crit, "warn": self.warn,
                "rx_seen": self.rx_seen, "rx_median": self.rx_median,
                "rx_worst": self.rx_worst, "outliers": self.outliers,
                "olt_id": self.olt_id, "pon_ports": list(self.pon_ports)}


@dataclass(frozen=True)
class BranchFault:
    """Every recorded subscriber below `passive_id` is dark; a sibling is lit.

    `cause` is the same power/fibre cross `ponfault` draws, judged on the same
    evidence and with the same refusal to guess:

      * ``fiber``  — a placed reference ONU inside the branch went dark (power
        cannot explain a UPS-backed subscriber), or the branch simply fell
        silent on hardware that reports no dying gasp.
      * ``power``  — the ONUs announced their own power loss. Recorded, and
        deliberately quieter: rolling a splicing crew for a DISCOM outage is the
        expensive mistake this whole area exists to avoid.

    `suspected` is False only when a witness proves it. Everything else is a
    strong hypothesis and is labelled as one.
    """
    passive_id: int
    parent_id: int | None
    olt_id: int | None
    pon_ports: tuple[str, ...]
    dark: int
    lit_siblings: int
    cause: str = "fiber"
    witness_dark: int = 0
    suspected: bool = True
    passives: tuple[int, ...] = ()   # the dark subtree, deepest included

    def as_dict(self) -> dict:
        return {"passive_id": self.passive_id, "parent_id": self.parent_id,
                "olt_id": self.olt_id, "pon_ports": list(self.pon_ports),
                "dark": self.dark, "lit_siblings": self.lit_siblings,
                "cause": self.cause, "witness_dark": self.witness_dark,
                "suspected": self.suspected, "passives": list(self.passives)}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def resolve_drops(roster: Iterable[dict], drop_map: dict[str, int],
                  witness_macs: set[str] | None = None) -> list[Drop]:
    """Join recorded drops to the CURRENT roster, by MAC.

    Ambiguity is dropped rather than guessed, the way the web-optics merge and
    the reference-ONU list both do it: a MAC on two live slots cannot be said to
    hang off one splitter, and pinning a subscriber to the wrong box sends a van
    to the wrong street. The row still appears — as an orphan, which is the
    honest rendering of "we know this drop exists and cannot place it".
    """
    witness = witness_macs or set()
    by_mac: dict[str, list[dict]] = {}
    for r in roster:
        mac = _norm_mac(r.get("serial"))
        if mac:
            by_mac.setdefault(mac, []).append(r)
    out: list[Drop] = []
    for mac, passive_id in sorted(drop_map.items()):
        hits = by_mac.get(mac, [])
        r = hits[0] if len(hits) == 1 else {}
        out.append(Drop(
            mac=mac, passive_id=passive_id,
            olt_id=r.get("device_id"), pon_port=r.get("pon_port"),
            onu_id=r.get("onu_id"), name=display_name(r) or None,
            state=r.get("state"),
            rx_dbm=r.get("rx_dbm"), severity=r.get("severity"),
            witness=mac in witness))
    return out


def splitter_loads(drops: Iterable[Drop]) -> dict[int, SplitterLoad]:
    """Per-passive rollup. Keyed on the passive so a caller can hang it straight
    off a map pin without a second pass over the roster."""
    groups: dict[int, list[Drop]] = {}
    for d in drops:
        groups.setdefault(d.passive_id, []).append(d)
    out: dict[int, SplitterLoad] = {}
    for pid, members in groups.items():
        rx = [d.rx_dbm for d in members if d.rx_dbm is not None and d.online]
        median = _median(rx)
        # An outlier is judged against this box's OWN median, so a splitter whose
        # whole branch reads low is NOT eight outliers — that case is the feeder,
        # and it surfaces as the median itself sitting below its siblings'.
        outliers = (0 if median is None else
                    sum(1 for v in rx if median - v >= OUTLIER_DB))
        ports = sorted({d.pon_port for d in members if d.pon_port})
        olts = {d.olt_id for d in members if d.olt_id is not None}
        out[pid] = SplitterLoad(
            passive_id=pid,
            recorded=len(members),
            online=sum(1 for d in members if d.online),
            dark=sum(1 for d in members if d.dark),
            orphans=sum(1 for d in members if not d.matched),
            crit=sum(1 for d in members if d.severity == "crit" and d.online),
            warn=sum(1 for d in members if d.severity == "warn" and d.online),
            rx_seen=len(rx), rx_median=median,
            rx_worst=min(rx) if rx else None,
            outliers=outliers,
            # One passive should sit under one OLT. More than one means the
            # records disagree with the walk, so claim neither.
            olt_id=next(iter(olts)) if len(olts) == 1 else None,
            pon_ports=tuple(ports))
    return out


@dataclass
class _Node:
    parent: int | None
    kids: list[int] = field(default_factory=list)


def _tree(passive_ids: set[int], parents: dict[int, int | None]) -> dict[int, _Node]:
    """Children map over the passives that carry drops, plus their ancestors.

    Walks the LIVE parent chain rather than a stored path, so re-parenting a
    splitter moves its subtree with no second edit — the same reason paging
    assignment derives its chain every time. Cycle-guarded: validation rejects a
    loop on the way in, but a read that a fault view depends on is the last place
    that may spin on a bad row.
    """
    tree: dict[int, _Node] = {}
    for pid in passive_ids:
        cur: int | None = pid
        seen: set[int] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            parent = parents.get(cur)
            node = tree.get(cur)
            if node is None:
                tree[cur] = _Node(parent=parent)
            cur = parent
    for pid, node in tree.items():
        if node.parent is not None and node.parent in tree:
            tree[node.parent].kids.append(pid)
    return tree


def branch_faults(drops: Iterable[Drop], parents: dict[int, int | None],
                  *, fresh_olt_ids: set[int] | None = None,
                  passive_ids: set[int] | None = None) -> list[BranchFault]:
    """Which span is broken, read off the topology instead of a distance.

    A branch qualifies when EVERY recorded subscriber under it is dark, at least
    `MIN_BRANCH_DARK` of them, and at least one subscriber under a sibling branch
    of the same parent is still lit. That last clause is the whole test: it says
    the feeder reaching the parent is fine, so the break is in the one span
    between the parent and this box.

    It also makes the result self-limiting. When a fault is higher up, the
    branches below it have no lit sibling and drop out on their own, leaving
    exactly the topmost dark node — no "deepest wins" rule to get backwards.

    Two deliberate refusals:

      * An OLT whose walk is stale (or absent from `fresh_olt_ids`) is SKIPPED.
        A silent OLT makes its whole tree look dark, and the ICMP outage already
        owns that page — the same gate `ponfault` keeps.
      * Unrecorded subscribers are never assumed dark or lit. The verdict counts
        only what the operator wrote down and the UI says "recorded", because a
        branch that looks total may simply be the only branch anyone mapped.

    The dark box must itself be PASSIVE plant (`passive_ids`). Ancestors are
    walked to tally subtrees, so without this an OLT losing every ONU — a dead
    PON card, say — would qualify against its own parent switch, and the map
    would paint that OLT's UPLINK as a suspected fibre break. That span carries
    backhaul, not the PON: it is the one cable in the chain this verdict has
    nothing to say about.
    """
    live = [d for d in drops
            if d.matched and (fresh_olt_ids is None or d.olt_id in fresh_olt_ids)]
    if not live:
        return []
    by_passive: dict[int, list[Drop]] = {}
    for d in live:
        by_passive.setdefault(d.passive_id, []).append(d)
    tree = _tree(set(by_passive), parents)

    # subtree tallies, computed by walking each passive's ancestors upward — the
    # tree is a handful of hops deep, so this beats a recursive descent and can
    # not blow a stack on a pathological row.
    dark: dict[int, int] = {}
    lit: dict[int, int] = {}
    members: dict[int, list[Drop]] = {}
    for pid, ds in by_passive.items():
        chain: list[int] = []
        cur: int | None = pid
        seen: set[int] = set()
        while cur is not None and cur not in seen and cur in tree:
            seen.add(cur)
            chain.append(cur)
            cur = tree[cur].parent
        for anc in chain:
            dark[anc] = dark.get(anc, 0) + sum(1 for d in ds if d.dark)
            lit[anc] = lit.get(anc, 0) + sum(1 for d in ds if d.online)
            members.setdefault(anc, []).extend(ds)

    faults: list[BranchFault] = []
    for pid, node in tree.items():
        if passive_ids is not None and pid not in passive_ids:
            continue
        if lit.get(pid, 0) or dark.get(pid, 0) < MIN_BRANCH_DARK:
            continue
        parent = node.parent
        # siblings = everything else under the same parent. Without a parent
        # there is nothing to compare against, and "the whole PON is dark" is a
        # PON fault, which ponfault already owns.
        if parent is None or parent not in tree:
            continue
        lit_siblings = lit.get(parent, 0)
        if lit_siblings <= 0:
            continue
        ds = members.get(pid, [])
        gasps = sum(1 for d in ds if d.state == "dying_gasp")
        # Hardware beats paperwork, exactly as the witness rule has it: an ONU
        # that announced its own power loss outranks the operator's label (its
        # backup failed, or the label was wrong), so a gasping witness proves
        # nothing about fibre and counts in neither tally.
        witness_dark = sum(1 for d in ds
                           if d.dark and d.witness and d.state != "dying_gasp")
        cause = "power" if (witness_dark == 0 and gasps * 2 > len(ds)) else "fiber"
        olts = {d.olt_id for d in ds if d.olt_id is not None}
        faults.append(BranchFault(
            passive_id=pid, parent_id=parent,
            olt_id=next(iter(olts)) if len(olts) == 1 else None,
            pon_ports=tuple(sorted({d.pon_port for d in ds if d.pon_port})),
            dark=dark.get(pid, 0), lit_siblings=lit_siblings,
            cause=cause, witness_dark=witness_dark,
            suspected=witness_dark == 0,
            passives=tuple(sorted({d.passive_id for d in ds}))))
    faults.sort(key=lambda f: (-f.dark, f.passive_id))
    return faults
