"""Device health over SNMP — CPU load, RAM, temperature.

Standard MIBs first: HOST-RESOURCES for CPU (hrProcessorLoad, one row per core)
and RAM (hrStorage rows typed hrStorageRam), ENTITY-SENSOR for temperature,
plus the MikroTik health subtree (RouterOS keeps temperature in its enterprise
tree). Everything is walked in one pass and folded best-effort — whatever a box
doesn't expose just stays None, so a switch with no sensors still reports CPU.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.health")

# sysObjectID — "who made you". Its value is an OID inside the maker's enterprise
# arc (…4.1.14988 = MikroTik, …4.1.5651 = Fiberhome); central-served profiles match
# on a prefix of it, so one profile row lights up every device of that vendor.
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2"

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

# Fiberhome (enterprise 5651) keeps host stats in its OWN tree, not HOST-RESOURCES or
# ENTITY-SENSOR — an S3330-class switch answers neither. A compact summary scalar group
# carries CPU/mem/temp; a separate group carries the raw RAM bytes (so the meter shows
# used/total, not just a percent). Both are walked best-effort like every other column
# and only fill fields the standard MIBs left None, so a non-Fiberhome box that returns
# nothing here costs one dead subtree and changes no reading. Confirmed on a real
# S3330-12TXF walk (2026-07); CPU-vs-temp column order (.2 vs .3) is field-inferred — if
# the switch web GUI disagrees, swap _FH_CPU_TAIL/_FH_TEMP_TAIL.
OID_FH_HEALTH = "1.3.6.1.4.1.5651.3.901"         # .1.0 mem %, .2.0 CPU %, .3.0 temp C
OID_FH_MEM = "1.3.6.1.4.1.5651.3.20.1.1.1"       # .8.0 total bytes, .5.0 used bytes
_FH_CPU_TAIL, _FH_TEMP_TAIL = "2.0", "3.0"
_FH_MEM_TOTAL_TAIL, _FH_MEM_USED_TAIL = "8.0", "5.0"

WALK_COLUMNS = (
    OID_HR_CPU_LOAD,
    OID_HR_STORAGE_TYPE, OID_HR_STORAGE_UNITS, OID_HR_STORAGE_SIZE, OID_HR_STORAGE_USED,
    OID_ENT_SENSOR_TYPE, OID_ENT_SENSOR_SCALE, OID_ENT_SENSOR_PRECISION, OID_ENT_SENSOR_VALUE,
    OID_MTXR_HEALTH,
    OID_FH_HEALTH, OID_FH_MEM,
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
    # Some vendors expose memory only as a ready-made percent — a profile can map
    # that directly when the raw byte counters aren't on the wire.
    mem_pct_direct: float | None = None

    @property
    def mem_pct(self) -> float | None:
        if self.mem_used_bytes is None or not self.mem_total_bytes:
            return self.mem_pct_direct
        return round(100.0 * self.mem_used_bytes / self.mem_total_bytes, 1)

    def is_empty(self) -> bool:
        return (self.cpu_pct is None and self.mem_total_bytes is None
                and self.temp_c is None and self.mem_pct_direct is None)

    def to_wire(self) -> dict:
        return {"cpu_pct": self.cpu_pct, "mem_used_bytes": self.mem_used_bytes,
                "mem_total_bytes": self.mem_total_bytes, "mem_pct": self.mem_pct,
                "temp_c": self.temp_c}


@runtime_checkable
class HealthPoller(Protocol):
    async def walk(self, target,
                   profiles: list[dict] | None = None) -> DeviceHealth: ...


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


def _parse_fiberhome_cpu(varbinds) -> float | None:
    v = _to_num(_fold_column(varbinds, OID_FH_HEALTH).get(_FH_CPU_TAIL))
    return round(v, 1) if v is not None and 0 <= v <= 100 else None


def _parse_fiberhome_temp(varbinds) -> float | None:
    return _plausible_temp(_to_num(_fold_column(varbinds, OID_FH_HEALTH).get(_FH_TEMP_TAIL)))


def _parse_fiberhome_memory(varbinds) -> tuple[int | None, int | None]:
    rows = _fold_column(varbinds, OID_FH_MEM)
    total, used = _to_num(rows.get(_FH_MEM_TOTAL_TAIL)), _to_num(rows.get(_FH_MEM_USED_TAIL))
    if total is None or used is None or total <= 0:
        return (None, None)
    return (int(used), int(total))


# --- central-served vendor profiles (data, not code — see CLAUDE.md) ---------------
# A profile maps health metrics to vendor OIDs plus a decode rule from this CLOSED
# vocabulary. Standard MIBs still parse first; profile values only fill fields the
# standards left None (same discipline as the hardcoded MikroTik/Fiberhome fallbacks,
# which remain in code for fleets running an older central).

_DECODES = {
    "as_is": lambda v: v,
    "div10": lambda v: v / 10.0,
    "div100": lambda v: v / 100.0,
    # Signed 16-bit in hundredths — the classic optical/temperature encoding
    # (63021 -> -25.15).
    "signed_div100": lambda v: (v - 65536.0 if v > 32767 else v) / 100.0,
}

_ENTERPRISES_PREFIX = "1.3.6.1.4.1"
_NUM_OID_RE = re.compile(r"^\d+(\.\d+)*$")


def _norm_oid_value(raw) -> str | None:
    """Normalise a sysObjectID VALUE to dotted-numeric — pysnmp may render it
    either numeric or as SNMPv2-SMI::enterprises.<tail> depending on loaded MIBs."""
    s = str(raw or "").strip().strip(".")
    if not s:
        return None
    if "enterprises" in s:
        tail = s.split("enterprises", 1)[1].lstrip(".")
        if _NUM_OID_RE.match(tail):
            return f"{_ENTERPRISES_PREFIX}.{tail}"
        return None
    return s if _NUM_OID_RE.match(s) else None


def sys_object_id(varbinds: list[tuple[str, str]]) -> str | None:
    for tail, value in _fold_column(varbinds, OID_SYS_OBJECT_ID).items():
        norm = _norm_oid_value(value)
        if norm:
            return norm
    return None


def match_profile(profiles: list[dict] | None, sysobjectid: str | None) -> dict | None:
    """Longest matching sysObjectID prefix wins (a model-specific profile beats a
    vendor-wide one)."""
    if not profiles or not sysobjectid:
        return None
    best: dict | None = None
    best_len = -1
    for p in profiles:
        prefix = str(p.get("match_sysobjectid") or "").strip().strip(".")
        if not prefix:
            continue
        if sysobjectid == prefix or sysobjectid.startswith(prefix + "."):
            if len(prefix) > best_len:
                best, best_len = p, len(prefix)
    return best


def profile_walk_roots(profile: dict | None) -> list[str]:
    """Subtree roots the poller must walk for a profile. A metric OID ending in .0
    is a scalar leaf — GETNEXT under it yields nothing, so walk its parent."""
    roots: list[str] = []
    for spec in (profile or {}).get("metrics", {}).values():
        oid = str(spec.get("oid") or "").strip().strip(".")
        if not oid:
            continue
        root = oid[:-2] if oid.endswith(".0") else oid
        if root and root not in roots:
            roots.append(root)
    return roots


def _select(values: list[float], how: str) -> float:
    if how == "avg":
        return sum(values) / len(values)
    if how == "max":
        return max(values)
    if how == "sum":
        return sum(values)
    return values[0]


def parse_profile_metrics(varbinds: list[tuple[str, str]],
                          profile: dict | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for metric, spec in (profile or {}).get("metrics", {}).items():
        oid = str(spec.get("oid") or "").strip().strip(".")
        if not oid:
            continue
        exact = [v for o, v in varbinds if o == oid]
        rows = exact or [v for o, v in varbinds if o.startswith(oid + ".")]
        nums = [n for n in (_to_num(v) for v in rows) if n is not None]
        if not nums:
            continue
        decode = _DECODES.get(str(spec.get("decode") or "as_is"), _DECODES["as_is"])
        out[metric] = _select([decode(n) for n in nums],
                              str(spec.get("select") or "first"))
    return out


def _apply_profile(health: DeviceHealth, metrics: dict[str, float]) -> DeviceHealth:
    if not metrics:
        return health
    cpu = health.cpu_pct
    if cpu is None and "cpu_pct" in metrics and 0 <= metrics["cpu_pct"] <= 100:
        cpu = round(metrics["cpu_pct"], 1)
    temp = health.temp_c
    if temp is None and "temp_c" in metrics:
        temp = _plausible_temp(metrics["temp_c"])
    used_b, total_b = health.mem_used_bytes, health.mem_total_bytes
    if total_b is None and metrics.get("mem_total_bytes", 0) > 0:
        total_b = int(metrics["mem_total_bytes"])
        used_b = (int(metrics["mem_used_bytes"])
                  if metrics.get("mem_used_bytes") is not None else None)
    mem_pct = health.mem_pct_direct
    if (total_b is None and mem_pct is None and "mem_pct" in metrics
            and 0 <= metrics["mem_pct"] <= 100):
        mem_pct = round(metrics["mem_pct"], 1)
    return DeviceHealth(cpu_pct=cpu, mem_used_bytes=used_b, mem_total_bytes=total_b,
                        temp_c=temp, mem_pct_direct=mem_pct)


def parse_health(varbinds: list[tuple[str, str]],
                 profile: dict | None = None) -> DeviceHealth:
    used_b, total_b = _parse_memory(varbinds)
    if total_b is None:
        used_b, total_b = _parse_fiberhome_memory(varbinds)
    temp = _parse_entity_temp(varbinds)
    if temp is None:
        temp = _parse_mikrotik_temp(varbinds)
    if temp is None:
        temp = _parse_fiberhome_temp(varbinds)
    cpu = _parse_cpu(varbinds)
    if cpu is None:
        cpu = _parse_fiberhome_cpu(varbinds)
    health = DeviceHealth(cpu_pct=cpu, mem_used_bytes=used_b,
                          mem_total_bytes=total_b, temp_c=temp)
    return _apply_profile(health, parse_profile_metrics(varbinds, profile))


class PysnmpHealthPoller:
    """One SnmpEngine per poller instance, NEVER one per walk (see CLAUDE.md —
    a per-walk engine leaks its UDP transport registration forever).

    Columns are walked BEST-EFFORT under one shared deadline: real fleets mix
    agents that answer HOST-RESOURCES but ignore ENTITY-SENSOR (each ignored
    subtree burns timeout x retries), so a dead subtree must cost only its own
    slice — never the device's whole reading. Whatever answered still parses."""

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_timeout_s
        # Stay inside the edge sweep's per-device cap with headroom to parse.
        self._budget_s = max(5.0, cfg.snmp_walk_timeout_s - 2.0)
        self._engine = None

    async def walk(self, target,
                   profiles: list[dict] | None = None) -> DeviceHealth:
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
        try:
            transport = await UdpTransportTarget.create(
                (target.ip, target.port), timeout=self._timeout, retries=1)
        except Exception as exc:
            raise RuntimeError(f"SNMP health walk of {target.ip} failed: {exc}") from exc

        async def one_column(column: str, out: list[tuple[str, str]]) -> None:
            async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                engine, community, transport, ContextData(),
                0, 25, ObjectType(ObjectIdentity(column)),
                lexicographicMode=False,
            ):
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                for name, val in binds:
                    out.append((str(name), val.prettyPrint()))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._budget_s
        varbinds: list[tuple[str, str]] = []

        async def walk_columns(columns) -> None:
            for column in columns:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.debug("health walk of %s ran out of budget at column %s",
                              target.ip, column)
                    return
                column_binds: list[tuple[str, str]] = []
                try:
                    await asyncio.wait_for(one_column(column, column_binds), remaining)
                except Exception as exc:
                    log.debug("health column %s on %s skipped: %s",
                              column, target.ip, exc)
                    continue
                varbinds.extend(column_binds)

        # sysObjectID first (one varbind) so the vendor profile is known up front;
        # its columns walk BEFORE the standard MIBs — on the boxes that need a
        # profile the standard subtrees are usually the dead ones, and a dead column
        # burns timeout x retries of the shared budget each.
        profile: dict | None = None
        if profiles:
            await walk_columns((OID_SYS_OBJECT_ID,))
            profile = match_profile(profiles, sys_object_id(varbinds))
            if profile is not None:
                await walk_columns(profile_walk_roots(profile))
        await walk_columns(WALK_COLUMNS)
        return parse_health(varbinds, profile)


def build_health_poller(cfg: Config = CONFIG) -> HealthPoller:
    return PysnmpHealthPoller(cfg)
