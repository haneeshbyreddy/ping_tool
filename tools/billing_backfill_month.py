"""CUTOVER: charge the current month from its 1st, at today's measured rate.

Billing v2 starts with an empty ledger, so the first sweep after deploy writes
ONE row (today) and the days already elapsed this month are never billed. The
operator bills from the start of the month, so this walks each org's month to
date and stamps the missing days.

Deliberately a TOOL, run once by hand, and NOT engine behaviour: `accrue_org`
must keep starting a brand-new org on the day it appears, or an org signing up
on the 20th would be charged for the 19 days before it existed.

Counts come from `BillingSweeper` itself, so a backfilled day is priced by the
same code path that will price tonight's. Every row is stamped
`{"backfilled": true, "cutover": "<month>"}` so these are distinguishable
later from ordinary after-downtime carry-forward.

Idempotent: accrual rows are INSERT OR IGNORE on (org, day), so a day that
already has a row is never rewritten and re-running changes nothing.

    .venv/bin/python tools/billing_backfill_month.py              # dry run
    .venv/bin/python tools/billing_backfill_month.py --apply
    .venv/bin/python tools/billing_backfill_month.py --org ispA --apply

Run it AFTER central is restarted on the new code, so the rate it reads is the
rate the dashboard shows.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wisp.central import metering  # noqa: E402
from wisp.central.billing import BillingSweeper, operator_today  # noqa: E402
from wisp.central.inventory import PASSIVE_TYPES  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.config import CONFIG, Config  # noqa: E402


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def plan_org(sweeper: BillingSweeper, org: dict, month: str, today: date,
             now: datetime) -> dict | None:
    """What this org would be charged for the days of `month` up to today."""
    org_id = org["org_id"]
    if org.get("billing_exempt") or org.get("deactivated"):
        return None

    # The SAME reads the nightly sweep uses, so the backfill cannot price a day
    # differently from the way the ledger will price the next one.
    count, source, flags, _eff = metering.resolve_count(
        sweeper.onu_source(org_id, now), None)
    conn_rate, floor = sweeper.store.org_billing_rates(org_id)
    devices = sweeper.store.org_monitored_device_count(org_id, PASSIVE_TYPES)

    last = today.day if month == metering.month_key(today) else \
        metering.days_in_month(month)
    rows, skipped = [], []
    for d in range(1, last + 1):
        day = f"{month}-{d:02d}"
        if sweeper.store.accrual_on(org_id, day) is not None:
            skipped.append(day)
            continue
        rows.append(metering.accrue_day(
            day, count, source, {"backfilled": True, "cutover": month},
            devices, conn_rate, floor))
    return {"org_id": org_id, "name": org.get("name") or org_id,
            "count": count, "source": source, "devices": devices,
            "conn_rate": conn_rate, "floor": floor,
            "rows": rows, "skipped": skipped,
            "paise": sum(r.paise for r in rows)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="central DB (default: config)")
    ap.add_argument("--month", default=None, help="YYYY-MM (default: this month)")
    ap.add_argument("--org", default=None, help="one org (default: every org)")
    ap.add_argument("--apply", action="store_true",
                    help="write the rows. Without this it only reports.")
    args = ap.parse_args()

    cfg = Config(central_db=Path(args.db)) if args.db else CONFIG
    # PRINT THE RESOLVED PATH BEFORE OPENING IT. Every store-touching script
    # here defaults to data/central.db, which IS production; a rehearsal once
    # migrated the live DB because nothing said which file it had picked.
    print(f"db: {Path(cfg.central_db).resolve()}")
    store = CentralStore(cfg.central_db)
    sweeper = BillingSweeper(store, cfg)
    now = datetime.now(timezone.utc)
    today = operator_today(cfg, now)
    month = args.month or metering.month_key(today)

    orgs = [o for o in store.billing_org_rows()
            if not args.org or o["org_id"] == args.org]
    if args.org and not orgs:
        print(f"no such org: {args.org}")
        raise SystemExit(1)

    print(f"cutover backfill · {metering.month_label(month)} · "
          f"operator today {today.isoformat()} · "
          f"{'APPLY' if args.apply else 'DRY RUN'}\n")

    total, wrote = 0, 0
    for org in orgs:
        plan = plan_org(sweeper, org, month, today, now)
        if plan is None:
            print(f"  {org['org_id']:<14} skipped "
                  f"({'exempt' if org.get('billing_exempt') else 'deactivated'})")
            continue
        if not plan["rows"]:
            print(f"  {plan['org_id']:<14} nothing to add "
                  f"({len(plan['skipped'])} days already billed)")
            continue
        first, last = plan["rows"][0].day, plan["rows"][-1].day
        print(f"  {plan['org_id']:<14} {len(plan['rows']):>2} days "
              f"{first[-2:]}..{last[-2:]}  "
              f"{plan['count']:>5} ONUs ({plan['source']}) x "
              f"{plan['devices']:>3} gear  ->  {_rupees(plan['paise'])}"
              + (f"   [{len(plan['skipped'])} already billed]"
                 if plan["skipped"] else ""))
        total += plan["paise"]
        if args.apply:
            for row in plan["rows"]:
                if store.insert_accrual(plan["org_id"], row):
                    wrote += 1

    print(f"\n  total {_rupees(total)}")
    if args.apply:
        print(f"  wrote {wrote} accrual rows")
        # The month is not closed here: close_invoices only ever bills a month
        # that has ENDED, so this month keeps accruing normally and invoices on
        # the 1st like any other.
        print("  this month stays open and will invoice on the 1st as usual")
    else:
        print("  dry run. Re-run with --apply to write these rows.")


if __name__ == "__main__":
    main()
