from __future__ import annotations

import ipaddress
import re

DEVICE_TYPES = ("core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul")
# Passive plant: splitters, fiber distribution boxes, splice closures. They live in
# org_devices (parent chains, map pins, routes — all shared machinery), but they
# don't ping: no IP, no probe assignment, no FSM. Three choke points keep them away
# from the monitoring path — org_device_topology (engine + /edge/devices),
# node_expected_ips (no assignment), device_reliability (no uptime math).
PASSIVE_TYPES = ("splitter", "fdb", "closure")
SNMP_VERSIONS = ("2c",)

def _gpon_vendors() -> frozenset[str]:
    from wisp.ingress.gpon import PROFILES
    return frozenset(PROFILES)

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

class InventoryError(ValueError):
    pass

def _str(data: dict, key: str, *, required: bool = False, default=None):
    v = data.get(key)
    v = v.strip() if isinstance(v, str) else (None if v is None else str(v).strip())
    if required and not v:
        raise InventoryError(f"{key.replace('_', ' ')} is required")
    return v or default

def clean_device_payload(data: dict, *, parents: dict[int, int | None],
                         device_id: int | None,
                         registered_nodes: set[str] | None = None,
                         passive_ids: set[int] | None = None) -> dict:
    name = _str(data, "name", required=True)
    device_type = _str(data, "device_type")
    if device_type and device_type not in DEVICE_TYPES + PASSIVE_TYPES:
        raise InventoryError(
            f"device type must be one of: {', '.join(DEVICE_TYPES + PASSIVE_TYPES)}")
    passive = device_type in PASSIVE_TYPES
    if passive:
        # a splitter has no address — the empty string satisfies the NOT NULL
        # column and never reaches an edge (org_device_topology filters passives)
        ip_address = _str(data, "ip_address") or ""
        if ip_address:
            raise InventoryError(
                f"a {device_type} is passive plant — it has no IP address")
    else:
        ip_address = _str(data, "ip_address", required=True)
        try:
            ipaddress.ip_address(str(ip_address))
        except ValueError:
            raise InventoryError(f"'{ip_address}' is not a valid IP address")
    region = _str(data, "region")

    parent_raw = data.get("parent_device_id")
    parent_id: int | None = None
    if parent_raw not in (None, "", "null"):
        try:
            parent_id = int(parent_raw)
        except (TypeError, ValueError):
            raise InventoryError("parent node is invalid")
        if parent_id not in parents:
            raise InventoryError("parent node does not exist")
        if parent_id == device_id:
            raise InventoryError("a node can't be its own parent")
        cur, seen = parent_id, set()
        while cur is not None:
            if cur == device_id:
                raise InventoryError("that parent would create a topology loop")
            if cur in seen:
                break
            seen.add(cur)
            cur = parents.get(cur)
        # a passive has no FSM state, so suppression through it is undefined —
        # monitored gear may not hang below plant (plant hangs below gear)
        if (not passive and passive_ids is not None and parent_id in passive_ids):
            raise InventoryError(
                "a monitored device can't sit under passive plant — "
                "parent it to the powered device above instead")

    assigned_node_id = _str(data, "assigned_node_id")
    if passive and assigned_node_id:
        raise InventoryError(f"a {device_type} is passive plant — nothing probes it")
    if (assigned_node_id and registered_nodes is not None
            and assigned_node_id not in registered_nodes):
        raise InventoryError("assigned wisp client does not exist")

    gpon_vendor = _str(data, "gpon_vendor")
    if gpon_vendor:
        gpon_vendor = gpon_vendor.lower()
        if device_type != "OLT":
            raise InventoryError("GPON vendor only applies to an OLT")
        if gpon_vendor not in _gpon_vendors():
            raise InventoryError(
                f"GPON vendor must be one of: {', '.join(sorted(_gpon_vendors()))}")

    # which PON a splitter/FDB serves — the fault localizer binds passives to
    # onu_optics rows through it (Phase D3); free-form, e.g. "0/6"
    pon_port = _str(data, "pon_port") if passive else None
    if pon_port and len(pon_port) > 32:
        raise InventoryError("PON port must be 32 characters or fewer")

    return {"name": name, "ip_address": ip_address, "device_type": device_type,
            "region": region, "parent_device_id": parent_id,
            "assigned_node_id": assigned_node_id, "gpon_vendor": gpon_vendor,
            "pon_port": pon_port}

