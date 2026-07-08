"""Device health over SNMP — CPU load, RAM, temperature.

Standard MIBs first: HOST-RESOURCES for CPU (hrProcessorLoad, one row per core)
and RAM (hrStorage rows typed hrStorageRam), ENTITY-SENSOR for temperature,
plus the MikroTik health subtree (RouterOS keeps temperature in its enterprise
tree). Everything is walked in one pass and folded best-effort — whatever a box
doesn't expose just stays None, so a switch with no sensors still reports CPU.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wisp.config import CONFIG, Config

OID_HR_CPU_LOAD = "1.3.6.1.2.1.25.3.3.1.2"       # hrProcessorLoad, 0-100 per core
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"   # hrStorageType
OID_HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"  # hrStorageAllocationUnits (bytes)
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"   # hrStorageSize (in units)
OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"   # hrStorageUsed (in units)
HR_STORAGE_RAM_SUFFIX = "25.2.1.2"               # hrStorageRam type OID tail

OID_ENT_SENSOR_TYPE = "1.3.6.1.2.1.99.1.1.1.1"   # entPhySensorType (8 = celsius)
OID_ENT_SENSOR_SCALE = "1.3.6.1.2.1.99.1.1.1.2"  # EntitySensorDataScale (9 = units)
OID_ENT_SENSOR_PRECISION = "1.3.6.1.2.1.99.1.1.1.3"
OID_ENT_SENSOR_VALUE = "1.3.6.1.2.1.99.1.1.1.4"

OID_MTXR_HEALTH = "1.3.6.1.4.1.14988.1.1.3"      # MikroTik health subtree
_MTXR_TEMP_TAILS = ("10", "11")                  # mtxrHlTemperature, mtxrHlProcessorTemperature

WALK_COLUMNS = (
    OID_HR_CPU_LOAD,
    OID_HR_STORAGE_TYPE, OID_HR_STORAGE_UNITS, OID_HR_STORAGE_SIZE, OID_HR_STORAGE_USED,
    OID_ENT_SENSOR_TYPE, OID_ENT_SENSOR_SCALE, OID_ENT_SENSOR_PRECISION, OID_ENT_SENSOR_VALUE,
    OID_MTXR_HEALTH,
)

# EntitySensorDataScale -> multiplier (units=9 is 1.0; milli=8 is 1e-3, ...)
_SENSOR_SCALE = {7: 1e-6, 8: 1e-3, 9: 1.0, 10: 1e3}
_TEMP_MIN_C, _TEMP_MAX_C = -10.0, 130.0


@dataclass(frozen=True)
class DeviceHealth:
    cpu_pct: float | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    temp_c: float | None = None

    @property
    def mem_pct(self) -> float | None:
        if self.mem_used_bytes is None or not self.mem_total_bytes:
            return None
        return round(100.0 * self.mem_used_bytes / self.mem_total_bytes, 1)

    def is_empty(self) -> bool:
        return (self.cpu_pct is None and self.mem_total_bytes is None
                and self.temp_c is None)

    def to_wire(self) -> dict:
        return {"cpu_pct": self.cpu_pct, "mem_used_bytes": self.mem_used_bytes,
                "mem_total_bytes": self.mem_total_bytes, "mem_pct": self.mem_pct,
                "temp_c": self.temp_c}


@runtime_checkable
class HealthPoller(Protocol):
    async def walk(self, target) -> DeviceHealth: ...


def _to_num(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _tail_after(oid: str, prefix: str) -> str | None:
    p = prefix + "."
    if not oid.startswith(p):
        return None
    return oid[len(p):] or None


def _plausible_temp(v: float | None) -> float | None:
    if v is None or not (_TEMP_MIN_C <= v <= _TEMP_MAX_C):
        return None
    return round(v, 1)


def _fold_column(varbinds, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for oid, value in varbinds:
        tail = _tail_after(oid, prefix)
        if tail is not None:
            out[tail] = "" if value is None else str(value)
    return out


def _parse_cpu(varbinds) -> float | None:
    loads = [v for v in (_to_num(x) for x in _fold_column(varbinds, OID_HR_CPU_LOAD).values())
             if v is not None and 0 <= v <= 100]
    return round(sum(loads) / len(loads), 1) if loads else None


def _parse_memory(varbinds) -> tuple[int | None, int | None]:
    types = _fold_column(varbinds, OID_HR_STORAGE_TYPE)
    units = _fold_column(varbinds, OID_HR_STORAGE_UNITS)
    sizes = _fold_column(varbinds, OID_HR_STORAGE_SIZE)
    useds = _fold_column(varbinds, OID_HR_STORAGE_USED)
    best: tuple[int, int] | None = None
    for idx, stype in types.items():
        if not (stype.endswith(HR_STORAGE_RAM_SUFFIX) or "hrStorageRam" in stype):
            continue
        unit, size, used = _to_num(units.get(idx)), _to_num(sizes.get(idx)), _to_num(useds.get(idx))
        if unit is None or size is None or used is None or size <= 0:
            continue
        total_b, used_b = int(size * unit), int(used * unit)
        if best is None or total_b > best[1]:
            best = (used_b, total_b)
    return (best[0], best[1]) if best else (None, None)


def _parse_entity_temp(varbinds) -> float | None:
    types = _fold_column(varbinds, OID_ENT_SENSOR_TYPE)
    scales = _fold_column(varbinds, OID_ENT_SENSOR_SCALE)
    precisions = _fold_column(varbinds, OID_ENT_SENSOR_PRECISION)
    values = _fold_column(varbinds, OID_ENT_SENSOR_VALUE)
    hottest: float | None = None
    for idx, stype in types.items():
        if _to_num(stype) != 8:  # celsius
            continue
        raw = _to_num(values.get(idx))
        if raw is None:
            continue
        scale = _SENSOR_SCALE.get(int(_to_num(scales.get(idx)) or 9), 1.0)
        precision = int(_to_num(precisions.get(idx)) or 0)
        temp = _plausible_temp(raw * scale / (10 ** precision))
        if temp is not None and (hottest is None or temp > hottest):
            hottest = temp
    return hottest


def _parse_mikrotik_temp(varbinds) -> float | None:
    rows = _fold_column(varbinds, OID_MTXR_HEALTH)
    hottest: float | None = None
    for tail, value in rows.items():
        if tail.split(".", 1)[0] not in _MTXR_TEMP_TAILS:
            continue
        raw = _to_num(value)
        if raw is None:
            continue
        # RouterOS reports some models in tenths of a degree, others in whole
        # degrees; anything above the plausible ceiling is read as tenths.
        temp = _plausible_temp(raw if raw <= _TEMP_MAX_C else raw / 10.0)
        if temp is not None and (hottest is None or temp > hottest):
            hottest = temp
    return hottest


def parse_health(varbinds: list[tuple[str, str]]) -> DeviceHealth:
    used_b, total_b = _parse_memory(varbinds)
    temp = _parse_entity_temp(varbinds)
    if temp is None:
        temp = _parse_mikrotik_temp(varbinds)
    return DeviceHealth(cpu_pct=_parse_cpu(varbinds), mem_used_bytes=used_b,
                        mem_total_bytes=total_b, temp_c=temp)


class PysnmpHealthPoller:
    """One SnmpEngine per poller instance, NEVER one per walk (see CLAUDE.md —
    a per-walk engine leaks its UDP transport registration forever)."""

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_timeout_s
        self._engine = None

    async def walk(self, target) -> DeviceHealth:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_walk_cmd,
            )
        except ImportError as exc:
            raise RuntimeError(
                "HealthPoller needs 'pysnmp' (pip install pysnmp)."
            ) from exc

        if self._engine is None:
            self._engine = SnmpEngine()
        engine = self._engine
        community = CommunityData(target.community, mpModel=1)
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
                            f"SNMP health walk of {target.ip} failed: {errInd or errStat}")
                    for name, val in binds:
                        varbinds.append((str(name), val.prettyPrint()))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"SNMP health walk of {target.ip} failed: {exc}") from exc
        return parse_health(varbinds)


def build_health_poller(cfg: Config = CONFIG) -> HealthPoller:
    return PysnmpHealthPoller(cfg)
