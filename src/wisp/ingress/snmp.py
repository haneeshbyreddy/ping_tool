from __future__ import annotations

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

    def is_down(self) -> bool:
        return self.admin_status == "up" and self.oper_status in _DOWN_OPER

@runtime_checkable
class SnmpPoller(Protocol):
    async def walk(self, target: SnmpTarget) -> list[PortStatus]: ...

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
        ))
    return ports

# GETBULK exception values (not data) that end a column mid-response.
_END_OF_TABLE = frozenset({"EndOfMibView", "NoSuchObject", "NoSuchInstance"})
# Each repetition carries one row of every active column, so 8 keeps a PDU near
# ~80 varbinds instead of a jumbo response a cheap agent might truncate.
_MAX_REPETITIONS = 8
# 400 rounds x 8 repetitions = 3200 rows per column; the biggest fleet OLT has
# ~333 interfaces. Past this the agent is looping, not answering.
_MAX_ROUNDS = 400

def _oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(part) for part in oid.split("."))

class MultiColumnWalk:
    """Cursor state for walking N table columns in one multi-varbind GETBULK.

    The ten ifTable/ifXTable columns share their ifIndex rows, so a combined
    walk returns every column per round and cuts round-trips ~10x vs ten serial
    walks. pysnmp's bulk_walk_cmd accepts exactly ONE varbind — passing the ten
    columns positionally was the v0.15.3 fleet-wide stale-ports outage — so the
    combined walk drives raw single-PDU bulk_cmd calls by hand: request() names
    the next OID for each still-active column, feed() consumes the
    repetition-major response (varbind i belongs to requested column i % N;
    GETBULK truncates only at the tail, so the mapping survives short
    responses). A column retires when it leaves its subtree, hits an exception
    value, or returns a non-increasing OID (a buggy agent must stall out, not
    spin forever). Pure — no I/O — so tests drive it with canned rows.
    """

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
        """rows = [(oid, value, value_class_name), ...] in response order."""
        cols = list(self._active)
        if not cols:
            return
        if not rows:  # empty response with no error: the agent has nothing more
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
        self._engine = None

    async def walk(self, target: SnmpTarget) -> list[PortStatus]:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_cmd, bulk_walk_cmd,
            )
        except ImportError as exc:
            raise RuntimeError(
                "SnmpPoller needs 'pysnmp' (pip install pysnmp)."
            ) from exc

        if self._engine is None:
            self._engine = SnmpEngine()
        engine = self._engine
        community = CommunityData(target.community, mpModel=1)
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=self._retries)

        # Fast path: all ten columns in one multi-varbind GETBULK stream (see
        # MultiColumnWalk). If it fails for ANY reason — tooBig from a cheap
        # agent, a mis-ordered response tripping the stall guard, an API drift —
        # fall back to the proven one-column-at-a-time walk below. Fleet-wide
        # stale ports must never ride on one optimization again.
        try:
            walker = MultiColumnWalk(WALK_COLUMNS)
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
        except Exception as exc:
            log.warning("combined ifTable walk of %s failed (%s); "
                        "falling back to per-column walks", target.ip, exc)

        varbinds: list[tuple[str, str]] = []
        try:
            for column in WALK_COLUMNS:
                async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                    engine, community, transport, ContextData(),
                    0, 25, ObjectType(ObjectIdentity(column)),
                    lexicographicMode=False,
                ):
                    if errInd or errStat:
                        raise RuntimeError(
                            f"SNMP walk of {target.ip} failed: {errInd or errStat}")
                    for name, val in binds:
                        varbinds.append((str(name), val.prettyPrint()))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"SNMP walk of {target.ip} failed: {exc}") from exc
        return parse_if_table(varbinds)

def build_snmp_poller(cfg: Config = CONFIG) -> SnmpPoller:
    return PysnmpPoller(cfg)
