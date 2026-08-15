from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.snmp")

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_LASTCHANGE = "1.3.6.1.2.1.2.2.1.9"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HCIN = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HCOUT = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGHSPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

WALK_COLUMNS = (
    OID_IF_DESCR, OID_IF_ADMIN, OID_IF_OPER, OID_IF_LASTCHANGE, OID_IF_SPEED,
    OID_IF_NAME, OID_IF_HCIN, OID_IF_HCOUT, OID_IF_HIGHSPEED, OID_IF_ALIAS,
)

_COLUMN_NAMES = {
    OID_IF_ADMIN: "admin_status", OID_IF_OPER: "oper_status",
    OID_IF_HCIN: "in_octets", OID_IF_HCOUT: "out_octets",
    OID_IF_NAME: "if_name", OID_IF_DESCR: "if_descr",
    OID_IF_LASTCHANGE: "last_change", OID_IF_HIGHSPEED: "speed",
    OID_IF_SPEED: "speed_32bit", OID_IF_ALIAS: "alias",
}

_MISSING_PHRASE = {
    "in_octets": "bandwidth counters", "out_octets": "bandwidth counters",
    "admin_status": "port status", "oper_status": "port status",
    "if_name": "port identity", "if_descr": "port identity",
    "alias": "port identity", "speed": "link speed",
    "speed_32bit": "link speed", "last_change": "last change",
}

def missing_detail(missing: Sequence[str]) -> str:
    groups: dict[str, list[str]] = {}
    for name in missing:
        groups.setdefault(_MISSING_PHRASE.get(name, name), []).append(name)
    return "walk dropped: " + "; ".join(
        f"{phrase} ({', '.join(names)})" for phrase, names in groups.items())

_IF_STATUS = {
    1: "up", 2: "down", 3: "testing", 4: "unknown",
    5: "dormant", 6: "notPresent", 7: "lowerLayerDown",
}
_DOWN_OPER = frozenset({"down", "lowerLayerDown"})

@dataclass(frozen=True)
class SnmpTarget:
    ip: str
    community: str
    port: int = 161
    version: str = "2c"

@dataclass(frozen=True)
class PortStatus:
    if_index: int
    if_name: str | None
    if_alias: str | None
    admin_status: str
    oper_status: str
    last_change: str | None = None
    in_octets: int | None = None
    out_octets: int | None = None
    speed_bps: int | None = None
    # Which wire keys this row actually has a cell for; None = presence unknown,
    # so a hand-built PortStatus keeps carrying every field.
    carried: frozenset[str] | None = None

    def is_down(self) -> bool:
        return self.admin_status == "up" and self.oper_status in _DOWN_OPER

@dataclass(frozen=True)
class PortWalkResult:
    ports: list[PortStatus]
    missing_columns: tuple[str, ...] = ()

@runtime_checkable
class SnmpPoller(Protocol):
    async def walk(self, target: SnmpTarget,
                   scope: str = "full") -> PortWalkResult: ...

def _status_label(raw: str) -> str:
    s = str(raw).strip()
    try:
        return _IF_STATUS.get(int(s), "unknown")
    except (TypeError, ValueError):
        return s.lower() if s else "unknown"

