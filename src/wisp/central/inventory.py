from __future__ import annotations

import ipaddress
import re

from wisp.central.onuroster import _norm_mac
from wisp.central import fiber
from wisp.central.fiber import (
    FiberError, clean_cable_name, clean_core_no, clean_fiber_count)

DEVICE_TYPES = ("core", "router", "switch", "gateway", "OLT", "AP", "CPE",
                "backhaul", "nvr")
PASSIVE_TYPES = ("splitter", "coupler", "fdb", "closure")
SPLIT_RATIOS = (2, 4, 8, 16)
SPLIT_INPUTS = (1, 2)
SNMP_VERSIONS = ("2c",)

def _gpon_vendors(extra: set[str] | None = None) -> frozenset[str]:

    from wisp.ingress.gpon import PROFILES
    return frozenset(PROFILES) | frozenset(
        v.lower() for v in (extra or ()) if v)


def _nvr_vendors(extra: set[str] | None = None) -> frozenset[str]:

    from wisp.central.nvr_profiles import builtin_names
    return frozenset(builtin_names()) | frozenset(
        v.lower() for v in (extra or ()) if v)

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
                         passive_ids: set[int] | None = None,
                         gpon_vendors: set[str] | None = None,
                         nvr_vendors: set[str] | None = None) -> dict:
    name = _str(data, "name", required=True)
    device_type = _str(data, "device_type")
    if device_type and device_type not in DEVICE_TYPES + PASSIVE_TYPES:
        raise InventoryError(
            f"device type must be one of: {', '.join(DEVICE_TYPES + PASSIVE_TYPES)}")
    passive = device_type in PASSIVE_TYPES
    if passive:
        ip_address = _str(data, "ip_address") or ""
        if ip_address:
            raise InventoryError(
                f"a {device_type} is passive plant: it has no IP address")
    else:
        ip_address = _str(data, "ip_address", required=True)
        try:
            ipaddress.ip_address(str(ip_address))
        except ValueError:
            raise InventoryError(f"'{ip_address}' is not a valid IP address")
    region = _str(data, "region")
    tags_raw = data.get("tags")
    if isinstance(tags_raw, str):
        parts = tags_raw.split(",")
    elif isinstance(tags_raw, (list, tuple)):
        parts = [str(t) for t in tags_raw]
    else:
        parts = []
    tags: list[str] = []
    seen: set[str] = set()
    for t in parts:
        t = t.strip()
        if not t:
            continue
        if len(t) > 32:
            raise InventoryError("a tag must be 32 characters or fewer")
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        tags.append(t)
    if len(tags) > 8:
        raise InventoryError("at most 8 tags per device")

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
        if (not passive and passive_ids is not None and parent_id in passive_ids):
            raise InventoryError(
                "a monitored device can't sit under passive plant. "
                "Parent it to the powered device above instead.")

    assigned_node_id = _str(data, "assigned_node_id")
    if passive and assigned_node_id:
        raise InventoryError(f"a {device_type} is passive plant: nothing probes it")
    if (assigned_node_id and registered_nodes is not None
            and assigned_node_id not in registered_nodes):
        raise InventoryError("assigned wisp client does not exist")

    gpon_vendor = _str(data, "gpon_vendor")
    if gpon_vendor:
        gpon_vendor = gpon_vendor.lower()
        if device_type != "OLT":
            raise InventoryError("GPON vendor only applies to an OLT")
        known = _gpon_vendors(gpon_vendors)
        if gpon_vendor not in known:
            raise InventoryError(
                f"GPON vendor must be one of: {', '.join(sorted(known))}")

    nvr_vendor = _str(data, "nvr_vendor")
    if nvr_vendor:
        nvr_vendor = nvr_vendor.lower()
        if device_type != "nvr":
            raise InventoryError("NVR brand only applies to an NVR")
        known_nvr = _nvr_vendors(nvr_vendors)
        if nvr_vendor not in known_nvr:
            raise InventoryError(
                f"NVR brand must be one of: {', '.join(sorted(known_nvr))}")

    pon_port = _str(data, "pon_port") if passive else None
    if pon_port and len(pon_port) > 32:
        raise InventoryError("PON port must be 32 characters or fewer")

    split_ratio = _split_ratio(data) if passive else None
    split_inputs = _split_inputs(data, split_ratio) if passive else None

    onu_pon_limit = (_clean_onu_limit(data, "onu_pon_limit")
                     if device_type == "OLT" else None)

    return {"name": name, "ip_address": ip_address, "device_type": device_type,
            "region": region, "tags": ",".join(tags) or None,
            "parent_device_id": parent_id,
            "assigned_node_id": assigned_node_id, "gpon_vendor": gpon_vendor,
            "nvr_vendor": nvr_vendor,
            "pon_port": pon_port, "split_ratio": split_ratio,
            "split_inputs": split_inputs,
            "onu_pon_limit": onu_pon_limit}


