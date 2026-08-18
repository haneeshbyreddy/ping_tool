"""Seed a THROWAWAY central DB with every billing v2 state worth looking at.

Verification only, never pointed at prod. Builds a synthetic install rather
than a neutered copy of the live DB, because billing needs states the real
fleet does not have yet (a locked org, an org in credit, a deactivated one)
and a synthetic org can hold all of them at once.

    .venv/bin/python tools/seed_billing_verify.py ~/verify/billing.db
    WISP_CENTRAL_DB=~/verify/billing.db WISP_CENTRAL_PORT=8899 \
        .venv/bin/python apps/central/main.py

Logins: owner/ownerpassword (ispA), locked/lockedpassword (ispLocked),
        root/rootpassword (superadmin).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wisp.central import auth, metering  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.central.store_util import _now_iso  # noqa: E402

CONN, FLOOR = metering.DEFAULT_CONN_PAISE, metering.DEFAULT_FLOOR_PAISE


_ip = [0]


def device(store, org, name, dtype=None):
    # Passives carry no probe in the product, but ip_address is NOT NULL in
    # the schema and inventory's validation is what refuses one. Seeding
    # straight into the store, a blank string is the honest stand-in.
    _ip[0] += 1
    return store.create_org_device(org, {
        "name": name,
        "ip_address": "" if dtype == "splitter" else f"10.20.{_ip[0] // 250}.{_ip[0] % 250 + 1}",
        "device_type": dtype, "region": "Hubli", "parent_device_id": None})


def accrue(store, org, day, conns, devices, source="onu", flags=None):
    paise, side = metering.daily_paise(conns, devices, CONN, FLOOR,
                                       metering.days_in_month(day[:7]))
    store.insert_accrual(org, metering.AccrualRow(
        day=day, paise=paise, conn_count=conns, conn_source=source,
        device_count=devices, winning_side=side, conn_rate_paise=CONN,
        floor_paise=FLOOR, flags=flags or {}))


def main() -> None:
    out = Path(sys.argv[1]).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    store = CentralStore(out)

    # The notifier reads app_settings FRESH on every send, so env vars do not
    # stop it. A verify install must never be able to page a real person.
    store.set_setting("whatsapp_enabled", "0")
    store.set_setting("whatsapp_token", "")
    store.set_setting("whatsapp_admin_number", "")

    today = date.today()

    # -- ispA: the healthy owner. Accruing, nothing due, ONU-counted -------
    store.set_org("ispA", name="Acme Networks")
    auth.create_user(store, "ispA", "owner", "ownerpassword", "owner")
    auth.create_user(store, "ispA", "field", "fieldpassword", "worker")
    for i in range(9):
        device(store, "ispA", f"HILL-OLT-{i + 1}", "OLT" if i < 3 else "switch")
    device(store, "ispA", "SPL-12", "splitter")          # passive: never billed
    start = today.replace(day=1)
    day = start
    while day <= today:
        # A visible subscriber drop 3 days ago: the chart has to show it.
        n = 412 if (today - day).days > 3 else 371
        held = (today - day).days == 5
        accrue(store, "ispA", day.isoformat(), n, 9,
               "held" if held else "onu",
               {"held": "onu"} if held else None)
        day += timedelta(days=1)

    # -- ispLocked: an unpaid invoice past the banner window ---------------
    store.set_org("ispLocked", name="Sahyadri Broadband")
    auth.create_user(store, "ispLocked", "locked", "lockedpassword", "owner")
    for i in range(4):
        device(store, "ispLocked", f"SAH-{i + 1}", "OLT")
    last = metering.prev_month(metering.month_key(today))
    for d in range(1, metering.days_in_month(last) + 1):
        accrue(store, "ispLocked", f"{last}-{d:02d}", 186, 4)
    inv = sum(r["paise"] for r in store.accruals_for_month("ispLocked", last))
    # Issued on the 1st of this month, i.e. already past BANNER_DAYS.
    store.ensure_invoice("ispLocked", last, inv,
                         issued_at=f"{metering.month_key(today)}-01T00:05:00+00:00")
    for d in range(1, today.day + 1):
        accrue(store, "ispLocked", f"{metering.month_key(today)}-{d:02d}", 186, 4)

    # -- ispCredit: paid ahead. Zero dunning, a projection instead ---------
    store.set_org("ispCredit", name="Malnad Fibre")
    for i in range(2):
        device(store, "ispCredit", f"MAL-{i + 1}", "OLT")
    for d in range(1, today.day + 1):
        accrue(store, "ispCredit", f"{metering.month_key(today)}-{d:02d}", 240, 2,
               "onu")
    store.record_payment("ispCredit", 900000, "manual",
                         note="advance for the quarter", recorded_by="root")

    # -- the two flag states, so the console has all its chips -------------
    store.set_org("ispFree", name="Community Mesh (not billed)")
    device(store, "ispFree", "MESH-1", "router")
    store.set_org_billing_flags("ispFree", exempt=True)

    store.set_org("ispGone", name="Old Customer")
    device(store, "ispGone", "OLD-1", "OLT")
    store.set_org_billing_flags("ispGone", deactivated=True)

    # An org NOTHING can count: an OLT that reports no roster. It pays the
    # device floor on an honest zero, and every count surface has to render
    # the dead zone rather than a confident "0 ONUs".
    store.set_org("ispSmall", name="Kittur Net")
    auth.create_user(store, "ispSmall", "small", "smallpassword", "owner")
    device(store, "ispSmall", "KIT-1", "OLT")
    for d in range(1, today.day + 1):
        accrue(store, "ispSmall", f"{metering.month_key(today)}-{d:02d}", 0, 1,
               "none")

    auth.create_user(store, None, "root", "rootpassword")
    store.settle_invoices("ispLocked")
    store.settle_invoices("ispCredit")

    print(f"seeded {out}")
    for org in ("ispA", "ispLocked", "ispCredit", "ispFree", "ispGone", "ispSmall"):
        print(f"  {org:10s} outstanding={store.outstanding_paise(org):>9d} paise"
              f"  open={store.oldest_open_invoice(org)}")
    print(f"  stamped {_now_iso()}")


if __name__ == "__main__":
    main()
