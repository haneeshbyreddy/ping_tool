"""Server-rendered invoice PDF for billing v2 (metered postpaid ledger).

Pure stdlib, composed from wisp.central.pdf's primitives (WinAnsi escaping,
content-measured column widths, the xref-safe builder). pdf.py itself is
untouched: the issues PDF depends on it byte-for-byte.

The cp1252 trap this module exists around: the rupee sign U+20B9 does NOT
encode in cp1252 (the fonts are WinAnsi), so every amount renders as
"Rs 1,847.50" and the glyph never enters the document. Money arrives as
integer paise (metering.py's contract); rupees exist only at display time.

Layout is a simple top-down flow across landscape pages: header with a status
chip, the daily charge table, the invoice's own total (printed, never
recomputed), payments received, the account outstanding line, and a two-line
formula footer. Tables re-draw their header band after a page break.
"""

from __future__ import annotations

from wisp.central.metering import month_label
from wisp.central.pdf import (MARGIN, PAGE_H, PAGE_W, Column, _PAD, _build,
                              _cell, _esc, _measure, _solve_widths, fit,
                              text_width)

_BOTTOM = MARGIN + 16.0          # keep clear of the page-number footer
_HEAD_H = 16.0
_BODY_SIZE = 8.5

# Numeric columns draw against their right edge (an invoice's amounts align).
_RIGHT_KEYS = frozenset({"conns", "devices", "charge", "amount"})

_DAY_COLUMNS = (
    Column("day", "Day", 1.0, mono=True),
    Column("conns", "ONUs", 1.0),
    Column("source", "Source", 1.6),
    Column("devices", "Devices", 1.0),
    Column("charge", "Charge", 1.4),
)

_PAY_COLUMNS = (
    Column("when", "Date", 1.4),
    Column("kind", "Kind", 1.0),
    Column("ref", "Reference", 2.0, mono=True),
    Column("amount", "Amount", 1.2),
)

_FORMULA = ("Daily charge is the greater of ONUs x ONU rate and "
            "devices x device floor, divided by days in the month.")


def format_paise(paise: int) -> str:
    """Integer paise to a cp1252-safe money string: 184750 -> "Rs 1,847.50".

    Two decimals only when a paise remainder exists; plain western thousands
    grouping; a negative amount leads with the sign ("-Rs 25").
    """
    p = int(paise)
    sign = "-" if p < 0 else ""
    rupees, rem = divmod(abs(p), 100)
    body = f"{rupees:,}"
    if rem:
        body += f".{rem:02d}"
    return f"{sign}Rs {body}"


def _source_text(row: dict) -> str:
    flags = row.get("flags") or {}
    src = str(row.get("conn_source") or "none")
    if src == "held" and flags.get("held"):
        src = f"held ({flags['held']})"
    if flags.get("backfilled"):
        src += " · backfilled"
    return src


def _charge_text(row: dict) -> str:
    charge = format_paise(int(row.get("paise") or 0))
    if row.get("winning_side") == "floor":
        charge += " (floor)"
    return charge


