#!/usr/bin/env python3


from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError, clean_gpon_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG
from wisp.version import is_newer

PACKED_INDEX_FLOOR = "0.15.14"

PROFILE = {
    "name": "stelfiber_stgp08x",
    "match_sysobjectid": "1.3.6.1.4.1.50224",
    "oids": {
        "state": "1.3.6.1.4.1.50224.3.12.2.1.4",
        "serial": "1.3.6.1.4.1.50224.3.12.2.1.15",
        "name": "1.3.6.1.4.1.50224.3.12.2.1.16",
        "distance": "1.3.6.1.4.1.50224.3.12.2.1.19",
    },
    "scales": {"distance": 1.0},
    "state_map": {"1": "online", "2": "offline", "0": "unknown"},
    "state_default": "unknown",
    "pon_index": "packed_ifindex",
    "pon_label": "",
    "enabled": 1,
}


def main() -> int:
    ap = argparse.ArgumentParser(description='Install the Stelfiber STGP08X (PEN 50224) GPON profile.')
    ap.add_argument("--org", default=None,
                    help="org_id to scope the profile to (default: global)")
    ap.add_argument("--force", action="store_true",
                    help="skip the probe-version gate (see the module docstring)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = CentralStore(CONFIG.central_db)
    scope = args.org

    existing = [p for p in store.list_gpon_profiles(None)
                if p["name"] == PROFILE["name"] and p["org_id"] == scope]
    if existing:
        print(f"profile {PROFILE['name']!r} already exists for"
              f" org={scope or 'global'} (id={existing[0]['id']}) — nothing to do")
        return 0

    live = {(n["org_id"], n["node_id"]) for n in store.node_liveness()}
    nodes = [dict(n, org_id=org) for org in sorted({o for o, _ in live})
             if scope in (None, org)
             for n in store.node_versions(org) if (org, n["node_id"]) in live]
    if not nodes:
        print("no live probe serves this scope — nothing to gate on, but nothing"
              " would walk the profile either", file=sys.stderr)
        return 1
    stale = [n for n in nodes if not is_newer(n.get("version"), PACKED_INDEX_FLOOR)]
    for n in nodes:
        print(f"  [{'OLD' if n in stale else 'ok '}] {n['org_id']}/{n['node_id']}"
              f" v{n.get('version') or '?'}")
    sys.stdout.flush()
    if stale and not args.force:
        print(f"\nREFUSING: {len(stale)} probe(s) cannot read a packed ifIndex"
              f" (need newer than v{PACKED_INDEX_FLOOR}). They would reject the"
              " whole profile and leave that OLT's optics off, while central"
              " showed a profile installed. Roll the fleet forward first.",
              file=sys.stderr)
        return 2

    try:
        clean = clean_gpon_profile_payload(dict(PROFILE))
    except InventoryError as exc:
        print(f"\nrejected: {exc}", file=sys.stderr)
        return 1

    print(f"\n{PROFILE['name']} -> org={scope or 'global'}"
          f" match={clean['match_sysobjectid']} oids={clean['spec']['oids']}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    pid = store.create_gpon_profile(scope, clean)
    print(f"written as profile {pid} — probes pick it up on their next"
          " /edge/devices reply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
