#!/usr/bin/env python3


from __future__ import annotations

import argparse
import sys

from wisp.central.inventory import InventoryError
from wisp.central.store import CentralStore
from wisp.central.weboptics_profiles import (BUILTIN_SPECS,
                                             clean_web_optics_profile_payload)
from wisp.config import CONFIG

NAME = "cdata_54824"


def main() -> int:
    ap = argparse.ArgumentParser(description='Make the cdata_54824 OLTs eligible for the OPM-Diag Rx scrape.')
    ap.add_argument("--org", default=None,
                    help="org_id to scope the profile to (default: global)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = CentralStore(CONFIG.central_db)
    scope = args.org

    existing = [p for p in store.list_web_optics_profiles(None)
                if p["name"] == NAME and p["org_id"] == scope]
    if existing:
        print(f"web-optics profile {NAME!r} already exists for"
              f" org={scope or 'global'} (id={existing[0]['id']}) — nothing to do")
        return 0

    try:
        clean = clean_web_optics_profile_payload({"name": NAME,
                                                  **BUILTIN_SPECS["dbc"]})
    except InventoryError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1

    spec = clean["spec"]
    print(f"{NAME} -> org={scope or 'global'}  (recipe copied from built-in 'dbc')")
    print(f"    login  {spec['login_page_path']} -> {spec['login_path']}")
    print(f"    optics {spec['optics_method']} {spec['optics_path']}"
          f"  session={spec['session']} charset={spec['charset']}")
    print(f"    columns {', '.join(sorted(spec['columns']))}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    pid = store.create_web_optics_profile(scope, clean)
    print(f"written as web-optics profile {pid}")

    targets = store.web_optics_targets(vendors=(NAME,))
    if not targets:
        print("\nNOTE: no OLT is eligible under this name yet. Eligibility needs"
              " the vendor token, a walked roster, stored web credentials, an"
              " assigned probe and the org's web_proxy grant.")
    for t in targets:
        print(f"  eligible: {t['id']} {t['name']} ({t['org_id']})"
              f" pons={t.get('pon_ports')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