class _Flow:
    """A top-down cursor over landscape pages; ops stay strings until build
    so every page can carry its "page N of M" footer once M is known."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = PAGE_H - MARGIN

    @property
    def ops(self) -> list[str]:
        return self.pages[-1]

    def break_page(self) -> None:
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    def need(self, height: float) -> None:
        if self.y - height < _BOTTOM:
            self.break_page()

    def line(self, s: str, size: float, *, font: str = "/F1",
             gray: float = 0.10, gap: float = 4.0) -> None:
        self.need(size + gap)
        self.ops.append("%.2f %.2f %.2f rg" % (gray, gray, gray))
        self.ops.append("BT %s %.1f Tf %.1f %.1f Td (%s) Tj ET"
                        % (font, size, MARGIN, self.y - size, _esc(s)))
        self.y -= size + gap

    def build(self, footer: str) -> bytes:
        n = len(self.pages)
        streams = []
        for page_no, ops in enumerate(self.pages, start=1):
            foot = f"{footer}   ·   page {page_no} of {n}"
            tail = ["0.45 0.45 0.45 rg",
                    "BT /F1 7.5 Tf %.1f %.1f Td (%s) Tj ET"
                    % (MARGIN, MARGIN, _esc(foot))]
            streams.append("\n".join(ops + tail).encode("cp1252", "replace"))
        return _build(streams)


def _draw_table(flow: _Flow, columns: tuple[Column, ...],
                rows: list[dict]) -> None:
    size = _BODY_SIZE
    avail = PAGE_W - 2 * MARGIN
    widths = _solve_widths(list(columns), rows, avail, size)
    xs, x = [], MARGIN
    for w in widths:
        xs.append(x)
        x += w
    row_h = size + 6.0

    def header() -> None:
        y = flow.y
        flow.ops.append("0.90 0.90 0.90 rg %.1f %.1f %.1f %.1f re f"
                        % (MARGIN, y - _HEAD_H + 3, avail, _HEAD_H - 2))
        flow.ops.append("0.10 0.10 0.10 rg")
        for col, cx, cw in zip(columns, xs, widths):
            t = fit(col.title, size, cw - _PAD)
            tx = cx + 3
            if col.key in _RIGHT_KEYS:
                tx = cx + cw - 3 - text_width(t, size) * 1.06
            flow.ops.append("BT /F2 %.1f Tf %.1f %.1f Td (%s) Tj ET"
                            % (size, tx, y - _HEAD_H + 8, _esc(t)))
        flow.y -= _HEAD_H + 2

    flow.need(_HEAD_H + row_h + 2)
    header()
    for i, r in enumerate(rows):
        if flow.y - row_h < _BOTTOM:
            flow.break_page()
            header()
        if i % 2 == 1:
            flow.ops.append("0.965 0.965 0.965 rg %.1f %.1f %.1f %.1f re f"
                            % (MARGIN, flow.y - row_h + 4, avail, row_h))
        flow.ops.append("0.10 0.10 0.10 rg")
        for col, cx, cw in zip(columns, xs, widths):
            font = "/F3" if col.mono else "/F1"
            t = fit(_cell(r, col), size, cw - _PAD, mono=col.mono)
            tx = cx + 3
            if col.key in _RIGHT_KEYS:
                tx = cx + cw - 3 - _measure(t, col, size)
            flow.ops.append("BT %s %.1f Tf %.1f %.1f Td (%s) Tj ET"
                            % (font, size, tx, flow.y - size + 1, _esc(t)))
        flow.y -= row_h


def _chip(flow: _Flow, label: str) -> None:
    size, pad, h = 8.0, 5.0, 13.0
    w = text_width(label, size) * 1.06 + 2 * pad
    x = PAGE_W - MARGIN - w
    y = flow.y - h
    flow.ops.append("0.90 0.90 0.90 rg %.1f %.1f %.1f %.1f re f"
                    % (x, y, w, h))
    flow.ops.append("0.10 0.10 0.10 rg")
    flow.ops.append("BT /F2 %.1f Tf %.1f %.1f Td (%s) Tj ET"
                    % (size, x + pad, y + 3.5, _esc(label)))


def _month_text(month: str) -> str:
    try:
        return month_label(month)
    except (ValueError, IndexError):
        return month or "-"


def render_invoice(*, org_name: str, org_id: str, invoice: dict,
                   accruals: list[dict], payments: list[dict],
                   outstanding_paise: int, tz_name: str) -> bytes:
    from wisp.egress.notifiers import _wa_time  # deferred, as in api/outages

    flow = _Flow()
    label = _month_text(str(invoice.get("month") or ""))
    name = str(org_name or "").strip() or str(org_id)
    status = str(invoice.get("status") or "open").upper()

    # -- header: title, status chip, org + issued line -----------------------
    top = flow.y
    flow.ops.append("0.10 0.10 0.10 rg")
    flow.ops.append("BT /F2 15.0 Tf %.1f %.1f Td (%s) Tj ET"
                    % (MARGIN, top - 15.0, _esc(f"Invoice · {label}")))
    _chip(flow, status)
    flow.y = top - 15.0 - 6.0
    issued = _wa_time(invoice.get("issued_at") or "", tz_name)
    org_line = name + (f" · issued {issued}" if issued else "")
    flow.line(org_line, 9.0, gray=0.30, gap=8.0)

    # -- daily charges -------------------------------------------------------
    flow.need(60.0)
    flow.line("Daily charges", 10.0, font="/F2", gap=6.0)
    day_rows = [{
        "day": a.get("day") or None,
        "conns": str(int(a.get("conn_count") or 0)),
        "source": _source_text(a),
        "devices": str(int(a.get("device_count") or 0)),
        "charge": _charge_text(a),
    } for a in accruals]
    _draw_table(flow, _DAY_COLUMNS, day_rows)

    # The invoice's own number, printed verbatim: never a recomputation.
    flow.y -= 4.0
    flow.line(f"Total for {label}: "
              f"{format_paise(int(invoice.get('paise') or 0))}",
              10.0, font="/F2", gap=6.0)

    # -- payments received ---------------------------------------------------
    flow.y -= 6.0
    flow.need(60.0)
    flow.line("Payments received", 10.0, font="/F2", gap=6.0)
    if payments:
        pay_rows = [{
            "when": _wa_time(p.get("created_at") or "", tz_name) or None,
            "kind": str(p.get("kind") or "") or None,
            "ref": p.get("provider_payment_id") or p.get("note") or None,
            "amount": format_paise(int(p.get("paise") or 0)),
        } for p in payments]
        _draw_table(flow, _PAY_COLUMNS, pay_rows)
    else:
        flow.line("No payments recorded.", 9.0, gray=0.35, gap=4.0)

    # -- account position ----------------------------------------------------
    flow.y -= 6.0
    out = int(outstanding_paise)
    if out < 0:
        position = f"Account in credit: {format_paise(-out)}"
    else:
        position = f"Outstanding across the account: {format_paise(out)}"
    flow.line(position, 10.0, font="/F2", gap=6.0)

    # -- formula footer ------------------------------------------------------
    flow.y -= 8.0
    flow.need(26.0)
    flow.line(_FORMULA, 8.0, gray=0.35, gap=4.0)
    if accruals:
        last = accruals[-1]
        flow.line(f"This month: "
                  f"{format_paise(int(last.get('conn_rate_paise') or 0))} "
                  f"per ONU, "
                  f"{format_paise(int(last.get('floor_paise') or 0))} "
                  f"per device floor.",
                  8.0, gray=0.35, gap=4.0)

    return flow.build(f"WISP Central · {name}")
