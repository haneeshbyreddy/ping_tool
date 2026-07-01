"""ISP device-inventory validation (Phase A) — pure, no DB, no tenant crossing.

The org-managed topology (`org_devices` in `central/store.py`) needs the same field
coercion, IP sanity, device-type pick-list, and parent-cycle check as the edge's
`server/services.py:_clean_device_payload`. That logic is separable from storage (it's a
pure function of a payload + the tenant's current parent map), so it lives here,
unit-testable without a DB — exactly like the edge's SNMP-payload cleaner.

`clean_device_payload`'s cycle check walks the PRIMARY parent chain only — a device's
`parent_device_id`. `clean_backup_link` (CLAUDE.md item 3) is the sibling validator for the
EXTRA redundancy edge (`org_device_links`, kind='backup'): its cycle check walks the FULL
edge set (primary + existing backups), mirroring the old single-box `add_backup_link`.
`parents`/`backups` must already be scoped to one tenant — the caller
(`central/store.py:org_device_parent_map`/`org_device_backup_map`) never leaks another
org's ids into them, so a cross-tenant id simply looks like "parent node does not exist".
"""
from __future__ import annotations

import ipaddress
import re

# Kept in lockstep with the central dashboard SPA's device-type form.
DEVICE_TYPES = ("core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul")
SNMP_VERSIONS = ("2c",)  # room for '3' later; v3 auth/priv is out of scope for now

# node_id becomes a systemd unit's identity, a filesystem path component (/etc/wisp on
# the edge), and a URL query/JSON value on the wire — keep it boring on purpose.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InventoryError(ValueError):
    """A bad device/SNMP/node-enrollment payload (validation), surfaced to the UI as a 422."""


def _str(data: dict, key: str, *, required: bool = False, default=None):
    v = data.get(key)
    v = v.strip() if isinstance(v, str) else (None if v is None else str(v).strip())
    if required and not v:
        raise InventoryError(f"{key.replace('_', ' ')} is required")
    return v or default


def clean_device_payload(data: dict, *, parents: dict[int, int | None],
                         device_id: int | None) -> dict:
    """Validate + normalise a create/update payload against one tenant's current parent
    map. Raises InventoryError on the first problem. `device_id` is None on create."""
    name = _str(data, "name", required=True)
    ip_address = _str(data, "ip_address", required=True)
    try:
        ipaddress.ip_address(str(ip_address))
    except ValueError:
        raise InventoryError(f"'{ip_address}' is not a valid IP address")
    device_type = _str(data, "device_type")
    if device_type and device_type not in DEVICE_TYPES:
        raise InventoryError(f"device type must be one of: {', '.join(DEVICE_TYPES)}")
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
        # Walk UP from the proposed parent over the primary chain; reaching this device
        # means the new edge would close a loop.
        cur, seen = parent_id, set()
        while cur is not None:
            if cur == device_id:
                raise InventoryError("that parent would create a topology loop")
            if cur in seen:
                break
            seen.add(cur)
            cur = parents.get(cur)

    return {"name": name, "ip_address": ip_address, "device_type": device_type,
            "region": region, "parent_device_id": parent_id}


def clean_backup_link(child_id: int, parent_id: int, *,
                      parents: dict[int, int | None],
                      backups: dict[int, set[int]]) -> None:
    """Validate a proposed BACKUP parent edge (`child_id` runs a redundant uplink to
    `parent_id`) against the tenant's current topology. Raises `InventoryError` on the
    first problem; returns nothing (an OK edge) otherwise. Pure — mirrors the old
    single-box `add_backup_link`'s checks one-for-one, minus the DB, so it's
    unit-testable like `clean_device_payload`."""
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
    # Cycle check over the FULL edge set (primary + existing backups): walk UP from the
    # proposed parent; reaching the child means the new edge would close a loop.
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


def clean_port_bandwidth_payload(data: dict) -> dict:
    """Validate a per-port low-bandwidth alarm config: `threshold_mbps=None`/absent
    disables the alarm for that port (badge-only)."""
    raw = data.get("threshold_mbps")
    threshold: float | None = None
    if raw not in (None, "", "null"):
        try:
            threshold = float(raw)
        except (TypeError, ValueError):
            raise InventoryError("threshold_mbps must be a number")
        if threshold <= 0:
            raise InventoryError("threshold_mbps must be positive")
    direction = (str(data.get("direction") or "either")).strip().lower()
    if direction not in BW_DIRECTIONS:
        raise InventoryError(f"direction must be one of: {', '.join(BW_DIRECTIONS)}")
    return {"threshold_mbps": threshold, "direction": direction}


def clean_snmp_payload(data: dict) -> dict:
    """Validate an SNMP-config payload (enable + community + version + port)."""
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


def clean_node_id(raw) -> str:
    """Validate a node_id an ISP user types in when self-service-registering a new edge
    (`POST /api/nodes`, `central/server.py`) — deliberately boring charset since it ends
    up as a systemd identity, a path segment under /etc/wisp on the edge box, and a bare
    JSON/query value on the wire."""
    node_id = str(raw or "").strip()
    if not node_id:
        raise InventoryError("node id is required")
    if not _NODE_ID_RE.match(node_id):
        raise InventoryError(
            "node id must be 1-64 characters, starting with a letter or digit, and "
            "contain only letters, digits, '.', '_', or '-'")
    return node_id
