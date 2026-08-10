#!/usr/bin/env python3
"""Install the `cdata_54824` GPON profile — a C-Data EPON OLT whose registration
table lives under PEN **54824** instead of 37950.

chandana-network's MAIN_OLT5 (192.168.8.104) is a C-Data EPON box in every way
that matters, but its sysObjectID LIES: it answers `1.3.6.1.4.1.37950.1.1.5.10.14.1`
— the same arc its two working siblings report — while its whole 37950 tree is 81
varbinds of VLAN config with no `.5.12` registration table in it. Auto-detect
therefore picked the `dbc` built-in, walked 37950, found nothing, and stored
`optics ok, 0 ONUs`: an OLT with 54 live subscribers rendering as an OLT with no
subscribers, which is the "nothing is wrong / nothing is measured" trap in its
purest form.

Its real MIB is PEN 54824, and the roster is the SAME 14-column C-Data table at
the SAME sub-arc — `1.1.5.12.1.12.1.<col>` — so this profile is the `dbc`
built-in with one number changed. Evidence is walk 295 (3,444 varbinds, not
truncated, 2026-08-07), 246 rows x 14 columns:

  col2   PON       1(119) 2(49) 3(44) 4(34)
  col3   ONU id    1..119, per PON
  col5   state     1=online(54) 0=offline(192)
  col6   MAC       184 unique of 246 — this firmware never drops a vacated slot
  col10  name      the web-UI description ('NULL' when unset)
  col13  distance  343..3430 on the online rows, 0 on none of them

THREE INDEPENDENT SOURCES AGREE on the state column, which is why it is trusted
without a web-UI capture: the box's own ifTable has exactly four PON interfaces
(EPON0/1..4) of which **EPON0/1 and EPON0/4 are up and 0/2 and 0/3 are down**,
and the roster puts all 54 of its online ONUs on PONs 1 and 4 and none on 2 or 3;
the count of `up` per-ONU sub-interfaces (`EPON0/1:7` style) is 54 as well.

`match_sysobjectid` IS DELIBERATELY BLANK, and this is the load-bearing part.
The obvious value — 1.3.6.1.4.1.37950, what this box actually reports — would
make the profile claim MAIN_OLT and MAIN_OLT2 as well, and a central-served
profile WINS an equal-length prefix tie against a built-in
(`match_gpon_profile`), so it would take the two fully-working OLTs off the
`dbc` roster they read today and point them at a PEN they do not answer. A box
whose sysObjectID is wrong cannot be auto-detected by anyone; it is reached ONLY
through the device's own `gpon_vendor` override, which is exactly the precedence
`GponPollerPool.resolve` already documents.

`state_default` is `unknown`, never `offline` — an edge on v0.15.14 still gives
an ABSENT state cell the default, and these agents quit a GETBULK mid-table, so
a truncated state column would otherwise hand `ponfault` a cohort of fabricated
dark subscribers. `unknown` is excluded from `DARK_STATES` by design.

`scales.distance` is 1.0 to MATCH the `dbc` built-in, not because the unit is
right: on C-Data EPON this column is RTT in time quanta, so every cut bracket
printed from it runs ~39% short. That is a known, uniform error across this org's
OLTs, and uniform is what `ponfault` needs — a bracket mixing metres and quanta
INVERTS, one that is uniformly wrong stays monotonic. Fix the unit fleet-wide or
not at all.

NO Rx COLUMN, for the same reason `dbc` has none: per-ONU Rx does not exist in
this firmware's SNMP (field-debunked twice on the PYLON EPOLT-3304). dBm for this
box comes from the OPM-Diag web scrape — see tools/weboptics_add_cdata_54824.py.

    PYTHONPATH=src .venv/bin/python tools/gpon_add_cdata_54824.py \
        --org chandana-network --device 126

Takes effect on the OLT's next optics sweep — no restart, no rollout: every word
of this profile is in the vocabulary v0.15.14 already speaks.
"""
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
    # Blank ON PURPOSE — see the module docstring. This profile is reachable
    # only through an explicit device gpon_vendor override.
    "match_sysobjectid": "",
    "oids": {
        "ident_key": f"{ROOT}.6",       # MAC — the roster's identity column
        "ident_pon": f"{ROOT}.2",
        "ident_onu": f"{ROOT}.3",
        "ident_state": f"{ROOT}.5",
        "ident_distance": f"{ROOT}.13",
        "ident_name": f"{ROOT}.10",
    },
    "scales": {"distance": 1.0},
    # Both spellings, like the `_dbc_state` callable this mirrors: the agent
    # returns the digit today, and a build that ever answers the word must not
    # fall through to `unknown` and quietly empty the PON.
    "state_map": {"1": "online", "online": "online",
                  "0": "offline", "offline": "offline"},
    "state_default": "unknown",
    # Irrelevant here (no metric table is mapped, so the PON always comes from
    # the ident_pon COLUMN), but the vocabulary requires a value: keep it at the
    # inert default rather than implying an index decode this profile never does.
    "pon_index": "as_is",
    # Matches the box's own ifTable, which names its four PONs EPON0/1..EPON0/4.
    "pon_label": "EPON0/{pon}",
    "enabled": 1,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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

    # The override is not a convenience — it is the ONLY way this profile is
    # ever selected, so installing the row without stamping the device leaves
    # the OLT exactly as broken as before, with a profile sitting there looking
    # applied. Same failure the STGP08X version gate exists to prevent.
    #
    # Stamped as a single-column UPDATE rather than through `update_org_device`
    # ON PURPOSE: that path rewrites the whole row from a payload, and an absent
    # key there reads as "not set" — which is how a GPON box silently loses its
    # onu_pon_limit. One column is the only thing this tool has any business
    # changing.
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
