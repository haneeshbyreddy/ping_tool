from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from wisp.config import CONFIG, Config
from wisp.ingress.health import OID_SYS_OBJECT_ID, sys_object_id
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
    oid_rx: str = ""
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
    oid_ident_state: str = ""
    oid_ident_distance: str = ""
    oid_ident_name: str = ""
    format_pon_label: Callable[[str], str] = lambda pon: pon
    derive_onu_id: Callable[[str], int | None] | None = None
    match_sysobjectid: str = ""

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
    match_sysobjectid="1.3.6.1.4.1.2011",
)

def _dbc_state(raw: str) -> str:
    s = str(raw).strip().lower()
    if s in ("1", "online"):
        return STATE_ONLINE
    if s in ("0", "offline"):
        return STATE_OFFLINE
    return STATE_ONLINE

DBC = GponProfile(
    name="dbc",
    rx_scale=1.0,
    distance_scale=1.0,
    decode_state=_dbc_state,
    oid_ident_key="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.6",
    oid_ident_pon="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.2",
    oid_ident_onu="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.3",
    oid_ident_state="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.5",
    oid_ident_distance="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.13",
    oid_ident_name="1.3.6.1.4.1.37950.1.1.5.12.1.12.1.10",
    format_pon_label=lambda pon: f"EPON0/{pon}",
    match_sysobjectid="1.3.6.1.4.1.37950",
)

PROFILES: dict[str, GponProfile] = {HUAWEI.name: HUAWEI, DBC.name: DBC}

_STATE_VOCAB = (STATE_ONLINE, STATE_OFFLINE, STATE_DYING_GASP, STATE_LOS, STATE_UNKNOWN)
def _packed_ifindex(idx: str) -> int | None:

    head = (idx or "").split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _packed_pon(idx: str) -> str:
    n = _packed_ifindex(idx)
    return str((n >> 8) & 0xFF) if n is not None else idx


def _packed_onu(idx: str) -> int | None:
    n = _packed_ifindex(idx)
    return (n & 0xFF) if n is not None else None


_PON_INDEX_STRATEGIES: dict[str, Callable[[str], str]] = {
    "as_is": lambda idx: idx,
    "first_segment": lambda idx: idx.split(".", 1)[0] if idx else idx,
    "packed_ifindex": _packed_pon,
}
_ONU_INDEX_STRATEGIES: dict[str, Callable[[str], int | None]] = {
    "packed_ifindex": _packed_onu,
}
_PROFILE_OID_FIELDS = ("rx", "tx", "state", "distance", "serial", "name",
                       "ident_key", "ident_pon", "ident_onu", "ident_state",
                       "ident_distance", "ident_name")
_OID_RE = re.compile(r"^\d+(\.\d+)+$")

def _state_decoder(state_map: dict[str, str], default: str) -> Callable[[str], str]:
    norm = {str(k).strip().lower(): v for k, v in state_map.items()}
    return lambda raw: norm.get(str(raw).strip().lower(), default)

def _label_formatter(template: str) -> Callable[[str], str]:
    return lambda pon: template.replace("{pon}", str(pon))