def clean_location_payload(data: dict) -> dict:
    """Map pin for a device: both coordinates, or both null (= remove the pin)."""
    lat_raw, lng_raw = data.get("lat"), data.get("lng")
    if lat_raw in (None, "", "null") and lng_raw in (None, "", "null"):
        return {"lat": None, "lng": None}
    try:
        lat, lng = float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        raise InventoryError("lat and lng must both be numbers (or both null to clear)")
    if not (-90.0 <= lat <= 90.0):
        raise InventoryError("lat must be between -90 and 90")
    if not (-180.0 <= lng <= 180.0):
        raise InventoryError("lng must be between -180 and 180")
    # ~1e-6° ≈ 0.1 m — anything longer is float noise from a drag event
    return {"lat": round(lat, 6), "lng": round(lng, 6)}

ROUTE_MAX_WAYPOINTS = 200

def clean_route_payload(data: dict) -> dict:
    """Drawn cable path for a link: intermediate vertices only, parent→child order.

    An empty waypoint list clears the route. Endpoint devices are validated by the
    caller (the pair must be a real link in this org)."""
    try:
        child_id = int(data.get("child_id"))
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        raise InventoryError("child_id and parent_id are required")
    raw = data.get("waypoints")
    if raw in (None, "", "null"):
        raw = []
    if not isinstance(raw, list):
        raise InventoryError("waypoints must be a list of [lat, lng] pairs")
    if len(raw) > ROUTE_MAX_WAYPOINTS:
        raise InventoryError(f"a route can hold at most {ROUTE_MAX_WAYPOINTS} waypoints")
    waypoints: list[list[float]] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise InventoryError("each waypoint must be a [lat, lng] pair")
        try:
            lat, lng = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            raise InventoryError("waypoint coordinates must be numbers")
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            raise InventoryError("waypoint coordinates are out of range")
        waypoints.append([round(lat, 6), round(lng, 6)])
    return {"child_id": child_id, "parent_id": parent_id, "waypoints": waypoints}

def clean_region_name(raw) -> str:
    name = str(raw).strip() if raw is not None else ""
    if not name:
        raise InventoryError("region name is required")
    if len(name) > 64:
        raise InventoryError("region name must be 64 characters or fewer")
    return name

def clean_backup_link(child_id: int, parent_id: int, *,
                      parents: dict[int, int | None],
                      backups: dict[int, set[int]]) -> None:
    if child_id not in parents:
        raise InventoryError("node not found")
    if parent_id not in parents:
        raise InventoryError("backup parent does not exist")
    if parent_id == child_id:
        raise InventoryError("a node can't be its own backup parent")
    if parents.get(child_id) == parent_id:
        raise InventoryError("that node is already the primary parent")
    if parent_id in backups.get(child_id, set()):
        raise InventoryError("that backup link already exists")
    edges_of: dict[int, set[int]] = {}
    for cid, pid in parents.items():
        if pid is not None:
            edges_of.setdefault(cid, set()).add(pid)
    for cid, pids in backups.items():
        edges_of.setdefault(cid, set()).update(pids)
    stack, seen = [parent_id], set()
    while stack:
        cur = stack.pop()
        if cur == child_id:
            raise InventoryError("that backup link would create a topology loop")
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(edges_of.get(cur, ()))

BW_DIRECTIONS = ("in", "out", "either", "total")

