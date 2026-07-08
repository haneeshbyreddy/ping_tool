from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from wisp.config import CONFIG, Config
from wisp.ingress.snmp import SnmpTarget

log = logging.getLogger(__name__)

STATE_ONLINE = "online"
STATE_OFFLINE = "offline"
STATE_DYING_GASP = "dying_gasp"
STATE_LOS = "los"
STATE_UNKNOWN = "unknown"

@dataclass(frozen=True)
class OnuOptic:
    onu_key: str
    pon_port: str | None = None
    onu_id: int | None = None
    name: str | None = None
    serial: str | None = None
    state: str = STATE_UNKNOWN
    rx_dbm: float | None = None
    tx_dbm: float | None = None
    olt_rx_dbm: float | None = None
    distance_m: int | None = None

    def to_wire(self) -> dict:
        return {
            "onu_key": self.onu_key, "pon_port": self.pon_port, "onu_id": self.onu_id,
            "name": self.name, "serial": self.serial, "state": self.state,
            "rx_dbm": self.rx_dbm, "tx_dbm": self.tx_dbm, "olt_rx_dbm": self.olt_rx_dbm,
            "distance_m": self.distance_m,
        }

@dataclass(frozen=True)
class GponProfile:
    name: str
    oid_rx: str
    oid_tx: str = ""
    oid_state: str = ""
    oid_distance: str = ""
    oid_serial: str = ""
    oid_name: str = ""
    rx_scale: float = 0.01
    tx_scale: float = 0.01
    distance_scale: float = 1.0
    decode_state: Callable[[str], str] = lambda raw: STATE_UNKNOWN
    format_pon: Callable[[str], str] = lambda idx: idx
    oid_ident_key: str = ""
    oid_ident_pon: str = ""
    oid_ident_onu: str = ""
    format_pon_label: Callable[[str], str] = lambda pon: pon

def _huawei_state(raw: str) -> str:
    m = {"1": STATE_ONLINE, "2": STATE_OFFLINE, "online": STATE_ONLINE,
         "offline": STATE_OFFLINE, "los": STATE_LOS, "dyinggasp": STATE_DYING_GASP}
    return m.get(str(raw).strip().lower(), STATE_UNKNOWN)

def _huawei_pon(idx: str) -> str:
    return idx.split(".", 1)[0] if idx else idx

HUAWEI = GponProfile(
    name="huawei",
    oid_rx="1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
    oid_tx="1.3.6.1.4.1.2011.6.128.1.1.2.51.1.3",
    oid_state="1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
    oid_distance="1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
    oid_serial="1.3.6.1.4.1.2011.6.128.1.1.2.43.1.3",
    oid_name="1.3.6.1.4.1.2011.6.128.1.1.2.46.1.4",
    rx_scale=0.01, tx_scale=0.01, distance_scale=1.0,
    decode_state=_huawei_state, format_pon=_huawei_pon,
)

def _cdata_state(raw: str) -> str:
    return STATE_ONLINE

DBC = GponProfile(
    name="dbc",
    oid_rx="1.3.6.1.4.1.37950.1.1.5.12.1.28.1.3",
    oid_serial="1.3.6.1.4.1.37950.1.1.5.12.1.28.1.2",
    rx_scale=1.0,
    decode_state=_cdata_state,
    oid_ident_key="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.6",
    oid_ident_pon="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.2",
    oid_ident_onu="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.3",
    format_pon_label=lambda pon: f"EPON0/{pon}",
)

PROFILES: dict[str, GponProfile] = {HUAWEI.name: HUAWEI, DBC.name: DBC}

class GponPoller(Protocol):
    async def walk(self, target: SnmpTarget) -> list[OnuOptic]: ...

def _to_float(raw, scale: float) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return round(int(str(raw).strip()) * scale, 2)
    except (TypeError, ValueError):
        try:
            return round(float(raw) * scale, 2)
        except (TypeError, ValueError):
            return None

def _to_int(raw, scale: float = 1.0) -> int | None:
    v = _to_float(raw, scale)
    return None if v is None else int(round(v))

def _index_after(oid: str, prefix: str) -> str | None:
    p = prefix if prefix.endswith(".") else prefix + "."
    if not oid.startswith(p):
        return None
    return oid[len(p):] or None

def _mac_norm(s: str) -> str:
    return re.sub(r"[^0-9a-f]", "", (s or "").lower())