def _split_ratio(data: dict) -> int | None:
    raw = data.get("split_ratio")
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, str):
        raw = raw.strip().replace("1:", "").replace("1/", "") or "0"
    try:
        ratio = int(raw)
    except (TypeError, ValueError):
        raise InventoryError("split ratio is invalid")
    if ratio not in SPLIT_RATIOS:
        raise InventoryError(
            "split ratio must be one of: "
            + ", ".join(f"1:{r}" for r in SPLIT_RATIOS))
    return ratio


def _split_inputs(data: dict, ratio: int | None) -> int | None:


    raw = data.get("split_inputs")
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, str):
        raw = raw.strip().split(":")[0] or "0"
    try:
        inputs = int(raw)
    except (TypeError, ValueError):
        raise InventoryError("split inputs is invalid")
    if inputs not in SPLIT_INPUTS:
        raise InventoryError(
            "split inputs must be one of: " + ", ".join(str(i) for i in SPLIT_INPUTS))
    if inputs > 1 and not ratio:
        raise InventoryError(
            "record the split ratio before recording a second input")
    return inputs if inputs > 1 else None

def clean_location_payload(data: dict) -> dict:
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
    return {"lat": round(lat, 6), "lng": round(lng, 6)}

PLACE_SOURCES = ("gps", "manual")

GPS_ACCURACY_HINT_M = 25.0
_MAX_ACCURACY_M = 10_000.0


def clean_field_location_payload(data: dict) -> dict:

    lat_raw, lng_raw = data.get("lat"), data.get("lng")
    if lat_raw in (None, "", "null") or lng_raw in (None, "", "null"):
        raise InventoryError("a field placement needs both lat and lng")
    loc = clean_location_payload(data)

    source = _str(data, "source") or "gps"
    if source not in PLACE_SOURCES:
        raise InventoryError("source must be one of: " + ", ".join(PLACE_SOURCES))

    acc_raw = data.get("accuracy_m")
    accuracy = None
    if acc_raw not in (None, "", "null"):
        try:
            accuracy = float(acc_raw)
        except (TypeError, ValueError):
            raise InventoryError("accuracy_m must be a number")
        if accuracy < 0 or accuracy > _MAX_ACCURACY_M:
            raise InventoryError("accuracy_m is out of range")
        accuracy = round(accuracy, 1)
    if source == "gps" and accuracy is None:
        source = "manual"

    return {"lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": accuracy, "source": source}


def clean_field_passive_payload(data: dict) -> dict:

    name = _str(data, "name", required=True)
    if len(name) > 120:
        raise InventoryError("name is too long")

    device_type = _str(data, "device_type", required=True)
    if device_type not in PASSIVE_TYPES:
        raise InventoryError(
            "field-created plant must be one of: " + ", ".join(PASSIVE_TYPES))

    loc = clean_field_location_payload(data)

    region = _str(data, "region")
    if region and len(region) > 120:
        raise InventoryError("region is too long")
    pon_port = _str(data, "pon_port")
    if pon_port and len(pon_port) > 60:
        raise InventoryError("PON label is too long")

    return {"name": name, "device_type": device_type,
            "ip_address": "", "parent_device_id": None,
            "assigned_node_id": None, "region": region,
            "pon_port": pon_port or None,
            "split_ratio": (ratio := _split_ratio(data)),
            "split_inputs": _split_inputs(data, ratio),
            "lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": loc["accuracy_m"], "source": loc["source"]}


def _onu_label(data: dict, *, required: bool = False) -> str | None:


    label = _str(data, "label", required=required)
    if label and len(label) > 120:
        raise InventoryError("label is too long")
    return label.upper() if label else None


_ONU_PHONE_RE = re.compile(r"^\+?\d{7,15}$")


