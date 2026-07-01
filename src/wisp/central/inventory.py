"""ISP device-inventory validation (Phase A) — pure, no DB, no tenant crossing.

The org-managed topology (`org_devices` in `central/store.py`) needs the same field
coercion, IP sanity, device-type pick-list, and parent-cycle check as the edge's
`server/services.py:_clean_device_payload`. That logic is separable from storage (it's a
pure function of a payload + the tenant's current parent map), so it lives here,
unit-testable without a DB — exactly like the edge's SNMP-payload cleaner.

Phase A has no backup/redundancy links yet (that's topology suppression, which only means
something once Phase B is running live detection), so the cycle check here walks the
PRIMARY parent chain only. `parents` must already be scoped to one tenant — the caller
(`central/store.py:org_device_parent_map`) never leaks another org's ids into it, so a
cross-tenant id simply looks like "parent node does not exist".
"""
from __future__ import annotations

import ipaddress

# Kept in lockstep with the edge's list (server/services.py DEVICE_TYPES) and the SPA form.
DEVICE_TYPES = ("core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul")
SNMP_VERSIONS = ("2c",)  # room for '3' later; v3 auth/priv is out of scope for now


class InventoryError(ValueError):
    """A bad device/SNMP payload (validation), surfaced to the UI as a 422."""


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
