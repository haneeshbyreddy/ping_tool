"""Metering math for the postpaid ledger — pure, like the FSM.

Counts + config in, an accrual row out. No I/O, no store handle, no clock
reads: central/billing.py owns the queries, the persistence and the sweep
cadence; this module owns every decision that turns counts into money, so it
unit-tests the way core/state_machine does (tests/unit/test_metering).

Money is INTEGER PAISE everywhere. Rupees exist only at display time.

THE BILL IS PER ONU (operator decision 2026-08-17, replacing the RADIUS
username count): the billable connection is a subscriber ONU seen online in
the last ONU_ONLINE_WINDOW_DAYS, counted by distinct MAC off the roster the
OLT walks. RADIUS is no longer a metering input at all — the two disagreed by
a third in both directions across the fleet (one org billed 839 usernames
against 405 live ONUs, another 0 against 800), and the ONU is the thing this
product actually measures.

The one measuring rung is LATCHED: an OLT fleet that stops answering holds its
last good count for up to HOLD_DAYS before the ladder falls through to zero —
a walk that breaks must never silently move a bill. That matters MORE now than
it did with two rungs, because there is nothing underneath to catch it except
the device floor. Every hold, fall-through and source change is recorded on the
accrual row's flags.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# Global defaults; superadmin overrides live in app_settings
# (billing_conn_paise / billing_device_floor_paise), per-org overrides on the
# orgs row. Override wins; rate changes apply forward only.
DEFAULT_CONN_PAISE = 300        # Rs 3 per billable ONU per month
DEFAULT_FLOOR_PAISE = 10000     # Rs 100 per monitored device per month

# Closed vocabularies. 'held' means "the roster is not answering today and this
# is its last good count"; 'none' means "nothing could answer at all" (no
# roster, or every OLT stale past the hold) — an honest zero, never a guess.
CONN_SOURCES = ("onu", "held", "none")
WINNING_SIDES = ("conn", "floor")

# The ladder rungs in priority order (the value stored in flags/"effective").
# ONE measuring rung by design: a second one is a second answer, and a bill
# that can quietly move between two answers is the failure this whole latch
# exists to prevent.
SOURCE_LADDER = ("onu", "none")

# Written by the pre-2026-08-17 ladder, which metered RADIUS usernames and
# could fall back to a hand-typed number. NOTHING writes these any more; they
# stay READABLE so an old accrual row still renders as what it was charged on
# rather than as "no source" (the dead-column house rule, applied to a stored
# vocabulary). Never add one back to SOURCE_LADDER.
RETIRED_CONN_SOURCES = ("radius", "declared")

# A source whose last good read is older than this is "broken today" — its
# count is HELD (OLT roster walks run every ~300 s, so a full day of slack
# absorbs restarts and rollouts without ever flagging a healthy org).
LIVE_AGE_DAYS = 1.0
# ...and past this the hold expires and the ladder falls through, flagged.
HOLD_DAYS = 7
# An ONU is billable if it was online within this window (7 not 30: a 30-day
# window double-counts every RMA'd box for a month; 7 still covers the evening
# dark-out). This is the whole definition of a billable connection now, so
# widening it is a PRICE CHANGE, not a tuning knob.
ONU_ONLINE_WINDOW_DAYS = 7

# Dunning ladder day math, anchored to the INVOICE, never to outstanding
# (postpaid means outstanding is nonzero from day one of usage). The invoice
# issues on the 1st = overdue day 1; days 1..3 banner; day 4+ locks; day 60
# puts the org on the superadmin deactivation LIST (deactivation itself is
# always a human click).
BANNER_DAYS = 3
DEACTIVATE_LIST_DAYS = 60

# Carry-forward backfill after central downtime is bounded: a gap longer than
# this stays a gap (charging a month of unknown days blind is worse than an
# honest hole in the ledger).
BACKFILL_MAX_DAYS = 31


# ---------------------------------------------------------------- month math

def month_key(dt: datetime | date) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def month_of_day(day: str) -> str:
    return day[:7]


def next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + (m == 12)}-{(m % 12) + 1:02d}"


def prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def month_label(month: str) -> str:
    return f"{calendar.month_name[int(month[5:7])]} {month[:4]}"


def days_in_month(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


# ---------------------------------------------------------------- daily money

def daily_paise(conn_count: int, device_count: int, conn_rate_paise: int,
                floor_paise: int, days: int) -> tuple[int, str]:
    """One operator-day's charge: max(ONUs, device floor) / days.

    Integer half-up division, rounded exactly ONCE per row — the invoice is
    the SUM of its stored rows, never recomputed, so the chart and the
    invoice cannot disagree. A tie goes to 'conn' (billed per ONU is the
    headline story; the floor is the backstop that keeps an org whose OLTs
    report no roster from paying nothing for monitored gear).
    """
    conn_side = max(0, int(conn_count)) * max(0, int(conn_rate_paise))
    floor_side = max(0, int(device_count)) * max(0, int(floor_paise))
    monthly = max(conn_side, floor_side)
    side = "conn" if conn_side >= floor_side else "floor"
    return (monthly * 2 + days) // (days * 2), side


# ------------------------------------------------------------- source ladder

@dataclass(frozen=True)
class Source:
    """The roster's health as the engine measured it.

    present  — the org has a roster at all (any ONU row under a live OLT)
    count    — what it answers right now; for a fleet that has stopped walking
               this is naturally its last good read (the roster keeps its rows)
    age_days — since the newest successful walk; None = never succeeded
    """
    present: bool
    count: int
    age_days: float | None


def resolve_count(onu: Source | None,
                  prior_effective: str | None) -> tuple[int, str, dict, str]:
    """Read the one rung. Returns (count, conn_source, flags, effective).

    conn_source is what the accrual row stores (CONN_SOURCES); effective is
    the ladder rung the number actually came from — 'held' is a condition of
    a rung, not a rung, and change/downgrade detection compares rungs.

    A roster that has never answered is skipped rather than believed at zero:
    "we have not measured this org" and "this org has no subscribers online"
    are different claims, and only the second one is worth billing on. Both
    land on the device floor; only the second says 'onu' on the row.
    """
    flags: dict = {}
    count, stored, effective = 0, "none", "none"
    if (onu is not None and onu.present and onu.age_days is not None
            and onu.age_days <= HOLD_DAYS):
        count, effective = max(0, int(onu.count)), "onu"
        if onu.age_days > LIVE_AGE_DAYS:
            stored = "held"
            flags["held"] = "onu"
        else:
            stored = "onu"

    # A prior rung outside SOURCE_LADDER (a row the retired RADIUS ladder
    # wrote) sorts last, so the cutover reads as a source CHANGE and never as
    # a downgrade — the basis moved by decision, not by a broken feed.
    if prior_effective and prior_effective != effective:
        flags["source_changed"] = {"from": prior_effective, "to": effective}
        order = {name: i for i, name in enumerate(SOURCE_LADDER)}
        if order.get(effective, len(order)) > order.get(prior_effective, len(order)):
            flags["downgraded"] = {"from": prior_effective, "to": effective}
    return count, stored, flags, effective


def effective_source(conn_source: str, flags: dict | None) -> str:
    """The ladder rung behind a stored row ('held' resolves to its rung)."""
    if conn_source == "held":
        return (flags or {}).get("held") or "none"
    return conn_source


# ------------------------------------------------------------- accrual rows

@dataclass(frozen=True)
class AccrualRow:
    day: str                 # YYYY-MM-DD, the OPERATOR's day (WISP_DISPLAY_TZ)
    paise: int
    conn_count: int
    conn_source: str
    device_count: int
    winning_side: str
    conn_rate_paise: int
    floor_paise: int
    flags: dict


def accrue_day(day: str, conn_count: int, conn_source: str, flags: dict,
               device_count: int, conn_rate_paise: int,
               floor_paise: int) -> AccrualRow:
    paise, side = daily_paise(conn_count, device_count, conn_rate_paise,
                              floor_paise, days_in_month(month_of_day(day)))
    return AccrualRow(day=day, paise=paise, conn_count=max(0, int(conn_count)),
                      conn_source=conn_source,
                      device_count=max(0, int(device_count)),
                      winning_side=side,
                      conn_rate_paise=int(conn_rate_paise),
                      floor_paise=int(floor_paise), flags=dict(flags or {}))


def carry_forward(prior: AccrualRow, day: str) -> AccrualRow:
    """A backfilled day after central downtime: the prior row's counts AND
    rates continue (rate changes apply forward only, and a day computed late
    must not retroactively ride a rate that changed during the outage).
    Recharged against the target month's own day count."""
    row = accrue_day(day, prior.conn_count, prior.conn_source, {"backfilled": True},
                     prior.device_count, prior.conn_rate_paise, prior.floor_paise)
    if prior.conn_source == "held" and prior.flags.get("held"):
        row.flags["held"] = prior.flags["held"]
    return row