def _onu_phone(data: dict, *, required: bool = False) -> str | None:
    raw = _str(data, "phone", required=required)
    if not raw:
        return None
    compact = re.sub(r"[\s\-().]", "", raw)
    if not _ONU_PHONE_RE.match(compact):
        raise InventoryError("enter a contact number of 7-15 digits, "
                             "e.g. 9876543210")
    return compact


def clean_field_onu_payload(data: dict) -> dict:


    mac = _norm_mac(_str(data, "mac", required=True))
    if not mac:
        raise InventoryError("a MAC is required")
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    loc = clean_field_location_payload(data)
    return {"mac": mac, "lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": loc["accuracy_m"], "source": loc["source"],
            "label": _onu_label(data, required=True),
            "phone": _onu_phone(data, required=True)}


def clean_field_onu_name_payload(data: dict) -> dict:


    mac = _norm_mac(_str(data, "mac", required=True))
    if not mac:
        raise InventoryError("a MAC is required")
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    return {"mac": mac, "label": _onu_label(data, required=True),
            "phone": _onu_phone(data, required=True)}


def clean_onu_contact_payload(data: dict) -> dict:


    mac = _norm_mac(_str(data, "mac", required=True))
    if not mac:
        raise InventoryError("a MAC is required")
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    notes = _str(data, "notes")
    if notes and len(notes) > 500:
        raise InventoryError("notes are too long")
    return {"mac": mac, "label": _onu_label(data), "phone": _onu_phone(data),
            "notes": notes or None}


def clean_onu_place_payload(data: dict) -> dict:


    mac = _norm_mac(_str(data, "mac", required=True))
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    loc = clean_location_payload(data)
    notes = _str(data, "notes")
    if notes and len(notes) > 500:
        raise InventoryError("notes are too long")
    return {"mac": mac, "lat": loc["lat"], "lng": loc["lng"],
            "label": _onu_label(data), "notes": notes,
            "phone": _onu_phone(data)}


def clean_onu_witness_payload(data: dict) -> dict:


    mac = _norm_mac(_str(data, "mac", required=True))
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    witness = data.get("witness")
    if not isinstance(witness, bool):
        raise InventoryError("witness must be true or false")
    return {"mac": mac, "witness": witness}


MAX_DROPS_PER_WRITE = 512


def clean_onu_drops_payload(data: dict, *,
                            split_ratio: int | None = None) -> dict:


    raw = data.get("macs")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("macs must be a list")
    macs: list[str] = []
    seen: set[str] = set()
    for m in raw:
        mac = _norm_mac(str(m))
        if not mac or mac in seen:
            continue
        if len(mac) > 64:
            raise InventoryError("MAC is too long")
        seen.add(mac)
        macs.append(mac)
    if not macs:
        raise InventoryError("no ONUs given")
    if len(macs) > MAX_DROPS_PER_WRITE:
        raise InventoryError(
            f"at most {MAX_DROPS_PER_WRITE} ONUs per request")

    passive_raw = data.get("passive_id")
    passive_id: int | None = None
    if passive_raw not in (None, "", "null"):
        try:
            passive_id = int(passive_raw)
        except (TypeError, ValueError):
            raise InventoryError("serving splitter is invalid")
    leg = data.get("leg_no")
    leg_no = None
    if leg not in (None, "", "null"):
        # `onu_drops.leg_no` stays an integer column — a leg is a numbered kind, so
        # its ref round-trips through the same bound check the fibre panel uses.
        leg_no = fiber.port_no(clean_port({"port_kind": "leg", "port_ref": leg},
                                          split_ratio=split_ratio)["port_ref"])
    return {"macs": macs, "passive_id": passive_id, "leg_no": leg_no}


def clean_web_access_payload(data: dict) -> dict:
    ip_raw = _str(data, "web_ip") or ""
    web_ip = ip_raw.strip()
    if web_ip:
        try:
            ipaddress.ip_address(web_ip)
        except ValueError:
            raise InventoryError(f"'{web_ip}' is not a valid IP address")

    port_raw = data.get("web_port")
    web_port: int | None = None
    if port_raw not in (None, "", "null"):
        try:
            web_port = int(port_raw)
        except (TypeError, ValueError):
            raise InventoryError("web port must be a whole number")
        if not (1 <= web_port <= 65535):
            raise InventoryError("web port must be between 1 and 65535")

    scheme_raw = (_str(data, "web_scheme") or "").strip().lower()
    if scheme_raw and scheme_raw not in ("http", "https"):
        raise InventoryError("web scheme must be http or https")
    web_scheme = scheme_raw or None

    return {"web_ip": web_ip or None, "web_port": web_port, "web_scheme": web_scheme}


