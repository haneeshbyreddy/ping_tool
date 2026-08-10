#!/usr/bin/env python3
"""Install a health profile for the Stelfiber STGP08X (PEN 50224).

The BOX does not name its maker: `sysDescr` is a bare "STGP08X", firmware
"IGB_V1.3.8_Rel", `sysObjectID` 1.3.6.1.4.1.50224.3.1.1. Vendor identification is
the OPERATOR's (2026-08-07) — an earlier "Syrotech" reading was carried over from
a sibling org's `syrotech_gpon` work and was never evidence. The PEN is what the
matching runs on, so the name here is a label for humans and nothing else.

chandana-network's MAIN_OLT4 reports `health: empty` — "the agent answered but
exposes no standard health OIDs" — because nothing in `snmp_profiles` claims PEN
50224. Health profiles are DATA, so this needs no rollout and is independent of
the ONU-roster work that IS waiting on an edge release.

TEMPERATURE ONLY, AND THAT IS THE POINT. The box's system block
(`.3.1.1.<n>.0`, 25 scalars, walks 284/296/297) holds several live numbers and
only one of them is identified by evidence rather than by plausibility:

  .21.0  "Fan1:209 Fan2:209 Fan3:209"   fan speeds, as a labelled string
  .22.0  48 -> 55 -> 55                  <-- mapped as temp_c
  .23.0  46 -> 52 -> 52                  a second sensor, same behaviour
  .17.0  13 -> 14 -> 13                  NOT MAPPED — see below
  .18.0  41 -> 42 -> 41                  NOT MAPPED — see below

`.22`/`.23` rose together (+7 / +6) over the 8.6 h between the first two samples
while the fans ramped 209 -> 237, then both held exactly steady across a 5-minute
third sample with the fans at 238. A pair of slow-moving numbers in the 46-55
range that track fan speed is a thermal reading; nothing else in this block
behaves that way. `.22` is mapped rather than `.23` because it reads consistently
higher, and a single temperature display must not understate the box.

`.17`/`.18` are ALSO live — they moved and moved back inside five minutes, so
they are measurements and not config (unlike `.13`-`.16`, which are the SNMP
communities and trap host, and `.8`-`.11`, which are 8/6/2/16 and are PROVEN to
be the port counts: `.3.2.1.1.2.*` lists exactly PON01-08, GE01-06, XGE01-02).
13% CPU with 41% memory is the textbook idle profile for a box like this, and
that is EXACTLY why they are not mapped: "plausible" is what the DBC `.28.1.3`
placeholder trap looked like too, and mapping them the wrong way round would
print this OLT's memory usage as its CPU load. They also moved in lockstep across
both samples, which two independent quantities have no reason to do.

TO FINISH THIS: that OLT runs a modern Vue web UI with JSON endpoints, and
`proxy_audit` already records the operator opening `/board?info=system` on it.
Open that one page through the dashboard's device proxy and compare its CPU and
memory figures against `.17.0` and `.18.0` read at the same moment. One capture
settles both; until then a blank field is recoverable and a mislabelled one is
not.

    PYTHONPATH=src .venv/bin/python tools/health_add_stgp08x.py --org chandana-network

Takes effect on the OLT's next health sweep (`snmp_interval_s`, 300s) — no
restart, no rollout.
"""
from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError, clean_profile_payload
from wisp.central.store import CentralStore
from wisp.config import CONFIG

PROFILE = {
    "name": "Stelfiber STGP08X GPON OLT",
    # The PEN arc itself: sysObjectID reads 1.3.6.1.4.1.50224.3.1.1 and this is
    # the only product seen under it, so a second STGP08X is covered without
    # anyone touching the device form. `health.py` matches by LONGEST prefix, so
    # a future model-specific profile still wins over this one.
    "match_sysobjectid": "1.3.6.1.4.1.50224",
    "metrics": {
        "temp_c": {"oid": "1.3.6.1.4.1.50224.3.1.1.22.0",
                   "decode": "as_is", "select": "first"},
    },
    "enabled": 1,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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
