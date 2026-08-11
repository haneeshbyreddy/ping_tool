#!/usr/bin/env python3


from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError, clean_gpon_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG
from wisp.version import is_newer

SLOT_KEY_FLOOR = "0.15.13"


def main() -> int:
    ap = argparse.ArgumentParser(description="Map a GPON profile's ONU serial column, once the fleet can key on the slot.")
    ap.add_argument("--profile", required=True, help="gpon_profiles.name")
    ap.add_argument("--oid", required=True, help="the ONU serial/MAC column OID")
    ap.add_argument("--field", default="serial", help="oids key to set (default: serial)")
    ap.add_argument("--force", action="store_true",
                    help="skip the probe-version gate (see the module docstring)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = CentralStore(CONFIG.central_db)
    matches = [p for p in store.list_gpon_profiles(None) if p["name"] == args.profile]
    if not matches:
        print(f"no gpon_profiles row named {args.profile!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} rows named {args.profile!r} — resolve by hand", file=sys.stderr)
        return 1
    profile = matches[0]

    scope = profile["org_id"]
    live = {(n["org_id"], n["node_id"]) for n in store.node_liveness()}
    nodes = [dict(n, org_id=org) for org in sorted({o for o, _ in live})
             if scope in (None, org)
             for n in store.node_versions(org) if (org, n["node_id"]) in live]
    if not nodes:
        print("no live probe serves this profile's scope — nothing to gate on,"
              " but nothing is walking it either", file=sys.stderr)
        return 1
    stale = [n for n in nodes if not is_newer(n.get("version"), SLOT_KEY_FLOOR)]
    for n in nodes:
        mark = "OLD" if n in stale else "ok "
        print(f"  [{mark}] {n['org_id']}/{n['node_id']} v{n.get('version') or '?'}")
    sys.stdout.flush()
    if stale and not args.force:
        print(f"\nREFUSING: {len(stale)} probe(s) still key an ONU on its serial"
              f" (need newer than v{SLOT_KEY_FLOOR}). Mapping the column now would"
              " collapse every re-registered ONU into one row and store live ones"
              " as dark. Roll the fleet forward first.", file=sys.stderr)
        return 2

    spec = dict(profile["spec"])
    oids = dict(spec.get("oids") or {})
    if oids.get(args.field) == args.oid:
        print(f"\noids.{args.field} is already {args.oid} — nothing to do")
        return 0
    if oids.get(args.field):
        print(f"\noids.{args.field} is currently {oids[args.field]} — overwriting")
    oids[args.field] = args.oid
    spec["oids"] = oids
    payload = dict(spec)
    payload["name"] = profile["name"]
    payload["match_sysobjectid"] = profile["match_sysobjectid"]
    payload["enabled"] = profile["enabled"]
    try:
        clean = clean_gpon_profile_payload(payload)
    except InventoryError as exc:
        print(f"\nrejected: {exc}", file=sys.stderr)
        return 1

    print(f"\nprofile {profile['id']} ({profile['name']}, org={scope or 'global'})"
          f" oids -> {clean['spec']['oids']}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    if not store.update_gpon_profile(profile["id"], clean):
        print("update wrote no row", file=sys.stderr)
        return 1
    print("written — probes pick it up on their next /edge/devices reply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