_STD_WEB_PORTS = (80, 443)


def normalize_web_access(clean: dict, device_ip: str | None) -> dict:

    device_ip = (device_ip or "").strip()
    web_ip = clean.get("web_ip")
    web_port = clean.get("web_port")
    web_scheme = clean.get("web_scheme")
    distinct_ip = bool(web_ip) and web_ip != device_ip
    distinct_port = web_port is not None and web_port not in _STD_WEB_PORTS
    if distinct_ip:
        return {"web_ip": web_ip, "web_port": web_port, "web_scheme": web_scheme}
    if distinct_port:
        return {"web_ip": None, "web_port": web_port, "web_scheme": web_scheme}
    return {"web_ip": None, "web_port": None, "web_scheme": None}


ROUTE_MAX_WAYPOINTS = 200

PALETTE = ("violet", "magenta", "teal", "lime", "indigo", "chalk")

COLOR_KINDS = ("tag", "node")


def clean_color(raw) -> str | None:
    if raw in (None, "", "none", "null"):
        return None
    if raw in PALETTE:
        return raw
    raise InventoryError(f"colour must be one of: {', '.join(PALETTE)}")


def clean_color_key(kind: str, raw) -> str:
    if kind not in COLOR_KINDS:
        raise InventoryError("unknown colour kind")
    key = (raw or "").strip()
    if not key:
        raise InventoryError("nothing to colour")
    if len(key) > 64:
        raise InventoryError("name is too long")
    return key


def _waypoints(raw) -> list[list[float]]:

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
    return waypoints


def clean_route_payload(data: dict) -> dict:

    try:
        child_id = int(data.get("child_id"))
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        raise InventoryError("child_id and parent_id are required")
    return {"child_id": child_id, "parent_id": parent_id,
            "waypoints": _waypoints(data.get("waypoints"))}


def _cable_end(data: dict, side: str) -> dict | None:


    device_raw = data.get(f"{side}_device_id")
    mac_raw = data.get(f"{side}_mac")
    has_device = device_raw not in (None, "", "null")
    has_mac = mac_raw not in (None, "", "null")
    if has_device and has_mac:
        raise InventoryError(f"end {side.upper()} is a box or a customer, not both")
    if has_device:
        try:
            return {"device_id": int(device_raw), "mac": None}
        except (TypeError, ValueError):
            raise InventoryError(f"end {side.upper()} device id is invalid")
    if has_mac:
        mac = _norm_mac(str(mac_raw))
        if not mac:
            raise InventoryError(f"end {side.upper()} customer id is invalid")
        return {"device_id": None, "mac": mac}
    if f"{side}_device_id" in data or f"{side}_mac" in data:
        raise InventoryError(f"a cable needs a point at end {side.upper()}")
    return None


def clean_cable_payload(data: dict) -> dict:


    cable_id = data.get("id")
    if cable_id in (None, "", "null"):
        cable_id = None
    else:
        try:
            cable_id = int(cable_id)
        except (TypeError, ValueError):
            raise InventoryError("cable id is invalid")
    try:
        name = clean_cable_name(data.get("name"))
        cores = clean_fiber_count(data.get("cores"))
    except FiberError as exc:
        raise InventoryError(str(exc)) from exc
    notes = str(data.get("notes") or "").strip() or None
    if notes and len(notes) > 500:
        raise InventoryError("notes must be 500 characters or fewer")
    a, b = _cable_end(data, "a"), _cable_end(data, "b")
    if cable_id is None and (a is None or b is None):
        raise InventoryError("a cable runs between two points — name both ends")
    if (a is None) != (b is None):
        raise InventoryError("change both ends of a cable together, or neither")
    if a is not None and a == b:
        raise InventoryError("a cable cannot run from a point back to itself")
    return {"id": cable_id, "name": name, "cores": cores, "notes": notes,
            "a": a, "b": b}


