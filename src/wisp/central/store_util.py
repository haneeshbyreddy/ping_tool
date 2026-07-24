"""Shared helpers/constants for the CentralStore mixin modules."""
from __future__ import annotations

from datetime import datetime, timezone

# Diagnostic walk results kept per device (newest first) — older ones are pruned at
# create time so a chatty operator can't grow the DB unbounded.
SNMP_WALKS_KEEP = 10

# Closed vocabularies for device_snmp_status / device_capability writes — anything
# outside them is dropped at ingest (the edge is honest, but the wire isn't trusted).
SNMP_SUBSYSTEMS = ("health", "ports", "optics")

SNMP_STATUS_STATES = ("ok", "empty", "no_response", "timeout", "no_profile", "error")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
