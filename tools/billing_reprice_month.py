"""RE-PRICE an un-invoiced month onto the CURRENT metering basis.

Written for the 2026-08-17 basis change (per RADIUS username -> per ONU). The
ledger had already accrued August 1..17 counting RADIUS usernames; those days
are real usage, nobody has been invoiced for them yet, and the operator bills
the whole month on the new basis. So the rows are rewritten rather than left
as a fortnight priced one way and a fortnight priced the other.

WHAT IT CHANGES, and nothing else:

    the CONNECTION COUNT and its source, re-measured today on the current
    ladder (metering.resolve_count over the ONU roster)

WHAT IT PRESERVES, deliberately:

    the DAY SET     — exactly the days that already have rows. This is a
                      re-price, not a re-decision about which days are
                      billable, so an org's start date, a backfill hole and
                      an exempt-period gap all survive untouched. An org with
                      no rows this month gets nothing; tonight's sweep starts
                      it normally.
    the DEVICE COUNT— what was measured on that day, a fact about that day.
    the RATES       — each row keeps the rate it was charged at, so this tool
                      can never launder a price change backwards. Rate changes
                      apply FORWARD ONLY (metering's invariant); if you want a
                      new rate on an open month, that is a separate, explicit
                      decision and not this script.

Past days are re-counted at TODAY's measurement — the same approximation the
cutover backfill made, and for the same reason: nothing recorded a per-day
history of the billable count before the ledger existed. Every rewritten row
is stamped `{"repriced": {"on": <day>, "from": <old source>}}` so the month
stays auditable and the SPA prints why the row moved.

REFUSES an invoiced month outright (store.clear_month_accruals raises): an
invoice is the SUM of its stored rows and is never recomputed, so rewriting
rows under an issued bill would detach the bill from the chart it must equal.

    .venv/bin/python tools/billing_reprice_month.py                # dry run
    .venv/bin/python tools/billing_reprice_month.py --apply
    .venv/bin/python tools/billing_reprice_month.py --org byreddy --apply

Run it AFTER central is restarted on the new code, so the basis it measures on
is the basis the dashboard shows.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wisp.central import metering  # noqa: E402
from wisp.central.billing import BillingSweeper, operator_today  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.config import CONFIG, Config  # noqa: E402

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def plan_org(sweeper: BillingSweeper, org: dict, month: str,
             now: datetime) -> dict | None:
    """The month's rows as the current basis would price them."""
    org_id = org["org_id"]
    existing = sweeper.store.accruals_for_month(org_id, month)
    if not existing:
        return None

    count, source, _flags, _eff = metering.resolve_count(
        sweeper.onu_source(org_id, now), None)

    stamp = operator_today(sweeper.cfg, now).isoformat()
    rows, was = [], sum(int(a["paise"]) for a in existing)
    for old in existing:
        # The flags a row already carried stay: how a day arrived (backfilled,
        # cutover) is still true after it is re-priced. Only the basis moved.
        flags = {k: v for k, v in (old.get("flags") or {}).items()
                 if k in ("backfilled", "cutover")}
        flags["repriced"] = {"on": stamp, "from": old.get("conn_source")}
        rows.append(metering.accrue_day(
            old["day"], count, source, flags,
            int(old["device_count"]), int(old["conn_rate_paise"]),
            int(old["floor_paise"])))
    now_paise = sum(r.paise for r in rows)
    return {"org_id": org_id, "name": org.get("name") or org_id,
            "count": count, "source": source,
            "was_count": max((int(a["conn_count"]) for a in existing),
                             default=0),
            "was_source": existing[-1].get("conn_source"),
            "rows": rows, "was": was, "paise": now_paise,
            "delta": now_paise - was}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="central DB (default: config)")
    ap.add_argument("--month", default=None, help="YYYY-MM (default: this month)")
    ap.add_argument("--org", default=None, help="one org (default: every org)")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the rows. Without this it only reports.")
    args = ap.parse_args()

    cfg = Config(central_db=Path(args.db)) if args.db else CONFIG
    # PRINT THE RESOLVED PATH BEFORE OPENING IT. Every store-touching script
    # here defaults to data/central.db, which IS production.
    print(f"db: {Path(cfg.central_db).resolve()}")
    store = CentralStore(cfg.central_db)
    sweeper = BillingSweeper(store, cfg)
    now = datetime.now(timezone.utc)
    today = operator_today(cfg, now)
    month = args.month or metering.month_key(today)
    if not _MONTH_RE.match(month):
        print(f"--month must be YYYY-MM, got {month!r}")
        raise SystemExit(2)

    orgs = [o for o in store.billing_org_rows()
            if not args.org or o["org_id"] == args.org]
    if args.org and not orgs:
        print(f"no such org: {args.org}")
        raise SystemExit(1)

    print(f"re-price · {metering.month_label(month)} · basis "
          f"{'/'.join(metering.SOURCE_LADDER)} · operator today "
          f"{today.isoformat()} · {'APPLY' if args.apply else 'DRY RUN'}\n")

    was_total = now_total = 0
    plans = []
    for org in orgs:
        # An invoiced month is refused per-org rather than aborting the run:
        # one closed month must not stop the others from being corrected.
        if store.org_invoice(org["org_id"], month):
            print(f"  {org['org_id']:<16} REFUSED · already invoiced")
            continue
        plan = plan_org(sweeper, org, month, now)
        if plan is None:
            print(f"  {org['org_id']:<16} nothing accrued this month")
            continue
        plans.append(plan)
        was_total += plan["was"]
        now_total += plan["paise"]
        arrow = "+" if plan["delta"] > 0 else ""
        print(f"  {plan['org_id']:<16} {len(plan['rows']):>2} days  "
              f"{plan['was_count']:>5} {str(plan['was_source'] or '-'):<8}"
              f"-> {plan['count']:>5} {plan['source']:<6}  "
              f"{_rupees(plan['was']):>13} -> {_rupees(plan['paise']):>13}  "
              f"({arrow}{_rupees(plan['delta']).replace('Rs ', 'Rs ')})")

    delta = now_total - was_total
    print(f"\n  month was {_rupees(was_total)} · now {_rupees(now_total)} · "
          f"{'+' if delta > 0 else ''}{_rupees(delta)}")

    if not args.apply:
        print("  dry run. Re-run with --apply to rewrite these rows.")
        return

    wrote = 0
    for plan in plans:
        # Delete and re-insert inside the same pass per org: insert_accrual is
        # INSERT OR IGNORE on (org, day), so the old row has to go first or the
        # new price is silently dropped and the run reports success.
        removed = store.clear_month_accruals(plan["org_id"], month)
        for row in plan["rows"]:
            if store.insert_accrual(plan["org_id"], row):
                wrote += 1
        if removed != len(plan["rows"]):
            print(f"  ! {plan['org_id']}: removed {removed} rows, wrote "
                  f"{len(plan['rows'])} — check this org before invoicing")
    print(f"  rewrote {wrote} accrual rows")
    print("  this month stays open and will invoice on the 1st as usual")


if __name__ == "__main__":
    main()