def clean_cable_path_payload(data: dict) -> dict:


    try:
        cable_id = int(data.get("cable_id"))
    except (TypeError, ValueError):
        raise InventoryError("cable_id is required")
    path = _waypoints(data.get("path"))
    if len(path) == 1:
        raise InventoryError("a cable route needs at least two points")
    return {"cable_id": cable_id, "path": path}


def clean_cable_move_payload(data: dict) -> dict:


    frm, to = _cable_end(data, "from"), _cable_end(data, "to")
    if frm is None or to is None:
        raise InventoryError("a move names the point the fibres leave and the one"
                             " they land on")
    if (frm["device_id"], frm["mac"]) == (to["device_id"], to["mac"]):
        raise InventoryError("that is where these fibres already end")
    raw = data.get("cable_ids")
    if not isinstance(raw, list) or not raw:
        raise InventoryError("name at least one cable to move")
    ids: list[int] = []
    for r in raw:
        try:
            ids.append(int(r))
        except (TypeError, ValueError):
            raise InventoryError("cable id is invalid")
    return {"from": (frm["device_id"], frm["mac"]),
            "to": (to["device_id"], to["mac"]),
            "cable_ids": ids, "preview": bool(data.get("preview"))}


def clean_cable_split_payload(data: dict) -> dict:


    try:
        cable_id = int(data.get("cable_id"))
    except (TypeError, ValueError):
        raise InventoryError("cable_id is required")
    try:
        lat, lng = float(data.get("lat")), float(data.get("lng"))
    except (TypeError, ValueError):
        raise InventoryError("a split needs the point to cut at")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise InventoryError("split coordinates are out of range")
    name = str(data.get("name") or "").strip() or None
    if name and len(name) > 64:
        raise InventoryError("name must be 64 characters or fewer")
    return {"cable_id": cable_id, "lat": lat, "lng": lng, "name": name}


def _fibre_point(data: dict) -> dict:
    device_raw, mac_raw = data.get("device_id"), data.get("mac")
    has_device = device_raw not in (None, "", "null")
    has_mac = mac_raw not in (None, "", "null")
    if has_device == has_mac:
        raise InventoryError("a joint is made at a box or at a customer, not both")
    if has_device:
        try:
            return {"device_id": int(device_raw), "mac": None}
        except (TypeError, ValueError):
            raise InventoryError("device_id is invalid")
    mac = _norm_mac(str(mac_raw))
    if not mac:
        raise InventoryError("mac is invalid")
    return {"device_id": None, "mac": mac}


def clean_port(data: dict, *, split_ratio: int | None = None,
               split_inputs: int | None = None, prefix: str = "port") -> dict:


    kk, rk = f"{prefix}_kind", f"{prefix}_ref"
    kind = (_str(data, kk) or "").strip().lower()
    raw = data.get(rk)
    if raw is None:
        raw = data.get(f"{prefix}_no")          # older SPA builds still send _no
    if not kind and raw in (None, "", "null"):
        return {kk: None, rk: None}
    if kind not in fiber.PORT_KINDS:
        raise InventoryError(
            "port must be one of: " + ", ".join(fiber.PORT_KINDS))
    ref = str(raw).strip() if raw is not None else ""
    if not ref or ref == "null":
        if kind == "in":
            return {kk: kind, rk: None}
        raise InventoryError("name which port the fibre lands on")
    if len(ref) > fiber.PORT_REF_MAX:
        raise InventoryError(
            f"a port name must be {fiber.PORT_REF_MAX} characters or fewer")
    # A `port` is the box's own interface string and is kept AS TYPED. Only the
    # numbered kinds are arithmetic, and only they are bounded — a number is refused
    # for being impossible (leg 9 of a 1:8), never for being unusual.
    if kind not in fiber.NUMBERED_KINDS:
        return {kk: kind, rk: ref}
    no = fiber.port_no(ref)
    if no is None:
        raise InventoryError(
            f"a {fiber.port_label(kind, None)} is numbered — give its number")
    if no < 1:
        raise InventoryError("port numbers start at 1")
    if kind == "pon" and no > fiber.MAX_PON_INDEX:
        raise InventoryError(f"PON numbers stop at {fiber.MAX_PON_INDEX}")
    bound = fiber.port_bound(kind, split_ratio=split_ratio,
                             split_inputs=split_inputs)
    if bound is not None and no > bound:
        raise InventoryError(
            f"this box has {bound} {'leg' if kind == 'leg' else 'input'}"
            f"{'' if bound == 1 else 's'}")
    return {kk: kind, rk: str(no)}


