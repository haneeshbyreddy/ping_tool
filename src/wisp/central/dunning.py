"""Dunning: the paging half of billing v2 (the ladder's WhatsApp side).

Called once per BillingSweeper tick. The SPA banner and the 402 lock are
read-side (billing.org_status / org_locked); THIS module owns what gets SENT:
per-org overdue-invoice pages to the org owners, and ONE daily digest to the
superadmin ops number (overdue orgs, source-downgrade flags, deactivation
candidates) — never per-org admin pings.

Rules it must keep:
  * dedupe per (org, invoice month, kind) via store.billing_notice /
    record_billing_notice (only 'sent'/'skipped' suppress; 'failed' retries);
  * an org in credit gets ZERO dunning (the projection IS the no-reminder
    switch); exempt and deactivated orgs likewise;
  * a send can never crash the sweep (nothing raises out of here);
  * copy follows the house style — periods and colons, no prose em-dashes.

Two things this module deliberately does NOT do. It does not route through
notify_policy.AlertRouter: the governor's vocabulary is device/outage alert
KINDS and billing has neither a device nor an outage, so a billing notice
would have to fake both to be logged; its dedupe ledger is billing_notices,
which is also the retry state. And it never deactivates anything — the digest
LISTS candidates, the superadmin clicks.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from wisp.central import metering
from wisp.central.billing_pdf import format_paise
from wisp.config import CONFIG, Config
from wisp.egress.notifiers import WhatsAppFacts, build_notifier

log = logging.getLogger("wisp.central.dunning")

# The ladder's RUNGS: a closed vocabulary keyed off metering's day math, so a
# page and the SPA banner can never disagree about where an org stands.
#   issued  · days 1..BANNER_DAYS. The invoice exists and is due; the
#             dashboard is still open.
#   overdue · past BANNER_DAYS. org_locked() now 402s every /api call, which
#             is the one fact the owner cannot read off the ledger himself.
#   final   · DEACTIVATE_LIST_DAYS+. The account is on the superadmin's
#             deactivation list (a human still has to click).
# Three and no more: with the (org, month, kind) dedupe each rung fires once,
# so an unpaid invoice costs an owner exactly three pages in sixty days. The
# rungs are not back-filled (_kind_for answers for TODAY only) — an org first
# seen on day 61 gets 'final', never a stale "your invoice is due".
ISSUED, OVERDUE, FINAL = "issued", "overdue", "final"
KINDS = (ISSUED, OVERDUE, FINAL)

# The daily digest rides the SAME billing_notices ledger (one dedupe
# mechanism in this module, not two). Its key is (DIGEST_ORG, operator day,
# 'digest'):
#   * the digest is about a DAY, not an invoice month, so the operator's own
#     date goes in the month slot. The table's PK then makes the ~48 sweeps
#     in a day idempotent for free, with no new table and no new state;
#   * '*' is not a legal org id (inventory.clean_org_id rejects it), so the
#     row can never collide with a real org's notice, and org deletion (which
#     sweeps every table carrying an org_id) can never take the ops record
#     with it.
DIGEST_ORG = "*"
DIGEST_KIND = "digest"

# How many orgs a digest clause names before it collapses to "+N more". The
# digest is a nudge to open the dashboard, not the dashboard.
_LISTED = 3

# HashRouter, so the route lives after the '#'. Billing has its own top-level
# route (App.tsx: <Route path="billing">); the old #/settings/billing section
# is GONE, and a link into a route the SPA has no match for renders a blank
# page. Verify this path still resolves before changing either side.
_BILLING_PATH = "/app#/billing"


def billing_link(cfg: Config = CONFIG) -> str:
    """The link the owner taps.

    There is no public-URL field on Config. cfg.central_url is the one
    address the install already knows itself by, and where it is unset
    (central rarely sets WISP_CENTRAL_URL — it is the EDGE's mandatory var)
    the copy degrades to the bare path rather than inventing a host: a
    plausible wrong link is worse than an honest partial one.
    """
    base = str(getattr(cfg, "central_url", "") or "").strip().rstrip("/")
    return f"{base}{_BILLING_PATH}" if base else _BILLING_PATH


def _kind_for(days: int) -> str | None:
    """The ONE rung an invoice sits on today, or None below the ladder."""
    if days >= metering.DEACTIVATE_LIST_DAYS:
        return FINAL
    if days > metering.BANNER_DAYS:
        return OVERDUE
    return ISSUED if days >= 1 else None


def _more(total: int) -> str:
    return f" +{total - _LISTED} more" if total > _LISTED else ""


def _overdue(row: dict) -> bool:
    """The ONE predicate both halves read, so a page and the digest can never
    disagree about who is overdue.

    Anchored to the INVOICE, never to a balance: postpaid means outstanding
    is nonzero from day one of usage, so keying on the balance would dun a
    brand-new signup for existing.

    Credit IS the no-reminder switch (advance payment is how this ledger
    models prepayment), and credit is a NEGATIVE balance, exactly as
    org_status reads it (credit_paise = max(0, -outstanding)). A balance
    sitting at zero beside an open invoice is not prepayment; settle_invoices
    runs earlier in the same sweep, so a genuinely squared account has no
    open invoice left by the time this reads.
    """
    return (row["invoice"] is not None and row["days"] >= 1
            and row["outstanding"] >= 0)


# ----------------------------------------------------------------- the send

def _send(notifier, numbers, title: str, body: str, priority: int,
          facts: WhatsAppFacts) -> str:
    """Send and answer with the billing_notices status.

    INLINE, not queue_send: the ledger row IS the retry (a 'failed' row
    re-pages next tick), and that needs the result in hand. Same argument the
    watchdog makes for staying inline. Nothing raises out.
    """
    if not numbers:
        return "skipped"
    try:
        res = notifier.send(title, body, priority, whatsapp=numbers,
                            facts=facts)
        return "sent" if getattr(res, "ok", False) else "failed"
    except Exception:
        log.exception("dunning send raised; recorded as failed (retries)")
        return "failed"


# ------------------------------------------------------------ per-org pages

def _copy(row: dict, kind: str, month: str, invoice_paise: int,
          link: str) -> tuple[str, str, str, int]:
    """(title, template status, body, priority). Rupees exist only here."""
    label = metering.month_label(month)
    amount = format_paise(invoice_paise)
    pay = f"Pay from your dashboard: {link}"
    # The invoice is ONE month; outstanding is the whole account. Printing
    # both only when they differ keeps the common case a single number.
    extra = (f" Total outstanding: {format_paise(row['outstanding'])}."
             if row["outstanding"] > invoice_paise else "")
    name, days = row["name"], row["days"]
    if kind == ISSUED:
        return (f"💳 {name}: invoice for {label}", "INVOICE DUE",
                f"{label} invoice: {amount}.{extra} {pay}", 3)
    if kind == OVERDUE:
        # "Monitoring and alerts keep running" is load-bearing, not comfort:
        # edge ingest and paging are never gated, and an owner who assumes
        # otherwise stops trusting a page that is still arriving.
        return (f"🔒 {name}: dashboard locked", "OVERDUE",
                f"{label} invoice: {amount}, {days} days overdue. The "
                f"dashboard is locked until it is paid. Monitoring and "
                f"alerts keep running.{extra} {pay}", 4)
    return (f"⚠️ {name}: final notice", "FINAL NOTICE",
            f"{label} invoice: {amount}, {days} days overdue. The account "
            f"is on the deactivation list.{extra} {pay}", 5)


def _page(store, notifier, row: dict, link: str,
          stamp: str) -> tuple[str, str, str] | None:
    """At most one page per org per tick. Returns the (org, month, kind) of a
    page that actually went out."""
    if not _overdue(row):
        return None
    kind = _kind_for(row["days"])
    if kind is None:
        return None            # the ladder's own floor, not dead code
    inv = row["invoice"]
    month, org_id = str(inv["month"]), row["org_id"]
    # 'failed' deliberately falls through and re-pages: a page must not
    # vanish to a blip.
    if store.billing_notice(org_id, month, kind) in ("sent", "skipped"):
        return None
    title, status, body, priority = _copy(
        row, kind, month, int(inv["paise"] or 0), link)
    result = _send(notifier, row["numbers"], title, body, priority,
                   WhatsAppFacts(subject=row["name"], status=status,
                                 detail=body, timestamp=stamp))
    store.record_billing_notice(org_id, month, kind, result, stamp)
    return (org_id, month, kind) if result == "sent" else None


# ------------------------------------------------------------- daily digest

def _flag_clauses(rows: list[dict]) -> list[str]:
    """Today's accrual flags, grouped. A downgrade means the ladder fell to a
    weaker source and the bill moved with it, so it is named before the hold
    that usually precedes it; 'source changed' covers the lateral and upward
    moves, which are news but not alarm."""
    down: list[str] = []
    held: list[str] = []
    moved: list[str] = []
    for r in rows:
        flags = r["flags"] or {}
        step = flags.get("downgraded") or {}
        if step:
            down.append(f"{r['name']} {step.get('from')} to {step.get('to')}")
        else:
            step = flags.get("source_changed") or {}
            if step:
                moved.append(
                    f"{r['name']} {step.get('from')} to {step.get('to')}")
        if flags.get("held"):
            held.append(f"{r['name']} {flags['held']}")
    out = []
    for label, items in (("downgraded", down), ("holding a stale source", held),
                         ("source changed", moved)):
        if items:
            out.append(f"{len(items)} {label}: "
                       f"{', '.join(items[:_LISTED])}{_more(len(items))}.")
    return out


def _digest_body(rows: list[dict]) -> str:
    """The digest text, or "" when there is nothing to say. A daily all-clear
    ping trains the operator to ignore the channel."""
    overdue = [r for r in rows if _overdue(r)]
    overdue.sort(key=lambda r: (-r["outstanding"], -r["days"]))
    parts: list[str] = []
    if overdue:
        total = sum(r["outstanding"] for r in overdue)
        worst = "; ".join(
            f"{r['name']} {format_paise(r['outstanding'])} ({r['days']}d)"
            for r in overdue[:_LISTED])
        parts.append(f"{len(overdue)} overdue, {format_paise(total)} owed."
                     f" Worst: {worst}{_more(len(overdue))}.")
        mute = [r["name"] for r in overdue if not r["numbers"]]
        if mute:
            # Reported, never widened around (the assignment rule): an org
            # nobody can be paged for otherwise looks exactly like one that
            # was paged and ignored.
            parts.append(f"{len(mute)} with no owner WhatsApp number: "
                         f"{', '.join(mute[:_LISTED])}{_more(len(mute))}.")
        # Candidates are drawn from the overdue set, never from the ladder
        # alone: an org that paid stops being a candidate the moment the
        # balance says so, whatever day count the invoice still carries.
        cand = [r for r in overdue if r["candidate"]]
        if cand:
            named = ", ".join(f"{r['name']} ({r['days']}d)"
                              for r in cand[:_LISTED])
            parts.append(
                f"Deactivation candidates "
                f"({metering.DEACTIVATE_LIST_DAYS}+ days): "
                f"{named}{_more(len(cand))}.")
    parts.extend(_flag_clauses(rows))
    return " · ".join(parts)


def _digest(store, cfg: Config, notifier, rows: list[dict], today: date,
            stamp: str) -> tuple[str, str, str] | None:
    day = today.isoformat()
    if store.billing_notice(DIGEST_ORG, day, DIGEST_KIND) in ("sent", "skipped"):
        return None
    body = _digest_body(rows)
    if not body:
        # A quiet tick writes NO row, so the day stays open and the first
        # tick that has news can still page. A "nothing to report" row would
        # swallow the afternoon's real news for the sake of a message nobody
        # wanted sent.
        return None
    # The superadmin ops number takes topic-less pings only and is NEVER in
    # an org audience; this is the same store-then-env read the release-sync
    # ping uses. Deferred so a paging module does not carry the GitHub mirror
    # on its import graph.
    from wisp.central.releasesync import _admin_numbers
    numbers = _admin_numbers(store, cfg)
    # today is already the operator's date (WISP_DISPLAY_TZ via
    # billing.operator_today), not a stored UTC stamp being formatted raw;
    # the send time itself rides the template's Time Logged slot, which
    # converts at notifiers._wa_time.
    title = f"📒 Billing digest · {today:%d %b}"
    result = _send(notifier, numbers, title, body, 3,
                   WhatsAppFacts(subject="Billing", status="BILLING DIGEST",
                                 detail=body, timestamp=stamp))
    store.record_billing_notice(DIGEST_ORG, day, DIGEST_KIND, result, stamp)
    return (DIGEST_ORG, day, DIGEST_KIND) if result == "sent" else None


# ------------------------------------------------------------------- the pass

def _look(store, org: dict, today: date) -> dict | None:
    """One org's dunning facts, or None for an org outside dunning entirely.

    Exempt owes nothing and deactivated is already off; neither accrues, so
    neither has flags to report either. Reads are lazy on purpose: the org
    with no open invoice (the healthy majority, every tick, forever) costs
    one indexed point read plus today's accrual row.
    """
    org_id = str(org.get("org_id") or "")
    if not org_id or org.get("billing_exempt") or org.get("deactivated"):
        return None
    invoice = store.oldest_open_invoice(org_id)
    ladder = metering.ladder_stage(
        invoice["month"] if invoice else None, today)
    row = {
        "org_id": org_id,
        "name": str(org.get("name") or "").strip() or org_id,
        "invoice": invoice,
        "outstanding": int(store.outstanding_paise(org_id)) if invoice else 0,
        "days": int(ladder["days_overdue"]),
        "candidate": bool(ladder["deactivation_candidate"]),
        "flags": (store.accrual_on(org_id, today.isoformat()) or {}).get(
            "flags") or {},
        "numbers": [],
    }
    # Owners only, and fetched only for an org that is actually overdue: the
    # set that can be paged and the set the digest reports as unreachable are
    # the same set. A billing notice has no device, and a device-less event
    # reaches owners only (the assignment rule); the org's balance is also
    # not the field team's business. Same owner set
    # PagingAudience.owners_only() composes, at one query instead of three.
    if _overdue(row):
        row["numbers"] = store.org_role_whatsapp(org_id, "owner")
    return row


def sweep(store, cfg: Config = CONFIG, notifier=None,
          now: datetime | None = None) -> list[tuple[str, str, str]]:
    """One dunning pass. Returns [(org_id, invoice_month, kind), ...] for
    every page actually sent this tick (the digest reports as
    (DIGEST_ORG, operator day, 'digest') — its key shape, so a caller logging
    the list prints what the ledger holds)."""
    now = now or datetime.now(timezone.utc)
    # Deferred: billing imports dunning inside its own sweep, so the edge
    # between them stays one-way at import time. operator_today is the one
    # definition of the billing day boundary and this module must not grow a
    # second one.
    from wisp.central.billing import operator_today

    notifier = notifier or build_notifier(cfg, store)
    today = operator_today(cfg, now)
    stamp = now.isoformat(timespec="seconds")
    link = billing_link(cfg)

    sent: list[tuple[str, str, str]] = []
    looked: list[dict] = []
    try:
        orgs = store.billing_org_rows()
    except Exception:
        log.exception("dunning could not read the org roster; next tick")
        return sent

    for org in orgs:
        org_id = str((org or {}).get("org_id") or "?")
        # Per-org, not per-pass: one unreadable org must not strand the
        # ledger for every other org on the box.
        try:
            row = _look(store, org, today)
        except Exception:
            log.exception("dunning could not read %s; next org", org_id)
            continue
        if row is None:
            continue
        looked.append(row)
        try:
            page = _page(store, notifier, row, link, stamp)
        except Exception:
            log.exception("dunning page failed for %s; next org", org_id)
            continue
        if page:
            sent.append(page)

    try:
        digest = _digest(store, cfg, notifier, looked, today, stamp)
        if digest:
            sent.append(digest)
    except Exception:
        log.exception("billing digest failed; will retry next tick")

    if sent:
        log.info("dunning: %s", ", ".join(f"{o}/{m}/{k}" for o, m, k in sent))
    return sent