# ------------------------------------------------------------ dunning ladder

def days_overdue(invoice_month: str, today: date) -> int:
    """The invoice for a month issues on the 1st of the NEXT month; that day
    is overdue day 1. Sign up on the 20th: 11 days accrue, invoice on the
    1st, first possible lock the 4th."""
    issue = month_start(next_month(invoice_month))
    return max(0, (today - issue).days + 1)


def ladder_stage(open_month: str | None, today: date, *,
                 exempt: bool = False, deactivated: bool = False) -> dict:
    """The org's position on the ladder, from its OLDEST open invoice."""
    if deactivated:
        return {"stage": "deactivated", "locked": True, "days_overdue": 0,
                "deactivation_candidate": False}
    if exempt:
        return {"stage": "exempt", "locked": False, "days_overdue": 0,
                "deactivation_candidate": False}
    if not open_month:
        return {"stage": "clear", "locked": False, "days_overdue": 0,
                "deactivation_candidate": False}
    d = days_overdue(open_month, today)
    stage = "clear" if d == 0 else ("banner" if d <= BANNER_DAYS else "locked")
    return {"stage": stage, "locked": d > BANNER_DAYS, "days_overdue": d,
            "deactivation_candidate": d >= DEACTIVATE_LIST_DAYS}


# ------------------------------------------------------------------- credit

def credit_lasts_until(credit_paise: int, daily_rate_paise: int,
                       today: date) -> date | None:
    """Advance payment IS the credit mechanism: a projection of when it runs
    out at the current daily rate. None when there is no meaningful answer
    (no credit, or nothing accruing — credit against a zero rate lasts
    forever and printing a date would be a lie)."""
    if credit_paise <= 0 or daily_rate_paise <= 0:
        return None
    return today + timedelta(days=credit_paise // daily_rate_paise)