def clean_fibre_joint_payload(data: dict, *, a_cores: int | None = None,
                              b_cores: int | None = None,
                              split_ratio: int | None = None,
                              split_inputs: int | None = None) -> dict:


    point = _fibre_point(data)
    try:
        a_cable_id = int(data.get("a_cable_id"))
    except (TypeError, ValueError):
        raise InventoryError("a_cable_id is required")
    try:
        a_core_no = clean_core_no(data.get("a_core_no"), a_cores)
    except FiberError as exc:
        raise InventoryError(str(exc)) from exc
    if a_core_no is None:
        raise InventoryError("a joint names a specific fibre — give a core number")
    port = clean_port(data, split_ratio=split_ratio, split_inputs=split_inputs)
    b_raw = data.get("b_cable_id")
    if b_raw in (None, "", "null"):
        return {**point, "a_cable_id": a_cable_id, "a_core_no": a_core_no,
                "b_cable_id": None, "b_core_no": None, **port}
    try:
        b_cable_id = int(b_raw)
    except (TypeError, ValueError):
        raise InventoryError("b_cable_id is invalid")
    try:
        b_core_no = clean_core_no(data.get("b_core_no"), b_cores)
    except FiberError as exc:
        raise InventoryError(str(exc)) from exc
    if b_core_no is None:
        raise InventoryError("a splice names a specific fibre at both ends")
    return {**point, "a_cable_id": a_cable_id, "a_core_no": a_core_no,
            "b_cable_id": b_cable_id, "b_core_no": b_core_no, **port}


def clean_fibre_tail_payload(data: dict, *, a_cores: int | None = None,
                             split_ratio: int | None = None,
                             split_inputs: int | None = None,
                             to_cores: int | None = None) -> dict:


    point = _fibre_point(data)
    try:
        a_cable_id = int(data.get("a_cable_id"))
    except (TypeError, ValueError):
        raise InventoryError("a_cable_id is required")
    try:
        a_core_no = clean_core_no(data.get("a_core_no"), a_cores)
    except FiberError as exc:
        raise InventoryError(str(exc)) from exc
    if a_core_no is None:
        raise InventoryError("a tail names a specific fibre — give a core number")
    to = _cable_end(data, "to")
    if to is None:
        raise InventoryError("name the box this fibre goes out to")
    if (to["device_id"], to["mac"]) == (point["device_id"], point["mac"]):
        raise InventoryError("a fibre cannot be taken out to the point it leaves")
    name = str(data.get("name") or "").strip()
    port = clean_port(data, split_ratio=split_ratio, split_inputs=split_inputs)
    # ...and the far end of a TAIL may be a core too. Taking a core out to another
    # CLOSURE is the same shape as taking it out to a box: an enclosure has no ports,
    # so terminating there leaves the fibre arriving and stopping.
    to_cable, to_core = _far_core(data, to_cores)
    if to_cable is not None and port.get("port_kind"):
        raise InventoryError("a fibre lands on a port or joins a core, not both")
    return {**point, "a_cable_id": a_cable_id, "a_core_no": a_core_no,
            "to": to, "name": name or None, **port,
            "to_cable_id": to_cable, "to_core_no": to_core}


def _far_core(data: dict, to_cores: int | None) -> tuple[int | None, int | None]:

    # THE FAR END AS A CORE — shared by the connect and the tail so the two gestures
    # cannot come apart on what "join it there" means.
    raw = data.get("to_cable_id")
    if raw in (None, "", "null"):
        return None, None
    try:
        cable_id = int(raw)
    except (TypeError, ValueError):
        raise InventoryError("to_cable_id is invalid")
    try:
        core = clean_core_no(data.get("to_core_no"), to_cores)
    except FiberError as exc:
        raise InventoryError(str(exc)) from exc
    if core is None:
        raise InventoryError("a splice names a specific fibre — give a core number")
    return cable_id, core


