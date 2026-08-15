from __future__ import annotations

from datetime import datetime, timezone

SNMP_WALKS_KEEP = 10

SNMP_SUBSYSTEMS = ("health", "ports", "optics")

SNMP_STATUS_STATES = ("ok", "partial", "empty", "no_response", "timeout",
                      "no_profile", "error")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