def _int_or_none(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None

def throughput_bps(prev_octets: int | None, cur_octets: int | None,
                   dt_seconds: float) -> float | None:
    if prev_octets is None or cur_octets is None or dt_seconds <= 0:
        return None
    delta = cur_octets - prev_octets
    if delta < 0:
        return None
    return (delta * 8.0) / dt_seconds

def _index_after(oid: str, prefix: str) -> int | None:
    if not oid.startswith(prefix + "."):
        return None
    tail = oid[len(prefix) + 1:]
    head = tail.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None

def parse_if_table(varbinds: list[tuple[str, str]]) -> list[PortStatus]:
    cols: dict[int, dict[str, str]] = {}
    routing = {
        OID_IF_DESCR: "descr", OID_IF_ADMIN: "admin", OID_IF_OPER: "oper",
        OID_IF_LASTCHANGE: "lastchange", OID_IF_NAME: "name", OID_IF_ALIAS: "alias",
        OID_IF_SPEED: "speed", OID_IF_HCIN: "hcin", OID_IF_HCOUT: "hcout",
        OID_IF_HIGHSPEED: "highspeed",
    }
    for oid, value in varbinds:
        for prefix, key in routing.items():
            idx = _index_after(oid, prefix)
            if idx is not None:
                cols.setdefault(idx, {})[key] = "" if value is None else str(value)
                break

    ports: list[PortStatus] = []
    for idx in sorted(cols):
        c = cols[idx]
        if "oper" not in c and "admin" not in c:
            continue
        name = (c.get("name") or "").strip() or (c.get("descr") or "").strip() or None
        alias = (c.get("alias") or "").strip() or None
        last = (c.get("lastchange") or "").strip() or None
        hi = _int_or_none(c.get("highspeed"))
        lo = _int_or_none(c.get("speed"))
        speed_bps = (hi * 1_000_000) if hi else (lo if lo else None)
        ports.append(PortStatus(
            if_index=idx,
            if_name=name,
            if_alias=alias,
            admin_status=_status_label(c.get("admin", "")),
            oper_status=_status_label(c.get("oper", "")),
            last_change=last,
            in_octets=_int_or_none(c.get("hcin")),
            out_octets=_int_or_none(c.get("hcout")),
            speed_bps=speed_bps,
            carried=frozenset(k for k, arrived in (
                ("if_name", "name" in c or "descr" in c),
                ("if_alias", "alias" in c),
                ("last_change", "lastchange" in c),
                ("in_octets", "hcin" in c),
                ("out_octets", "hcout" in c),
                ("speed_bps", "speed" in c or "highspeed" in c),
            ) if arrived),
        ))
    return ports

_END_OF_TABLE = frozenset({"EndOfMibView", "NoSuchObject", "NoSuchInstance"})
_MAX_REPETITIONS = 8
_MAX_ROUNDS = 400

_FAST_PATH_MAX_S = 15.0
_WALK_MARGIN_S = 0.25
_DEAD_AGENT_GIVEUP = 3
_PROMOTE_INTERVAL_S = 6 * 3600.0
_PERCOLUMN_REPETITIONS = 25
_PERCOLUMN_SMALL_REPETITIONS = 4
_MAX_LEVEL = 2

# Status first (a budget-starved walk must still yield port up/down), counters
# next: central preserves stored identity for a row that carries no identity
# cell, so on a short walk the static columns are what may be sacrificed.
_FULL_ORDER = (
    OID_IF_ADMIN, OID_IF_OPER, OID_IF_HCIN, OID_IF_HCOUT, OID_IF_NAME,
    OID_IF_DESCR, OID_IF_LASTCHANGE, OID_IF_HIGHSPEED, OID_IF_SPEED, OID_IF_ALIAS,
)
_HOT_ORDER = (OID_IF_ADMIN, OID_IF_OPER, OID_IF_HCIN, OID_IF_HCOUT)
_SCOPE_COLUMNS = {"full": _FULL_ORDER, "hot": _HOT_ORDER}

def _oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(part) for part in oid.split("."))

class MultiColumnWalk:

    def __init__(self, columns: Sequence[str]) -> None:
        self._root = {c: _oid_tuple(c) for c in columns}
        self._cursor = {c: _oid_tuple(c) for c in columns}
        self._active = list(columns)
        self.varbinds: list[tuple[str, str]] = []

    @property
    def done(self) -> bool:
        return not self._active

    def request(self) -> list[str]:
        return [".".join(str(x) for x in self._cursor[c]) for c in self._active]

    def feed(self, rows: Sequence[tuple[str, str, str]]) -> None:
        cols = list(self._active)
        if not cols:
            return
        if not rows:
            self._active = []
            return
        finished: set[str] = set()
        for i, (oid, value, value_class) in enumerate(rows):
            col = cols[i % len(cols)]
            if col in finished:
                continue
            if value_class in _END_OF_TABLE:
                finished.add(col)
                continue
            t = _oid_tuple(oid)
            root = self._root[col]
            if t[: len(root)] != root or t <= self._cursor[col]:
                finished.add(col)
                continue
            self.varbinds.append((oid, value))
            self._cursor[col] = t
        self._active = [c for c in cols if c not in finished]

