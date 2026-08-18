// The billing console's pure layer: what a chip filters to, what the daily
// digest says, how a column sorts, and where rupees become paise.
//
// Nothing here renders. It is separate because two surfaces have to agree
// about the same sets — the chips and the summary block are both composed
// from THIS file over the same `orgs` array, which is what makes the screen
// and the WhatsApp digest incapable of disagreeing.

import type { AccrualFlags, BillingConsoleOrg } from "@/lib/types"

// ─────────────────────────────────────────────────────────────── the ladder
// Mirrors central/dunning.py:_look — an exempt or deactivated org is OUT of
// dunning entirely (it accrues nothing, is paged about nothing and appears in
// no digest clause). Every predicate below that talks about lateness starts
// here, so the console cannot report an org the ladder has already excused.
export const inLadder = (r: BillingConsoleOrg): boolean =>
  !r.exempt && !r.deactivated

// dunning.py's overdue set, verbatim: an OPEN INVOICE at least a day old that
// the account balance still fails to cover. Anchored to the invoice and never
// to the balance alone, because postpaid means outstanding is nonzero from
// day one of usage and a brand-new signup is not late.
export const isOverdue = (r: BillingConsoleOrg): boolean =>
  inLadder(r) && !!r.open_invoice && r.days_overdue >= 1
  && r.outstanding_paise > 0

// Drawn from the OVERDUE set, never from the day count alone: an org that
// paid stops being a candidate the moment the balance says so, whatever the
// invoice month still implies.
export const isCandidate = (r: BillingConsoleOrg): boolean =>
  isOverdue(r) && r.deactivation_candidate

// ──────────────────────────────────────────────────────────────── the flags
// The one thing that happened to today's count, ranked the way
// lib/billing.ts:accrualFlagNote ranks it: a re-price outranks a downgrade
// outranks a hold outranks a lateral move, because that is the order in which
// they move a bill. A downgrade is why this console exists — an OLT fleet that
// stops walking must never silently drop an org onto a cheaper count.
export type FlagKind =
  | "repriced" | "downgraded" | "held" | "source_changed" | "backfilled"

export function flagKind(f: AccrualFlags | undefined): FlagKind | null {
  if (!f) return null
  if (f.repriced) return "repriced"
  if (f.downgraded) return "downgraded"
  if (f.source_changed) return "source_changed"
  if (f.held) return "held"
  if (f.backfilled) return "backfilled"
  return null
}

export const FLAG_WORD: Record<FlagKind, string> = {
  repriced: "Re-priced",
  downgraded: "Downgraded",
  source_changed: "Source changed",
  held: "Holding",
  backfilled: "Backfilled",
}

// A meter fault an operator has to answer for. `backfilled` and `repriced`
// are deliberately NOT ones: filling a gap after central was down is the sweep
// working, and a re-price is a decision somebody made on purpose. A chip that
// counted either would bury the three that mean something.
export const isFlagged = (r: BillingConsoleOrg): boolean => {
  if (!inLadder(r)) return false
  const k = flagKind(r.today?.flags)
  return k === "downgraded" || k === "held" || k === "source_changed"
}

// ───────────────────────────────────────────────────────────────── the chips
// A CLOSED set, and every count is a recount of the rows the chip filters to
// (the /issues rule). Nothing here reads a server total: the tile and the
// list it opens are the same pass over the same array by construction.
export type ConsoleFilter =
  | "overdue" | "locked" | "candidates" | "flagged"
  | "credit" | "exempt" | "deactivated"

export const FILTERS: Record<ConsoleFilter, (r: BillingConsoleOrg) => boolean> = {
  overdue: isOverdue,
  // What the ORG is living with right now: past the banner days its dashboard
  // 402s on every call. Kept separate from `overdue` because the operator's
  // two questions are different ones ("who owes me" vs "who is locked out").
  locked: (r) => r.stage === "locked",
  candidates: isCandidate,
  flagged: isFlagged,
  // Negative outstanding. Advance payment IS the credit mechanism here, so a
  // credit is an ordinary state and not an error to chase.
  credit: (r) => r.outstanding_paise < 0,
  exempt: (r) => r.exempt,
  deactivated: (r) => r.deactivated,
}

