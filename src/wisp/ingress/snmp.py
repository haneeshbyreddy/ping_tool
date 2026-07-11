from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wisp.config import CONFIG, Config

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

class PysnmpPoller:

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_timeout_s
        self._engine = None

    async def walk(self, target: SnmpTarget) -> list[PortStatus]:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_walk_cmd,
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
            (target.ip, target.port), timeout=self._timeout, retries=1)
        varbinds: list[tuple[str, str]] = []
        # Walk all ten ifTable/ifXTable columns TOGETHER in one multi-varbind
        # GETBULK, not ten sequential per-column walks. They share the ifIndex
        # index (same rows), so a combined walk returns every column per round
        # and cuts round-trips ~10x — a 250-interface OLT that couldn't finish
        # ten serial walks inside port_walk_timeout_s (60s) now does. parse_if_table
        # keys by ifIndex, so interleaved varbinds parse identically. maxRepetitions
        # is 8 here (not 25): each repetition now yields all ten columns, so 8 keeps
        # a single PDU near ~80 varbinds instead of a jumbo response a cheap agent
        # might truncate.
        columns = [ObjectType(ObjectIdentity(c)) for c in WALK_COLUMNS]
        try:
            async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                engine, community, transport, ContextData(),
                0, 8, *columns,
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