class PysnmpPoller:

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_request_timeout_s or cfg.snmp_timeout_s
        self._retries = max(1, cfg.snmp_request_retries)
        self._budget_s = cfg.port_walk_timeout_s
        self._engine = None
        self._device_level: dict[str, int] = {}
        self._promote_at: dict[str, float] = {}

    def _start_level(self, key: str, now: float) -> int:
        level = self._device_level.get(key, 0)
        if level > 0 and now >= self._promote_at.get(key, 0.0):
            self._promote_at[key] = now + _PROMOTE_INTERVAL_S
            level -= 1
        return level

    def _remember(self, key: str, level: int, now: float) -> None:
        self._device_level[key] = level
        if level > 0:
            self._promote_at.setdefault(key, now + _PROMOTE_INTERVAL_S)
        else:
            self._promote_at.pop(key, None)

    async def walk(self, target: SnmpTarget, scope: str = "full") -> PortWalkResult:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget,
            )
        except ImportError as exc:
            raise RuntimeError(
                "SnmpPoller needs 'pysnmp' (pip install pysnmp)."
            ) from exc

        if self._engine is None:
            self._engine = SnmpEngine()
        community = CommunityData(target.community, mpModel=1)
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=self._retries)

        loop = asyncio.get_running_loop()
        now = loop.time()
        budget = self._budget_s
        if budget and budget > 0:
            hard_deadline: float | None = now + budget - _WALK_MARGIN_S
            fast_budget = min(_FAST_PATH_MAX_S, budget / 3.0)
        else:
            hard_deadline = None
            fast_budget = _FAST_PATH_MAX_S

        key = f"{target.ip}:{target.port}"
        columns = _SCOPE_COLUMNS.get(scope, _FULL_ORDER)
        answered = False
        last_exc: Exception | None = None
        missing: tuple[str, ...] = ()

        for level in range(self._start_level(key, now), _MAX_LEVEL + 1):
            if level == 0:
                ports = await self._combined_walk(
                    target, community, transport, fast_budget, columns)
                if ports is None:
                    continue
                answered = True
                if ports:
                    self._remember(key, 0, now)
                    return PortWalkResult(ports)
                continue
            max_rep = (_PERCOLUMN_REPETITIONS if level == 1
                       else _PERCOLUMN_SMALL_REPETITIONS)
            varbinds, responded, exc, missing = await self._percolumn_walk(
                target, community, transport, max_rep, hard_deadline, columns)
            answered = answered or responded
            if exc is not None:
                last_exc = exc
            ports = parse_if_table(varbinds)
            if ports:
                self._remember(key, level, now)
                return PortWalkResult(ports, missing)

        if answered:
            return PortWalkResult([], missing)
        raise RuntimeError(
            f"SNMP walk of {target.ip} failed: {last_exc}") from last_exc

    async def _combined_walk(self, target, community, transport, fast_budget,
                             columns):
        from pysnmp.hlapi.asyncio import (
            ContextData, ObjectType, ObjectIdentity, bulk_cmd,
        )
        engine = self._engine

        async def inner() -> list[PortStatus]:
            walker = MultiColumnWalk(columns)
            rounds = 0
            while not walker.done:
                rounds += 1
                if rounds > _MAX_ROUNDS:
                    raise RuntimeError("combined ifTable walk did not terminate")
                errInd, errStat, errIdx, binds = await bulk_cmd(
                    engine, community, transport, ContextData(),
                    0, _MAX_REPETITIONS,
                    *[ObjectType(ObjectIdentity(o)) for o in walker.request()])
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                walker.feed([(str(name), val.prettyPrint(), type(val).__name__)
                             for name, val in binds])
            return parse_if_table(walker.varbinds)

        try:
            if fast_budget and fast_budget > 0:
                return await asyncio.wait_for(inner(), fast_budget)
            return await inner()
        except Exception as exc:
            log.warning("combined ifTable walk of %s failed (%s); "
                        "falling back to per-column walks", target.ip, exc)
            return None

    async def _percolumn_walk(self, target, community, transport, max_rep,
                              hard_deadline, columns):
        from pysnmp.hlapi.asyncio import (
            ContextData, ObjectType, ObjectIdentity, bulk_walk_cmd,
        )
        engine = self._engine
        loop = asyncio.get_running_loop()

        # The accumulator is the CALLER's, so a column cut off at the deadline
        # keeps the rows it had already fetched instead of dying with the
        # cancelled coroutine.
        async def one_column(col: str, out: list[tuple[str, str]]) -> None:
            async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                engine, community, transport, ContextData(),
                0, max_rep, ObjectType(ObjectIdentity(col)),
                lexicographicMode=False,
            ):
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                for name, val in binds:
                    out.append((str(name), val.prettyPrint()))

        varbinds: list[tuple[str, str]] = []
        missing: list[str] = []
        responded = False
        consecutive_fail = 0
        last_exc: Exception | None = None
        for i, column in enumerate(columns):
            remaining = None
            if hard_deadline is not None:
                remaining = hard_deadline - loop.time()
                if remaining <= 0:
                    missing.extend(_COLUMN_NAMES[c] for c in columns[i:])
                    break
            out: list[tuple[str, str]] = []
            try:
                if remaining is not None:
                    await asyncio.wait_for(one_column(column, out), remaining)
                else:
                    await one_column(column, out)
            except Exception as exc:
                last_exc = exc
                missing.append(_COLUMN_NAMES[column])
                consecutive_fail += 1
                log.debug("ifTable column %s of %s cut short after %d rows: %s",
                          column, target.ip, len(out), exc)
                varbinds.extend(out)
                responded = responded or bool(out)
                if not responded and consecutive_fail >= _DEAD_AGENT_GIVEUP:
                    break
                continue
            responded = True
            consecutive_fail = 0
            varbinds.extend(out)
        return varbinds, responded, last_exc, tuple(missing)

def build_snmp_poller(cfg: Config = CONFIG) -> SnmpPoller:
    return PysnmpPoller(cfg)
