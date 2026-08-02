#!/usr/bin/env python3
"""Map a GPON profile's ONU serial column — AFTER the fleet can key on the slot.

A metric-only profile (one with no registration table, e.g. `syrotech_gpon`)
reports a serial the operator can read off the sticker, but an edge older than
SLOT_KEY_FLOOR used that serial as `onu_key`. These OLTs never drop a vacated
registration, so a re-registered ONU appears on both its old and new slot: a
serial key collapses the pair and the last write wins, storing LIVE ONUs as
offline at 0.00 dBm. That is exactly what happened on badri_fiber 2026-07-27
(9 serials of 194 on 2-3 slots each) and why the column was left unmapped.

So this is not a config edit that can be made whenever — it is only safe once
every probe serving the profile runs a build that keys on the slot. The version
gate is the whole point of the script; `--force` exists for a rebuilt fleet that
reports an unexpected version, not for impatience.

    PYTHONPATH=src .venv/bin/python tools/gpon_enable_serial.py \
        --profile syrotech_gpon --oid 1.3.6.1.4.1.37950.1.1.6.1.1.2.1.5

Takes effect on the next /edge/devices reply — no restart, no rollout.
"""
from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError, clean_gpon_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG
from wisp.version import is_newer

# The last edge release that keyed a metric-path ONU on its serial. A probe must
# be strictly newer than this before the serial column may be mapped.
SLOT_KEY_FLOOR = "0.15.13"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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

    # Whoever the profile is served to is who has to be new enough: an org-scoped
    # row reaches that org's probes, a global one reaches every probe. The set is
    # node_liveness() (registered and unrevoked), never the raw nodes table, which
    # remembers every identity ever seen — a retired probe must not gate this
    # forever. node_versions() then supplies the build each one reports.
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
    sys.stdout.flush()  # the roster explains the refusal below — keep them in order
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
