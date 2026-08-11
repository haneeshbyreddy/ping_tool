#!/usr/bin/env python3


from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError, clean_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG

PROFILE = {
    "name": "Stelfiber STGP08X GPON OLT",
    "match_sysobjectid": "1.3.6.1.4.1.50224",
    "metrics": {
        "temp_c": {"oid": "1.3.6.1.4.1.50224.3.1.1.22.0",
                   "decode": "as_is", "select": "first"},
    },
    "enabled": 1,
}


def main() -> int:
    ap = argparse.ArgumentParser(description='Install a health profile for the Stelfiber STGP08X (PEN 50224).')
    ap.add_argument("--org", default=None,
                    help="org_id to scope the profile to (default: global)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = CentralStore(CONFIG.central_db)
    scope = args.org

    existing = [p for p in store.list_snmp_profiles(None)
                if p["name"] == PROFILE["name"] and p["org_id"] == scope]
    if existing:
        print(f"profile {PROFILE['name']!r} already exists for"
              f" org={scope or 'global'} (id={existing[0]['id']}) — nothing to do")
        return 0

    try:
        clean = clean_profile_payload(dict(PROFILE))
    except InventoryError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1

    print(f"{PROFILE['name']} -> org={scope or 'global'}"
          f"  match={clean['match_sysobjectid']}")
    for metric, spec in clean["metrics"].items():
        print(f"    {metric:10s} {spec['oid']}  decode={spec['decode']}"
              f" select={spec['select']}")
    print("    (cpu_pct / mem_pct deliberately unmapped — see the module docstring)")
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    pid = store.create_snmp_profile(scope, clean)
    print(f"written as profile {pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