def parse_onu_table(varbinds: list[tuple[str, str]], profile: GponProfile) -> list[OnuOptic]:
    cols = {
        profile.oid_rx: "rx", profile.oid_tx: "tx", profile.oid_state: "state",
        profile.oid_distance: "distance", profile.oid_serial: "serial",
        profile.oid_name: "name",
    }
    ident_cols = {
        profile.oid_ident_key: "key", profile.oid_ident_pon: "pon",
        profile.oid_ident_onu: "onu",
    }
    rows: dict[str, dict] = {}
    ident: dict[str, dict] = {}
    for oid, val in varbinds:
        placed = False
        for prefix, fieldname in cols.items():
            if not prefix:
                continue
            idx = _index_after(oid, prefix)
            if idx is not None:
                rows.setdefault(idx, {})[fieldname] = val
                placed = True
                break
        if placed:
            continue
        for prefix, fieldname in ident_cols.items():
            if not prefix:
                continue
            idx = _index_after(oid, prefix)
            if idx is not None:
                ident.setdefault(idx, {})[fieldname] = val
                break
    pair_pon: dict[tuple[str, int], str] = {}
    mac_rows: dict[str, list[tuple[str | None, str | None]]] = {}
    for cells in ident.values():
        k = _mac_norm(cells.get("key", ""))
        if not k:
            continue
        pon, onu = cells.get("pon"), cells.get("onu")
        mac_rows.setdefault(k, []).append((pon, onu))
        if pon not in (None, "") and onu not in (None, "") and str(onu).isdigit():
            pair_pon[(k, int(onu))] = str(pon)
    out: list[OnuOptic] = []
    for idx, cells in rows.items():
        parts = idx.rsplit(".", 1)
        onu_id = None
        if len(parts) == 2 and parts[1].isdigit():
            onu_id = int(parts[1])
        elif idx.isdigit():
            onu_id = int(idx)
        serial = (cells.get("serial") or "").strip() or None
        pon_port = profile.format_pon(idx)
        key = _mac_norm(serial) if serial else ""
        if key and mac_rows:
            pon = None
            if onu_id is not None and (key, onu_id) in pair_pon:
                pon = pair_pon[(key, onu_id)]
            else:
                only = mac_rows.get(key)
                if only and len(only) == 1:
                    pon, onu_raw = only[0]
                    if onu_raw not in (None, "") and str(onu_raw).isdigit():
                        onu_id = int(onu_raw)
            if pon not in (None, ""):
                pon_port = profile.format_pon_label(str(pon))
        out.append(OnuOptic(
            onu_key=serial or idx,
            pon_port=pon_port,
            onu_id=onu_id,
            name=(cells.get("name") or "").strip() or None,
            serial=serial,
            state=profile.decode_state(cells.get("state", "")),
            rx_dbm=_to_float(cells.get("rx"), profile.rx_scale),
            tx_dbm=_to_float(cells.get("tx"), profile.tx_scale),
            distance_m=_to_int(cells.get("distance"), profile.distance_scale),
        ))
    return out

class PysnmpGponPoller:

    def __init__(self, profile: GponProfile, cfg: Config = CONFIG) -> None:
        self.profile = profile
        self._timeout = cfg.snmp_timeout_s
        self._engine = None

    async def walk(self, target: SnmpTarget) -> list[OnuOptic]:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_walk_cmd,
            )
        except ImportError as exc:
            raise RuntimeError("GponPoller needs 'pysnmp' (pip install pysnmp).") from exc

        p = self.profile
        columns = [c for c in (p.oid_rx, p.oid_tx, p.oid_state, p.oid_distance,
                               p.oid_serial, p.oid_name,
                               p.oid_ident_key, p.oid_ident_pon, p.oid_ident_onu) if c]
        if self._engine is None:
            self._engine = SnmpEngine()
        engine = self._engine
        community = CommunityData(target.community, mpModel=1)
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=1)
        varbinds: list[tuple[str, str]] = []
        try:
            for column in columns:
                async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                    engine, community, transport, ContextData(),
                    0, 25, ObjectType(ObjectIdentity(column)),
                    lexicographicMode=False,
                ):
                    if errInd or errStat:
                        raise RuntimeError(
                            f"GPON walk of {target.ip} failed: {errInd or errStat}")
                    for name, val in binds:
                        varbinds.append((str(name), val.prettyPrint()))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"GPON walk of {target.ip} failed: {exc}") from exc
        return parse_onu_table(varbinds, p)

def _resolve_profile(vendor: str) -> GponProfile:
    profile = PROFILES.get(vendor)
    if profile is None:
        log.warning("unknown GPON vendor %r; falling back to huawei profile", vendor)
        return HUAWEI
    return profile

def build_gpon_poller(cfg: Config = CONFIG) -> GponPoller:
    vendor = (getattr(cfg, "gpon_vendor", "") or "huawei").lower()
    return PysnmpGponPoller(_resolve_profile(vendor), cfg)

class GponPollerPool:

    def __init__(self, cfg: Config = CONFIG,
                 factory: Callable[[GponProfile, Config], GponPoller] = PysnmpGponPoller):
        self._cfg = cfg
        self._factory = factory
        self._fallback = (getattr(cfg, "gpon_vendor", "") or "huawei").lower()
        self._pollers: dict[str, GponPoller] = {}
        self._resolved: dict[str, str] = {}

    def for_vendor(self, vendor: str | None) -> GponPoller:
        name = (vendor or "").strip().lower() or self._fallback
        key = self._resolved.get(name)
        if key is None:
            key = _resolve_profile(name).name
            self._resolved[name] = key
        poller = self._pollers.get(key)
        if poller is None:
            poller = self._factory(_resolve_profile(key), self._cfg)
            self._pollers[key] = poller
        return poller
