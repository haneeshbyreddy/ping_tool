#!/usr/bin/env python3
"""Install the Stelfiber STGP08X (PEN 50224) GPON profile — AFTER the fleet can
read a byte-packed ifIndex.

The BOX names no maker: `sysDescr` is a bare "STGP08X", firmware
"IGB_V1.3.8_Rel", `sysObjectID` 1.3.6.1.4.1.50224.3.1.1. The vendor is the
OPERATOR's identification (2026-08-07); an earlier "Syrotech" reading was carried
over from a sibling org's `syrotech_gpon` work and was never evidence. It matters
here in a way it does not for a health profile: this name becomes the
`gpon_vendor` token on the device form, and a token nobody recognises reads on
screen as "this OLT has no optics".

That OLT indexes its ONU roster by a PACKED ifIndex rather than by `pon.onu`:
chassis<<24 | slot<<16 | pon<<8 | onu, so PON 1 ONU 0 is 16777472 (0x01000100).
Neither `as_is` nor `first_segment` can get a PON out of that, so the profile
needs `pon_index: "packed_ifindex"` — and an edge older than PACKED_INDEX_FLOOR
does not know that word. `gpon_profile_from_dict` rejects the WHOLE profile on
an unknown strategy, which is the safe direction (optics stay off, exactly as
they are today) but is invisible from central: the row would sit there looking
installed while the OLT reported nothing. Hence the gate.

Evidence for every OID below is walk 284 of chandana-network/MAIN_OLT4
(14,735 varbinds, not truncated, 2026-08-07):

  .3.12.2.1.4   state    1=online(132) 2=offline(110) 0=never-registered(68)
  .3.12.2.1.15  serial   310 unique, e.g. TJNW95b85c50 (GPON serial, not a MAC)
  .3.12.2.1.16  name     the OLT's description column (112 of 310 set)
  .3.12.2.1.19  distance 53-7498 m over the 242 provisioned slots, 0 on the rest

The decode was cross-checked against the OLT's OWN text column (.3.12.2.1.2,
"ONT01/000"): it agreed on all 310 rows, and the per-PON row counts match the
OLT's own counters at .3.2.3.1 (32/65/27/11/78/2/1/94, 132 online).

`state_map` maps 0 -> unknown ON PURPOSE. Those 68 slots are authorisation
entries that never registered: blank vendor id, distance 0, last-seen "-".
Calling them `offline` would invent 68 permanently-dark subscribers, and
`ponfault` would read that as a mass-drop cohort — a fabricated fibre cut with
nothing reporting an error. `state_default` is `unknown` for the same reason.

NO Rx COLUMN, deliberately. The box does publish per-ONU optics
(.3.12.3.1: col4 Rx dBm*100, col7 the 3.3 V rail, col8 temperature), but that
table is indexed `<ifindex>.0.0` while the roster is indexed `<ifindex>`, so
`parse_onu_table` keys them as different rows: mapping rx yields 448 rows of
which 138 carry a reading and NO identity, and zero rows carry both. Joining
them needs a parser change, not a profile edit. A blank Rx is recoverable; a
reading pinned to the wrong drop sends a tech to the wrong house.

    PYTHONPATH=src .venv/bin/python tools/gpon_add_stgp08x.py --org chandana-network

Takes effect on the next /edge/devices reply — no restart, no rollout.
"""
from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError, clean_gpon_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG
from wisp.version import is_newer

# The last edge release with no `packed_ifindex` strategy (gpon.py). A probe
# must be strictly newer than this before the profile may be installed.
PACKED_INDEX_FLOOR = "0.15.14"

PROFILE = {
    "name": "stelfiber_stgp08x",
    # The arc is the PEN itself: sysObjectID reads 1.3.6.1.4.1.50224.3.1.1 and
    # this is the only product we have seen under it. Auto-detect then covers a
    # second STGP08X without anyone typing a vendor on the device form.
    "match_sysobjectid": "1.3.6.1.4.1.50224",
    "oids": {
        "state": "1.3.6.1.4.1.50224.3.12.2.1.4",
        "serial": "1.3.6.1.4.1.50224.3.12.2.1.15",
        "name": "1.3.6.1.4.1.50224.3.12.2.1.16",
        "distance": "1.3.6.1.4.1.50224.3.12.2.1.19",
    },
    # Believed metres and unverified against the web UI, which this build gates
    # behind a CAPTCHA. The range is right for GPON (53 m to 7.5 km) and the
    # EPON time-quanta trap is EPON-specific — but if a cut bracket ever reads
    # short on this box, THIS is the number to check first.
    "scales": {"distance": 1.0},
    "state_map": {"1": "online", "2": "offline", "0": "unknown"},
    "state_default": "unknown",
    "pon_index": "packed_ifindex",
    "pon_label": "",
    "enabled": 1,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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

    # Whoever the profile is served to is who has to be new enough: an org-scoped
    # row reaches that org's probes, a global one reaches every probe. The set is
    # node_liveness() (registered and unrevoked), never the raw nodes table, which
    # remembers every identity ever seen.
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
    sys.stdout.flush()  # the roster explains the refusal below — keep them in order
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
