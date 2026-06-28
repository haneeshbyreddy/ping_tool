"""SNMP port-status ingress (graph topology Part B).

A *sibling* of the ICMP prober, not a Prober impl: `Prober.ping(ip) -> PingResult`
is one reading per IP, but a switch has N ports, so this is a parallel poller that
returns a list of `PortStatus` per device. Scope is IF-MIB **oper/admin status only**
(no CPU/mem/temp): the signal we act on is a *monitored* uplink/infra port going
oper=down while admin=up.

Layering mirrors `probers.py`:

  * the wire format is parsed by a **pure** function (`parse_if_table`) that takes
    already-fetched varbinds — so the suite tests it with hand-built rows and never
    touches the network (same way the notifier tests inject a recording double);
  * `PysnmpPoller` lazy-imports `pysnmp` (only the daemon venv needs it; the
    dashboard + tests stay pure-stdlib), does the GETBULK walk, and hands the
    varbinds to the parser.

`build_snmp_poller(cfg)` is the swap point; keep any new SNMP provider behind the
tiny `SnmpPoller` protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wisp.config import CONFIG, Config

# --- IF-MIB columns we walk (one GETBULK subtree each) ----------------------
# value column OID  ->  field name. The trailing number after the prefix is the ifIndex.
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"          # fallback display name
OID_IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"          # ifAdminStatus
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"           # ifOperStatus
OID_IF_LASTCHANGE = "1.3.6.1.2.1.2.2.1.9"     # ifLastChange (sysUpTime ticks)
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"        # ifName (preferred display name)
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"      # ifAlias (operator label -> what it feeds)

WALK_COLUMNS = (
    OID_IF_DESCR, OID_IF_ADMIN, OID_IF_OPER,
    OID_IF_LASTCHANGE, OID_IF_NAME, OID_IF_ALIAS,
)

# IF-MIB integer status -> label. 1=up is the only "good" oper state; a monitored port
# whose oper is down/lowerLayerDown (while admin=up) is the alarm condition.
_IF_STATUS = {
    1: "up", 2: "down", 3: "testing", 4: "unknown",
    5: "dormant", 6: "notPresent", 7: "lowerLayerDown",
}
# oper states that count as a real port-down (admin must also be up to alarm).
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
    admin_status: str            # 'up' | 'down' | ...
    oper_status: str             # 'up' | 'down' | 'lowerLayerDown' | ...
    last_change: str | None = None  # raw ifLastChange ticks (as text), forensic only

    def is_down(self) -> bool:
        """The alarm condition: oper is down while admin is up (admin-down = intentional,
        stay silent). lowerLayerDown counts as down — an underlying link is gone."""
        return self.admin_status == "up" and self.oper_status in _DOWN_OPER


@runtime_checkable
class SnmpPoller(Protocol):
    async def walk(self, target: SnmpTarget) -> list[PortStatus]: ...


def _status_label(raw: str) -> str:
    """Coerce an ifAdminStatus/ifOperStatus value (an int, or already a label) to a label."""
    s = str(raw).strip()
    try:
        return _IF_STATUS.get(int(s), "unknown")
    except (TypeError, ValueError):
        return s.lower() if s else "unknown"


def _index_after(oid: str, prefix: str) -> int | None:
    """The ifIndex is whatever trails `<prefix>.` in a column OID. Returns None if `oid`
    isn't under `prefix` (so we can route each varbind to its column)."""
    if not oid.startswith(prefix + "."):
        return None
    tail = oid[len(prefix) + 1:]
    head = tail.split(".", 1)[0]   # first sub-id is the index; ignore any deeper suffix
    try:
        return int(head)
    except ValueError:
        return None


def parse_if_table(varbinds: list[tuple[str, str]]) -> list[PortStatus]:
    """Group a flat list of (oid, value) varbinds (the result of walking the IF-MIB
    columns) into one PortStatus per ifIndex. Pure: no pysnmp, no network — this is the
    boundary the tests exercise with hand-built rows.

    `oid` is dotted-decimal text; `value` is text (ints as their decimal string). A blank
    `ifName` falls back to `ifDescr`. Ports are returned sorted by ifIndex."""
    cols: dict[int, dict[str, str]] = {}
    routing = {
        OID_IF_DESCR: "descr", OID_IF_ADMIN: "admin", OID_IF_OPER: "oper",
        OID_IF_LASTCHANGE: "lastchange", OID_IF_NAME: "name", OID_IF_ALIAS: "alias",
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
        # oper status is the load-bearing field; skip a row that didn't return one.
        if "oper" not in c and "admin" not in c:
            continue
        name = (c.get("name") or "").strip() or (c.get("descr") or "").strip() or None
        alias = (c.get("alias") or "").strip() or None
        last = (c.get("lastchange") or "").strip() or None
        ports.append(PortStatus(
            if_index=idx,
            if_name=name,
            if_alias=alias,
            admin_status=_status_label(c.get("admin", "")),
            oper_status=_status_label(c.get("oper", "")),
            last_change=last,
        ))
    return ports


# --- Real pysnmp poller (lazy import) ---------------------------------------
class PysnmpPoller:
    """Real SNMP v2c walk via pysnmp, lazy-imported so the module (and the test suite)
    loads without the dependency. A failed walk raises RuntimeError; the daemon's SNMP
    task catches it and continues — a dead/blocked switch never sinks the ICMP cycle."""

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_timeout_s

    async def walk(self, target: SnmpTarget) -> list[PortStatus]:
        try:
            from pysnmp.hlapi.asyncio import (  # type: ignore
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_walk_cmd,
            )
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "SnmpPoller needs 'pysnmp' (pip install pysnmp)."
            ) from exc

        engine = SnmpEngine()
        community = CommunityData(target.community, mpModel=1)  # mpModel=1 => SNMP v2c
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=1)
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
        except Exception as exc:  # connection/timeout/library error -> surfaced, not masked
            raise RuntimeError(f"SNMP walk of {target.ip} failed: {exc}") from exc
        return parse_if_table(varbinds)


def build_snmp_poller(cfg: Config = CONFIG) -> SnmpPoller:
    return PysnmpPoller(cfg)


def load_snmp_targets(cfg: Config = CONFIG) -> list[tuple[int, SnmpTarget]]:
    """(device_id, SnmpTarget) for every active, non-maintenance device that has SNMP
    enabled and a community set. DB glue (mirrors load_device_meta) so the daemon's SNMP
    task can re-read targets each cycle — an enable/disable from the UI is picked up with
    no restart."""
    from wisp.database.client import connect
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT id, ip_address, snmp_community, snmp_port, snmp_version FROM devices"
            " WHERE is_active=1 AND maintenance=0 AND snmp_enabled=1"
            "   AND snmp_community IS NOT NULL AND snmp_community <> ''"
        ).fetchall()
    return [
        (r["id"], SnmpTarget(
            ip=r["ip_address"], community=r["snmp_community"],
            port=r["snmp_port"] or 161, version=r["snmp_version"] or "2c"))
        for r in rows
    ]