export const FILTER_KEYS = Object.keys(FILTERS) as ConsoleFilter[]

export const FILTER_LABEL: Record<ConsoleFilter, string> = {
  overdue: "Overdue",
  locked: "Locked out",
  candidates: "Deactivation list",
  flagged: "Meter flagged",
  credit: "In credit",
  exempt: "Not billed",
  deactivated: "Deactivated",
}

export const FILTER_HINT: Record<ConsoleFilter, string> = {
  overdue: "An open invoice at least a day old, with a balance still owing.",
  locked: "Past the banner days. Every dashboard call answers payment required.",
  candidates: "60 days or more overdue. Deactivation is still your click.",
  flagged: "Today's ONU count was downgraded, held or moved source.",
  credit: "Paid ahead. The balance is on their side.",
  exempt: "Not metered and never reminded.",
  deactivated: "Switched off by hand. Dashboard locked, probes stood down.",
}

// Chips that take the alarm tone when they hold anything. The rest are
// navigation, not news, and a wall of red is unactionable.
export const FILTER_HOT: ReadonlySet<ConsoleFilter> =
  new Set<ConsoleFilter>(["overdue", "locked", "candidates"])

// Chips worth hiding at zero. `overdue` and `flagged` stay visible reading 0
// because "nothing is wrong" is the answer the operator came for; a chip that
// vanishes leaves them unsure whether it was ever computed.
export const FILTER_ALWAYS: ReadonlySet<ConsoleFilter> =
  new Set<ConsoleFilter>(["overdue", "flagged"])

export const isFilter = (raw: string | null): raw is ConsoleFilter =>
  raw != null && raw in FILTERS

export function filterCounts(
  rows: BillingConsoleOrg[],
): Record<ConsoleFilter, number> {
  const out = Object.fromEntries(FILTER_KEYS.map((f) => [f, 0])) as
    Record<ConsoleFilter, number>
  for (const r of rows) for (const f of FILTER_KEYS) if (FILTERS[f](r)) out[f]++
  return out
}

// ──────────────────────────────────────────────────────────────── the digest
// The superadmin gets ONE WhatsApp digest a day (central/dunning.py:_digest).
// This composes the same clauses from the same rows so the screen and the
// message cannot drift. Deliberately NOT fetched from the server: a mirror
// that reads a second endpoint is a second answer.
export interface DigestStep {
  row: BillingConsoleOrg
  from: string
  to: string
}

export interface Digest {
  /** Worst first: most owed, then most overdue. dunning's own sort. */
  overdue: BillingConsoleOrg[]
  owedPaise: number
  candidates: BillingConsoleOrg[]
  downgraded: DigestStep[]
  held: Array<{ row: BillingConsoleOrg; source: string }>
  moved: DigestStep[]
  /** Nothing to say. A daily all-clear ping trains the operator to ignore
   *  the channel, so on a quiet day the digest sends nothing at all. */
  quiet: boolean
}

export function digestOf(rows: BillingConsoleOrg[]): Digest {
  const looked = rows.filter(inLadder)
  const overdue = looked.filter(isOverdue).sort((a, b) =>
    (b.outstanding_paise - a.outstanding_paise) || (b.days_overdue - a.days_overdue))

  const downgraded: DigestStep[] = []
  const held: Array<{ row: BillingConsoleOrg; source: string }> = []
  const moved: DigestStep[] = []
  for (const row of looked) {
    const f = row.today?.flags
    if (!f) continue
    // A downgrade is reported INSTEAD of the source change that carries it
    // (every downgrade is also a source change, and naming both would double
    // the clause). The hold is independent and tallies on its own.
    if (f.downgraded) downgraded.push({ row, ...f.downgraded })
    else if (f.source_changed) moved.push({ row, ...f.source_changed })
    if (f.held) held.push({ row, source: f.held })
  }

  return {
    overdue,
    owedPaise: overdue.reduce((n, r) => n + r.outstanding_paise, 0),
    candidates: overdue.filter((r) => r.deactivation_candidate),
    downgraded,
    held,
    moved,
    quiet: overdue.length === 0 && downgraded.length === 0
      && held.length === 0 && moved.length === 0,
  }
}