def clean_fibre_connect_payload(data: dict, *, split_ratio: int | None = None,
                                split_inputs: int | None = None,
                                to_split_ratio: int | None = None,
                                to_split_inputs: int | None = None,
                                to_cores: int | None = None) -> dict:


    point = _fibre_point(data)
    to = _cable_end(data, "to")
    if to is None:
        raise InventoryError("name the box this port connects to")
    if (to["device_id"], to["mac"]) == (point["device_id"], point["mac"]):
        raise InventoryError("a cable runs between two points, not from a box"
                             " back to itself")
    port = clean_port(data, split_ratio=split_ratio, split_inputs=split_inputs)
    far = clean_port(data, split_ratio=to_split_ratio,
                     split_inputs=to_split_inputs, prefix="to_port")
    # THE FAR END MAY BE A CORE INSTEAD OF A PORT. An enclosure has no ports — every
    # fibre in one is a splice — so "connect this port to that closure" has to ask
    # WHICH CORE it joins there, or the fibre arrives at the closure and stops.
    to_cable, to_core = _far_core(data, to_cores)
    if to_cable is not None and far.get("to_port_kind"):
        raise InventoryError("a fibre lands on a port or joins a core, not both")
    name = str(data.get("name") or "").strip()
    return {**point, "to": to, "name": name or None, **port, **far,
            "to_cable_id": to_cable, "to_core_no": to_core}


def clean_fibre_through_payload(data: dict) -> dict:

    point = _fibre_point(data)
    try:
        a_cable_id = int(data.get("a_cable_id"))
        b_cable_id = int(data.get("b_cable_id"))
    except (TypeError, ValueError):
        raise InventoryError("two cables are required")
    if a_cable_id == b_cable_id:
        raise InventoryError("a cable cannot be spliced straight through to itself")
    return {**point, "a_cable_id": a_cable_id, "b_cable_id": b_cable_id}


def clean_fibre_clear_payload(data: dict) -> dict:

    point = _fibre_point(data)
    try:
        cable_id = int(data.get("cable_id"))
        core_no = int(data.get("core_no"))
    except (TypeError, ValueError):
        raise InventoryError("cable_id and core_no are required")
    return {**point, "cable_id": cable_id, "core_no": core_no}


def clean_drop_route_payload(data: dict) -> dict:


    mac = _norm_mac(_str(data, "mac", required=True))
    if not mac:
        raise InventoryError("mac is required")
    return {"mac": mac, "waypoints": _waypoints(data.get("waypoints"))}


def clean_link_style_payload(data: dict) -> dict:


    try:
        child_id = int(data.get("child_id"))
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        raise InventoryError("child_id and parent_id are required")
    for moved in ("cable_id", "core_no", "cores"):
        if moved in data:
            raise InventoryError(
                "a span no longer carries a cable: lay one with"
                " POST /api/inventory/cable")
    fields: dict = {}
    if "label_pos" in data:
        raw = data.get("label_pos")
        if raw in (None, "", "null"):
            fields["label_pos"] = None
        else:
            try:
                pos = float(raw)
            except (TypeError, ValueError):
                raise InventoryError("label_pos must be a number between 0 and 1")
            if not (0.0 <= pos <= 1.0):
                raise InventoryError("label_pos must be between 0 and 1")
            fields["label_pos"] = round(pos, 4)
    if not fields:
        raise InventoryError("nothing to set: pass label_pos")
    return {"child_id": child_id, "parent_id": parent_id, "fields": fields}


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

def clean_peer_link(a_id: int, b_id: int, *,
                    parents: dict[int, int | None],
                    backups: dict[int, set[int]],
                    peers: dict[int, set[int]]) -> None:


    if a_id not in parents:
        raise InventoryError("node not found")
    if b_id not in parents:
        raise InventoryError("that device does not exist")
    if a_id == b_id:
        raise InventoryError("a device can't cross-link to itself")
    if parents.get(a_id) == b_id or parents.get(b_id) == a_id:
        raise InventoryError("those devices are already linked as parent and child")
    if b_id in backups.get(a_id, set()) or a_id in backups.get(b_id, set()):
        raise InventoryError("those devices are already linked as a backup uplink")
    if b_id in peers.get(a_id, set()):
        raise InventoryError("that cross-link already exists")

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

def _clean_onu_limit(data: dict, key: str) -> int | None:
    raw = data.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InventoryError(f"{key} must be a whole number")
    if value < 1:
        raise InventoryError(f"{key} must be at least 1")
    return value