def _clean_bw_bound(data: dict, key: str) -> float | None:
    raw = data.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InventoryError(f"{key} must be a number")
    if value <= 0:
        raise InventoryError(f"{key} must be positive")
    return value

def clean_port_bandwidth_payload(data: dict) -> dict:
    threshold = _clean_bw_bound(data, "threshold_mbps")
    max_mbps = _clean_bw_bound(data, "max_mbps")
    if threshold is not None and max_mbps is not None and max_mbps <= threshold:
        raise InventoryError("max_mbps must be greater than threshold_mbps")
    direction = (str(data.get("direction") or "either")).strip().lower()
    if direction not in BW_DIRECTIONS:
        raise InventoryError(f"direction must be one of: {', '.join(BW_DIRECTIONS)}")
    return {"threshold_mbps": threshold, "max_mbps": max_mbps, "direction": direction}

def _clean_dbm(data: dict, key: str) -> float | None:
    raw = data.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InventoryError(f"{key} must be a number")
    if value > 0:
        raise InventoryError(f"{key} must be negative (dBm, e.g. -27)")
    return value

def clean_optical_thresholds(data: dict) -> dict:
    warn = _clean_dbm(data, "warn_dbm")
    crit = _clean_dbm(data, "crit_dbm")
    if warn is not None and crit is not None and crit > warn:
        raise InventoryError("crit_dbm must be lower (weaker) than warn_dbm")
    return {"warn_dbm": warn, "crit_dbm": crit}

def clean_ack_until(data: dict) -> str | None:
    from datetime import datetime, timedelta, timezone
    raw = data.get("until")
    if raw in (None, "", "null", "clear") and data.get("hours") in (None, "", "null"):
        return None
    hours = data.get("hours")
    if hours not in (None, "", "null"):
        try:
            h = float(hours)
        except (TypeError, ValueError):
            raise InventoryError("hours must be a number")
        if h <= 0:
            raise InventoryError("hours must be positive")
        return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(raw)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        raise InventoryError("until must be an ISO8601 timestamp")

def clean_snmp_payload(data: dict) -> dict:
    enabled = 0 if str(data.get("snmp_enabled", 0)) in ("0", "false", "False", "", "None") else 1
    version = (str(data.get("snmp_version") or "2c")).strip().lower()
    if version not in SNMP_VERSIONS:
        raise InventoryError(f"SNMP version must be one of: {', '.join(SNMP_VERSIONS)}")
    community = _str(data, "snmp_community")
    if enabled and not community:
        raise InventoryError("an SNMP community is required to enable SNMP")
    try:
        port = int(data.get("snmp_port") or 161)
    except (TypeError, ValueError):
        raise InventoryError("SNMP port must be a number")
    if not (1 <= port <= 65535):
        raise InventoryError("SNMP port must be 1–65535")
    return {"snmp_enabled": enabled, "snmp_version": version,
            "snmp_community": community, "snmp_port": port}

_OID_RE = re.compile(r"^\d+(\.\d+){0,127}$")

# Diagnostic walk bounds: a full enterprise-tree walk of a loaded OLT can run to
# hundreds of thousands of varbinds — the cap keeps one click from turning into a
# multi-megabyte upload and a minutes-long UDP storm inside the customer's network.
WALK_DEFAULT_MAX_VARBINDS = 2000
WALK_CAP_MAX_VARBINDS = 20000

def clean_oid(raw, *, default: str | None = None, field: str = "oid") -> str:
    oid = str(raw or "").strip().strip(".")
    if not oid and default:
        return default
    if not _OID_RE.match(oid):
        raise InventoryError(
            f"{field} must be a dotted numeric OID, e.g. 1.3.6.1.4.1")
    return oid

