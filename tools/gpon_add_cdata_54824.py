#!/usr/bin/env python3


from __future__ import annotations

import argparse
import sqlite3
import sys

from wisp.central.inventory import InventoryError, clean_gpon_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG

ROOT = "1.3.6.1.4.1.54824.1.1.5.12.1.12.1"

PROFILE = {
    "name": "cdata_54824",
    "match_sysobjectid": "",
    "oids": {
        "ident_key": f"{ROOT}.6",
        "ident_pon": f"{ROOT}.2",
        "ident_onu": f"{ROOT}.3",
        "ident_state": f"{ROOT}.5",
        "ident_distance": f"{ROOT}.13",
        "ident_name": f"{ROOT}.10",
    },
    "scales": {"distance": 1.0},
    "state_map": {"1": "online", "online": "online",
                  "0": "offline", "offline": "offline"},
    "state_default": "unknown",
    "pon_index": "as_is",
    "pon_label": "EPON0/{pon}",
    "enabled": 1,
}


def main() -> int:
    ap = argparse.ArgumentParser(description='Install the cdata_54824 GPON profile (C-Data EPON OLT under PEN 54824).')
    ap.add_argument("--org", default=None,
                    help="org_id to scope the profile to (default: global)")
    ap.add_argument("--device", type=int, action="append", default=[],
                    help="device id to stamp with this vendor (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = CentralStore(CONFIG.central_db)
    scope = args.org

    try:
        clean = clean_gpon_profile_payload(dict(PROFILE))
    except InventoryError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1

    existing = [p for p in store.list_gpon_profiles(None)
                if p["name"] == PROFILE["name"] and p["org_id"] == scope]
    if existing:
        print(f"profile {PROFILE['name']!r} already exists for"
              f" org={scope or 'global'} (id={existing[0]['id']})")
    else:
        print(f"{PROFILE['name']} -> org={scope or 'global'}"
              f" match={clean['match_sysobjectid'] or '(none — override only)'}")
        for k, v in clean["spec"]["oids"].items():
            print(f"    {k:15s} {v}")
        if args.dry_run:
            print("(dry run — nothing written)")
        else:
            pid = store.create_gpon_profile(scope, clean)
            print(f"written as profile {pid}")

    for dev_id in args.device:
        with sqlite3.connect(CONFIG.central_db, timeout=5.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            row = conn.execute(
                "SELECT name, org_id, gpon_vendor FROM org_devices WHERE id=?",
                (dev_id,)).fetchone()
            if not row:
                print(f"  device {dev_id}: no such device", file=sys.stderr)
                return 1
            name, dev_org, was = row
            if scope is not None and dev_org != scope:
                print(f"  device {dev_id} ({name}) belongs to org {dev_org!r}, not"
                      f" {scope!r} — an org-scoped profile would not reach it",
                      file=sys.stderr)
                return 1
            if args.dry_run:
                print(f"  would stamp device {dev_id} ({name}):"
                      f" gpon_vendor {was or '(auto)'} -> {PROFILE['name']}")
                continue
            conn.execute("UPDATE org_devices SET gpon_vendor=? WHERE id=?",
                         (PROFILE["name"], dev_id))
            conn.commit()
        print(f"  device {dev_id} ({name}): gpon_vendor {was or '(auto)'}"
              f" -> {PROFILE['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