def clean_optical_thresholds(data: dict) -> dict:
    warn = _clean_dbm(data, "warn_dbm")
    crit = _clean_dbm(data, "crit_dbm")
    if warn is not None and crit is not None and crit > warn:
        raise InventoryError("crit_dbm must be lower (weaker) than warn_dbm")
    return {"warn_dbm": warn, "crit_dbm": crit,
            "onu_pon_limit": _clean_onu_limit(data, "onu_pon_limit")}

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

WALK_DEFAULT_MAX_VARBINDS = 2000
WALK_CAP_MAX_VARBINDS = 20000

# The owner-facing "Test SNMP" walk, PINNED HERE and nowhere else. The client
# never names an OID and any root in the body is ignored: a permission that
# depends on a body field the client chooses is not a permission, and that is
# exactly the weak spot the raw-walk lock exists to remove.
# 1.3.6.1.2.1.1 is the RFC 1213 system group — sysDescr and its six neighbours,
# present on every agent alive, and the cheapest proof that the community
# string is right and UDP 161 reaches the box. The cap is small because the
# subtree is small; a bigger number would only buy a bigger dump.
SNMP_TEST_ROOT_OID = "1.3.6.1.2.1.1"
SNMP_TEST_MAX_VARBINDS = 12

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
                f"unknown metric {key!r}: must be one of {', '.join(PROFILE_METRICS)}")
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

GPON_PROFILE_OIDS = ("rx", "tx", "state", "distance", "serial", "name",
                     "ident_key", "ident_pon", "ident_onu", "ident_state",
                     "ident_distance", "ident_name")
GPON_PROFILE_STATES = ("online", "offline", "dying_gasp", "los", "unknown")
GPON_PON_INDEX_STRATEGIES = ("as_is", "first_segment", "packed_ifindex")

def clean_gpon_profile_payload(data: dict) -> dict:
    name = _str(data, "name", required=True).lower()
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    match = str(data.get("match_sysobjectid") or "").strip().strip(".")
    if match:
        match = clean_oid(match, field="match_sysobjectid")
    raw_oids = data.get("oids")
    if not isinstance(raw_oids, dict):
        raise InventoryError("oids must map ONU columns to OIDs")
    oids: dict = {}
    for key, val in raw_oids.items():
        if key not in GPON_PROFILE_OIDS:
            raise InventoryError(
                f"unknown oid field {key!r}: must be one of {', '.join(GPON_PROFILE_OIDS)}")
        if str(val or "").strip():
            oids[key] = clean_oid(val, field=f"oids.{key}")
    if not oids:
        raise InventoryError("profile must map at least one OID")
    scales: dict = {}
    for key, val in (data.get("scales") or {}).items():
        if key not in ("rx", "tx", "distance"):
            raise InventoryError("scales apply only to rx, tx, distance")
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise InventoryError(f"scales.{key} must be a number")
        if not 0 < f <= 1000:
            raise InventoryError(f"scales.{key} must be between 0 and 1000")
        scales[key] = f
    state_map_raw = data.get("state_map") or {}
    if not isinstance(state_map_raw, dict):
        raise InventoryError("state_map must be an object")
    state_map: dict = {}
    for k, v in state_map_raw.items():
        if v not in GPON_PROFILE_STATES:
            raise InventoryError(
                f"state_map[{k!r}] must be one of: {', '.join(GPON_PROFILE_STATES)}")
        state_map[str(k).strip()] = v
    state_default = str(data.get("state_default") or "unknown").strip().lower()
    if state_default not in GPON_PROFILE_STATES:
        raise InventoryError(
            f"state_default must be one of: {', '.join(GPON_PROFILE_STATES)}")
    pon_index = str(data.get("pon_index") or "as_is").strip().lower()
    if pon_index not in GPON_PON_INDEX_STRATEGIES:
        raise InventoryError(
            f"pon_index must be one of: {', '.join(GPON_PON_INDEX_STRATEGIES)}")
    pon_label = str(data.get("pon_label") or "").strip()
    if pon_label and "{pon}" not in pon_label:
        raise InventoryError("pon_label template must contain '{pon}'")
    if len(pon_label) > 32:
        raise InventoryError("pon_label must be 32 characters or fewer")
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    spec = {"oids": oids, "scales": scales, "state_map": state_map,
            "state_default": state_default, "pon_index": pon_index,
            "pon_label": pon_label}
    return {"name": name, "match_sysobjectid": match, "spec": spec,
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