def gpon_profile_from_dict(raw: dict) -> GponProfile | None:
    try:
        name = str(raw.get("name") or "").strip().lower()
        if not name:
            raise ValueError("profile has no name")
        match = str(raw.get("match_sysobjectid") or "").strip().strip(".")
        if match and not _OID_RE.match(match):
            raise ValueError(f"match_sysobjectid {match!r} is not an OID prefix")
        oids: dict[str, str] = {}
        for key, val in (raw.get("oids") or {}).items():
            if key not in _PROFILE_OID_FIELDS:
                raise ValueError(f"unknown oid field {key!r}")
            oid = str(val or "").strip().strip(".")
            if not oid:
                continue
            if not _OID_RE.match(oid):
                raise ValueError(f"oids.{key} {oid!r} is not an OID")
            oids[key] = oid
        if not oids:
            raise ValueError("profile maps no OIDs")
        scales: dict[str, float] = {}
        for key, val in (raw.get("scales") or {}).items():
            if key not in ("rx", "tx", "distance"):
                raise ValueError(f"unknown scale {key!r}")
            f = float(val)
            if not 0 < f <= 1000:
                raise ValueError(f"scales.{key} out of range")
            scales[key] = f
        state_map = raw.get("state_map") or {}
        if not isinstance(state_map, dict):
            raise ValueError("state_map must be an object")
        for k, v in state_map.items():
            if v not in _STATE_VOCAB:
                raise ValueError(f"state_map[{k!r}]={v!r} not in {_STATE_VOCAB}")
        default_state = raw.get("state_default", STATE_UNKNOWN)
        if default_state not in _STATE_VOCAB:
            raise ValueError(f"state_default {default_state!r} not in {_STATE_VOCAB}")
        pon_index = str(raw.get("pon_index") or "as_is").strip().lower()
        if pon_index not in _PON_INDEX_STRATEGIES:
            raise ValueError(
                f"pon_index {pon_index!r} not in {tuple(_PON_INDEX_STRATEGIES)}")
        pon_label = str(raw.get("pon_label") or "").strip()
        if pon_label and "{pon}" not in pon_label:
            raise ValueError("pon_label template must contain '{pon}'")
    except (ValueError, TypeError, AttributeError) as exc:
        log.warning("rejecting central GPON profile %r: %s — optics stay off for"
                    " any OLT it would have claimed, never guessed",
                    raw.get("name") if isinstance(raw, dict) else raw, exc)
        return None
    return GponProfile(
        name=name,
        oid_rx=oids.get("rx", ""), oid_tx=oids.get("tx", ""),
        oid_state=oids.get("state", ""), oid_distance=oids.get("distance", ""),
        oid_serial=oids.get("serial", ""), oid_name=oids.get("name", ""),
        rx_scale=scales.get("rx", 0.01), tx_scale=scales.get("tx", 0.01),
        distance_scale=scales.get("distance", 1.0),
        decode_state=_state_decoder(state_map, default_state),
        format_pon=_PON_INDEX_STRATEGIES[pon_index],
        derive_onu_id=_ONU_INDEX_STRATEGIES.get(pon_index),
        oid_ident_key=oids.get("ident_key", ""), oid_ident_pon=oids.get("ident_pon", ""),
        oid_ident_onu=oids.get("ident_onu", ""),
        oid_ident_state=oids.get("ident_state", ""),
        oid_ident_distance=oids.get("ident_distance", ""),
        oid_ident_name=oids.get("ident_name", ""),
        format_pon_label=(_label_formatter(pon_label) if pon_label
                          else (lambda pon: pon)),
        match_sysobjectid=match,
    )

def match_gpon_profile(sysobjectid: str | None,
                       extra: dict[str, GponProfile] | None = None) -> GponProfile | None:

    soid = (sysobjectid or "").strip().strip(".")
    if not soid:
        return None
    extra = extra or {}
    candidates = ([(p, False) for p in PROFILES.values() if p.name not in extra]
                  + [(p, True) for p in extra.values()])
    best: GponProfile | None = None
    best_len = -1
    for p, wins_ties in candidates:
        prefix = p.match_sysobjectid.strip().strip(".")
        if not prefix:
            continue
        if soid == prefix or soid.startswith(prefix + "."):
            if len(prefix) > best_len or (wins_ties and len(prefix) == best_len):
                best, best_len = p, len(prefix)
    return best

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

def _derive_onu_id(idx: str) -> int | None:
    parts = idx.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return int(idx) if idx.isdigit() else None

def _as_int(raw) -> int | None:
    return int(raw) if (raw not in (None, "") and str(raw).isdigit()) else None

def _place(oid: str, val: str, cols: dict[str, str], out: dict[str, dict]) -> bool:
    for prefix, fieldname in cols.items():
        if not prefix:
            continue
        idx = _index_after(oid, prefix)
        if idx is not None:
            out.setdefault(idx, {})[fieldname] = val
            return True
    return False

def _clean_name(raw) -> str | None:
    s = (raw or "").strip()
    return None if s.upper() in ("", "NULL", "N/A", "NONE") else s

def _metric_state(cells: dict, profile: GponProfile) -> str:


    raw = cells.get("state")
    if profile.oid_state and raw is None:
        return STATE_UNKNOWN
    return profile.decode_state(raw if raw is not None else "")

def _ident_state(cells: dict, profile: GponProfile) -> str:
    """The absence guard, on the IDENT path this time.

    Same rule as `_metric_state` and it has to be stated twice because the two
    parse paths build their cells separately: an absent COLUMN is a firmware
    fact and takes the profile's default, but a missing VALUE in a column that
    EXISTS is a fact about the row and is `unknown`.

    Missing it here was live and it muted alarms. The ident path decoded
    `cells.get("state", "")`, and `_dbc_state` answers ONLINE to anything it
    does not recognise — so on the profile that sets `oid_ident_state` (dbc,
    the C-Data EPON fleet) a dropped state column read as "every ONU is up"
    and ponfault never saw the dark cohort. The metric path's own guard was
    added after the opposite failure (a truncated column faking a fibre cut);
    fixing one path and not the other is the same trap CLAUDE.md already
    records for ONU identity, which is why that rule says "on BOTH parse
    paths".
    """
    raw = cells.get("state")
    if profile.oid_ident_state and raw is None:
        return STATE_UNKNOWN
    return profile.decode_state(raw if raw is not None else "")

