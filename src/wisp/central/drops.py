from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from wisp.central.onuroster import _norm_mac, display_name

OUTLIER_DB = 3.0

MIN_BRANCH_DARK = 2

_DARK_STATES = ("offline", "los", "dying_gasp")


@dataclass(frozen=True)
class Drop:
    mac: str
    passive_id: int
    olt_id: int | None
    pon_port: str | None
    onu_id: int | None
    name: str | None
    state: str | None
    rx_dbm: float | None
    severity: str | None
    witness: bool = False

    @property
    def matched(self) -> bool:
        return self.olt_id is not None

    @property
    def dark(self) -> bool:
        return self.matched and (self.state or "") in _DARK_STATES

    @property
    def online(self) -> bool:
        return self.state == "online"


@dataclass(frozen=True)
class SplitterLoad:
    passive_id: int
    recorded: int = 0
    online: int = 0
    dark: int = 0
    orphans: int = 0
    crit: int = 0
    warn: int = 0
    rx_seen: int = 0
    rx_median: float | None = None
    rx_worst: float | None = None
    outliers: int = 0
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


    passive_id: int
    parent_id: int | None
    olt_id: int | None
    pon_ports: tuple[str, ...]
    dark: int
    lit_siblings: int
    cause: str = "fiber"
    witness_dark: int = 0
    suspected: bool = True
    passives: tuple[int, ...] = ()

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
    groups: dict[int, list[Drop]] = {}
    for d in drops:
        groups.setdefault(d.passive_id, []).append(d)
    out: dict[int, SplitterLoad] = {}
    for pid, members in groups.items():
        rx = [d.rx_dbm for d in members if d.rx_dbm is not None and d.online]
        median = _median(rx)
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
            olt_id=next(iter(olts)) if len(olts) == 1 else None,
            pon_ports=tuple(ports))
    return out


@dataclass
class _Node:
    parent: int | None
    kids: list[int] = field(default_factory=list)


def _tree(passive_ids: set[int], parents: dict[int, int | None]) -> dict[int, _Node]:

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


    live = [d for d in drops
            if d.matched and (fresh_olt_ids is None or d.olt_id in fresh_olt_ids)]
    if not live:
        return []
    by_passive: dict[int, list[Drop]] = {}
    for d in live:
        by_passive.setdefault(d.passive_id, []).append(d)
    tree = _tree(set(by_passive), parents)

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
        if parent is None or parent not in tree:
            continue
        lit_siblings = lit.get(parent, 0)
        if lit_siblings <= 0:
            continue
        ds = members.get(pid, [])
        gasps = sum(1 for d in ds if d.state == "dying_gasp")
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