def clean_walk_payload(data: dict) -> dict:
    root_oid = clean_oid(data.get("root_oid"), default="1.3.6.1", field="root_oid")
    raw_max = data.get("max_varbinds")
    if raw_max in (None, "", "null"):
        max_varbinds = WALK_DEFAULT_MAX_VARBINDS
    else:
        try:
            max_varbinds = int(raw_max)
        except (TypeError, ValueError):
            raise InventoryError("max_varbinds must be a number")
        if max_varbinds <= 0:
            raise InventoryError("max_varbinds must be positive")
    return {"root_oid": root_oid,
            "max_varbinds": min(max_varbinds, WALK_CAP_MAX_VARBINDS)}

# Subsystems an operator can mark "not supported by this hardware" — mirrors
# store.SNMP_SUBSYSTEMS (the edge's snmp_status vocabulary).
CAPABILITY_SUBSYSTEMS = ("health", "ports", "optics")

def clean_capability_payload(data: dict) -> dict:
    try:
        device_id = int(data.get("device_id"))
    except (TypeError, ValueError):
        raise InventoryError("device_id required")
    subsystem = str(data.get("subsystem") or "").strip().lower()
    if subsystem not in CAPABILITY_SUBSYSTEMS:
        raise InventoryError(
            f"subsystem must be one of: {', '.join(CAPABILITY_SUBSYSTEMS)}")
    supported = str(data.get("supported", 1)) not in ("0", "false", "False", "", "None")
    note = str(data.get("note") or "").strip()[:200] or None
    return {"device_id": device_id, "subsystem": subsystem,
            "supported": supported, "note": note}

# The closed decode/select vocabulary the edge's profile interpreter understands
# (ingress/health.py). Deliberately tiny — a vendor encoding this can't express is
# the rare case that still warrants edge code, not a reason to grow this into a DSL.
PROFILE_METRICS = ("cpu_pct", "mem_pct", "mem_used_bytes", "mem_total_bytes", "temp_c")
PROFILE_DECODES = ("as_is", "div10", "div100", "signed_div100")
PROFILE_SELECTS = ("first", "avg", "max", "sum")

def clean_profile_payload(data: dict) -> dict:
    name = _str(data, "name", required=True)
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    match = clean_oid(data.get("match_sysobjectid"), field="match_sysobjectid")
    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, dict) or not raw_metrics:
        raise InventoryError("metrics must map at least one metric to an OID")
    metrics: dict = {}
    for key, spec in raw_metrics.items():
        if key not in PROFILE_METRICS:
            raise InventoryError(
                f"unknown metric {key!r} — must be one of: {', '.join(PROFILE_METRICS)}")
        if not isinstance(spec, dict):
            raise InventoryError(f"metric {key!r} must be an object with an oid")
        oid = clean_oid(spec.get("oid"), field=f"{key}.oid")
        decode = (str(spec.get("decode") or "as_is")).strip().lower()
        if decode not in PROFILE_DECODES:
            raise InventoryError(
                f"{key}.decode must be one of: {', '.join(PROFILE_DECODES)}")
        select = (str(spec.get("select") or "first")).strip().lower()
        if select not in PROFILE_SELECTS:
            raise InventoryError(
                f"{key}.select must be one of: {', '.join(PROFILE_SELECTS)}")
        metrics[key] = {"oid": oid, "decode": decode, "select": select}
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    return {"name": name, "match_sysobjectid": match, "metrics": metrics,
            "enabled": enabled}

def clean_node_id(raw) -> str:
    node_id = str(raw or "").strip()
    if not node_id:
        raise InventoryError("node id is required")
    if not _NODE_ID_RE.match(node_id):
        raise InventoryError(
            "node id must be 1-64 characters, starting with a letter or digit, and "
            "contain only letters, digits, '.', '_', or '-'")
    return node_id

def clean_org_id(raw) -> str:
    org_id = str(raw or "").strip()
    if not org_id:
        raise InventoryError("org id is required")
    if not _NODE_ID_RE.match(org_id):
        raise InventoryError(
            "org id must be 1-64 characters, starting with a letter or digit, and "
            "contain only letters, digits, '.', '_', or '-'")
    return org_id