def _onu_from_metric(idx: str, cells: dict, profile: GponProfile) -> OnuOptic:
    serial = (cells.get("serial") or "").strip() or None
    return OnuOptic(
        onu_key=idx or serial or "?",
        pon_port=profile.format_pon(idx),
        onu_id=(profile.derive_onu_id or _derive_onu_id)(idx),
        name=_clean_name(cells.get("name")),
        serial=serial,
        state=_metric_state(cells, profile),
        rx_dbm=_to_float(cells.get("rx"), profile.rx_scale),
        tx_dbm=_to_float(cells.get("tx"), profile.tx_scale),
        distance_m=_to_int(cells.get("distance"), profile.distance_scale),
    )

def parse_onu_table(varbinds: list[tuple[str, str]], profile: GponProfile) -> list[OnuOptic]:
    metric_cols = {
        profile.oid_rx: "rx", profile.oid_tx: "tx", profile.oid_state: "state",
        profile.oid_distance: "distance", profile.oid_serial: "serial",
        profile.oid_name: "name",
    }
    ident_cols = {
        profile.oid_ident_key: "key", profile.oid_ident_pon: "pon",
        profile.oid_ident_onu: "onu", profile.oid_ident_state: "state",
        profile.oid_ident_distance: "distance", profile.oid_ident_name: "name",
    }
    metric: dict[str, dict] = {}
    ident: dict[str, dict] = {}
    for oid, val in varbinds:
        if not _place(oid, val, metric_cols, metric):
            _place(oid, val, ident_cols, ident)

    if not (profile.oid_ident_key and ident):
        return [_onu_from_metric(idx, cells, profile) for idx, cells in metric.items()]

    opt_by_mac: dict[str, list[tuple[str, dict]]] = {}
    opt_by_mac_onu: dict[tuple[str, int], tuple[str, dict]] = {}
    for midx, cells in metric.items():
        mac = _mac_norm(cells.get("serial", ""))
        if not mac:
            continue
        opt_by_mac.setdefault(mac, []).append((midx, cells))
        onu = _derive_onu_id(midx)
        if onu is not None:
            opt_by_mac_onu.setdefault((mac, onu), (midx, cells))

    consumed: set[str] = set()
    out: list[OnuOptic] = []
    for cells in ident.values():
        mac_raw = (cells.get("key") or "").strip()
        mac = _mac_norm(mac_raw)
        onu_id = _as_int(cells.get("onu"))
        pon = cells.get("pon")
        match = None
        if mac:
            exact = opt_by_mac_onu.get((mac, onu_id)) if onu_id is not None else None
            if exact and exact[0] not in consumed:
                match = exact
            else:
                match = next((c for c in opt_by_mac.get(mac, []) if c[0] not in consumed),
                             None)
        ocells: dict = {}
        if match:
            consumed.add(match[0])
            ocells = match[1]
        pon_port = (profile.format_pon_label(str(pon)) if pon not in (None, "")
                    else profile.format_pon(str(onu_id) if onu_id is not None else ""))
        out.append(OnuOptic(
            onu_key=(f"{pon}.{onu_id}" if (pon not in (None, "") and onu_id is not None)
                     else (mac_raw.upper() or str(pon or onu_id or "?"))),
            pon_port=pon_port,
            onu_id=onu_id,
            name=_clean_name(cells.get("name")),
            serial=(mac_raw.upper() or None),
            state=_ident_state(cells, profile),
            rx_dbm=_to_float(ocells.get("rx"), profile.rx_scale),
            tx_dbm=_to_float(ocells.get("tx"), profile.tx_scale),
            distance_m=_to_int(cells.get("distance"), profile.distance_scale),
        ))
    for midx, cells in metric.items():
        if midx not in consumed:
            out.append(_onu_from_metric(midx, cells, profile))
    return out

class PysnmpGponPoller:

    def __init__(self, profile: GponProfile, cfg: Config = CONFIG) -> None:
        self.profile = profile
        self._timeout = cfg.gpon_request_timeout_s or cfg.snmp_timeout_s
        self._retries = max(0, cfg.gpon_request_retries)
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
                               p.oid_ident_key, p.oid_ident_pon, p.oid_ident_onu,
                               p.oid_ident_state, p.oid_ident_distance,
                               p.oid_ident_name) if c]
        if self._engine is None:
            self._engine = SnmpEngine()
        engine = self._engine
        community = CommunityData(target.community, mpModel=1)
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=self._retries)
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