/** How many orgs a digest clause names before it collapses. Mirrors
 *  dunning.py:_LISTED — the digest is a nudge to open this page, not a copy
 *  of it. */
export const LISTED = 3

// ───────────────────────────────────────────────────────────────── the sort
// Sorted by THE NUMBER THE COLUMN PRINTS (the customers-page rule): an
// operator sorting a column is asking about the figure in front of them, and
// a hidden key that disagrees with it reads as broken.
export type SortKey =
  | "name" | "outstanding" | "overdue" | "conns" | "devices" | "today"

/** The sortable value of one cell, or null when the cell prints no number.
 *  A null is NOT a zero: "the meter has not run for this org" and "this org
 *  has no ONUs online" are different findings, so nulls sort LAST in both
 *  directions rather than joining the zeros. */
export function sortValue(r: BillingConsoleOrg, key: SortKey): number | null {
  switch (key) {
    // Signed, so a credit sorts BELOW zero rather than beside the same
    // magnitude owed. "Owes 5,000" and "5,000 in credit" print the same digits
    // and mean opposite things; interleaving them answers neither question.
    case "outstanding": return r.outstanding_paise
    case "overdue": return r.days_overdue
    case "conns": return r.today?.conn_count ?? null
    case "devices": return r.today?.device_count ?? null
    case "today": return r.today?.paise ?? null
    default: return null
  }
}

export function bySort(key: SortKey, desc: boolean) {
  const name = (r: BillingConsoleOrg) => (r.name || r.org_id).toLowerCase()
  return (a: BillingConsoleOrg, b: BillingConsoleOrg): number => {
    if (key !== "name") {
      const x = sortValue(a, key), y = sortValue(b, key)
      if (x == null || y == null) {
        if (x != null || y != null) return x == null ? 1 : -1
      } else if (x !== y) {
        return desc ? y - x : x - y
      }
    }
    return name(a).localeCompare(name(b))
  }
}

// ────────────────────────────────────────────────────────────────── the money
// Integer paise on the wire, rupees only where a human types. These two are
// the boundary and nothing else in the console may divide or multiply by 100.

/** A typed rupee amount to integer paise, or null when it is not a number.
 *  Rounded once, on the rupee figure, so 12.345 becomes 1235 paise rather
 *  than riding a float into the ledger. `signed` allows the leading minus an
 *  adjustment needs; a payment refuses it before the server has to. */
export function paiseFromRupees(
  text: string, { signed = false }: { signed?: boolean } = {},
): number | null {
  const s = text.trim().replace(/[₹,\s]/g, "")
  if (!s) return null
  if (!/^-?\d*\.?\d*$/.test(s) || s === "-" || s === "." || s === "-.") return null
  const rupees = Number(s)
  if (!Number.isFinite(rupees)) return null
  if (!signed && rupees < 0) return null
  return Math.round(rupees * 100)
}

/** Integer paise back into a rupee figure a human can edit. No grouping and
 *  no symbol: it goes straight back into an <input>, where a comma would
 *  fail its own parser. */
export function rupeesFromPaise(paise: number): string {
  const abs = Math.abs(paise)
  const body = abs % 100 === 0
    ? String(Math.floor(abs / 100))
    : (abs / 100).toFixed(2)
  return paise < 0 ? `-${body}` : body
}