_DETECT_TTL_S = 3600.0
_DETECT_RETRY_S = 900.0

class PysnmpSysObjectIdReader:

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_timeout_s
        self._engine = None

    async def read(self, target: SnmpTarget) -> str | None:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_walk_cmd,
            )
        except ImportError as exc:
            raise RuntimeError("GPON auto-detect needs 'pysnmp' (pip install pysnmp).") from exc
        if self._engine is None:
            self._engine = SnmpEngine()
        community = CommunityData(target.community, mpModel=1)
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=1)
        varbinds: list[tuple[str, str]] = []
        async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
            self._engine, community, transport, ContextData(),
            0, 2, ObjectType(ObjectIdentity(OID_SYS_OBJECT_ID)),
            lexicographicMode=False,
        ):
            if errInd or errStat:
                raise RuntimeError(
                    f"sysObjectID read of {target.ip} failed: {errInd or errStat}")
            for name, val in binds:
                varbinds.append((str(name), val.prettyPrint()))
        return sys_object_id(varbinds)

class GponPollerPool:


    def __init__(self, cfg: Config = CONFIG,
                 factory: Callable[[GponProfile, Config], GponPoller] = PysnmpGponPoller,
                 detector=None):
        self._cfg = cfg
        self._factory = factory
        self._fallback = (getattr(cfg, "gpon_vendor", "") or "").strip().lower()
        self._pollers: dict[str, GponPoller] = {}
        self._detector = detector
        self._detected: dict[object, tuple[str | None, float]] = {}
        self._central: dict[str, GponProfile] = {}
        self._central_fp: str | None = None

    def set_profiles(self, raw: list[dict] | None) -> None:
        if raw is None:
            return
        fp = json.dumps(raw, sort_keys=True, default=str)
        if fp == self._central_fp:
            return
        self._central_fp = fp
        parsed: dict[str, GponProfile] = {}
        for d in raw:
            p = gpon_profile_from_dict(d) if isinstance(d, dict) else None
            if p is not None:
                parsed[p.name] = p
        stale = set(self._central) | set(parsed)
        self._central = parsed
        for name in stale:
            self._pollers.pop(name, None)
        log.info("central GPON profiles installed: %s",
                 ", ".join(sorted(parsed)) or "(none)")

    def _profile_named(self, name: str) -> GponProfile | None:
        return self._central.get(name) or PROFILES.get(name)

    def for_vendor(self, vendor: str | None) -> GponPoller | None:
        name = (vendor or "").strip().lower() or self._fallback
        if not name:
            return None
        profile = self._profile_named(name)
        if profile is None:
            log.warning("unknown GPON vendor %r; optics skipped — never guess OIDs", name)
            return None
        return self._poller_for(profile)

    async def resolve(self, device: dict, target: SnmpTarget) -> GponPoller | None:
        poller, _ = await self.resolve_info(device, target)
        return poller

    async def resolve_info(
        self, device: dict, target: SnmpTarget,
    ) -> tuple[GponPoller | None, dict]:
        vendor = (device.get("gpon_vendor") or "").strip().lower() or self._fallback
        if vendor:
            poller = self.for_vendor(vendor)
            return poller, {"vendor": vendor, "sysobjectid": None,
                            "reason": "override" if poller else "no_profile"}
        soid = await self._sysobjectid(device.get("id") or target.ip, target)
        if soid is None:
            return None, {"vendor": None, "sysobjectid": None, "reason": "no_response"}
        profile = match_gpon_profile(soid, self._central)
        if profile is None:
            log.debug("OLT %s (%s): sysObjectID %r matches no GPON profile; optics off",
                      device.get("id"), target.ip, soid)
            return None, {"vendor": None, "sysobjectid": soid, "reason": "no_profile"}
        return self._poller_for(profile), {"vendor": profile.name, "sysobjectid": soid,
                                           "reason": "matched"}

    def _poller_for(self, profile: GponProfile) -> GponPoller:
        poller = self._pollers.get(profile.name)
        if poller is None:
            poller = self._factory(profile, self._cfg)
            self._pollers[profile.name] = poller
        return poller

    async def _sysobjectid(self, key, target: SnmpTarget) -> str | None:
        now = time.monotonic()
        cached = self._detected.get(key)
        if cached is not None:
            soid, at = cached
            if now - at < (_DETECT_TTL_S if soid else _DETECT_RETRY_S):
                return soid
        if self._detector is None:
            self._detector = PysnmpSysObjectIdReader(self._cfg)
        try:
            soid = await self._detector.read(target)
        except Exception as exc:
            log.debug("sysObjectID detect failed for %s: %s", target.ip, exc)
            soid = None
        self._detected[key] = (soid, now)
        return soid
