// The superadmin billing console: every org's metered postpaid position on one
// screen, and the drawer that acts on one of them.
//
// Billing v2 in a line: max(ONUs × ONU rate, gear × device floor) ÷
// days in month accrues per operator-day in integer paise, a month closes into
// an invoice on the 1st, and outstanding is SUM(accruals) − SUM(payments),
// computed and never stored. Negative means credit. Plans, device caps and
// prepaid month-marking are gone (operator decision, 2026-08-17).
//
// Two rules run through the whole file:
//
//   * EVERY CHIP RECOUNTS THE ROWS IT FILTERS TO. Nothing here reads a server
//     total, and the summary block is composed from the same array the chips
//     filter, so the tile, the list and the WhatsApp digest cannot disagree
//     about a number. This is the /issues rule.
//
//   * AN ESTIMATED COUNT LOOKS ESTIMATED. An ONU count that is held from
//     a broken source, or declared by hand, or missing entirely, renders in the
//     Reading grammar and never as a plain solid figure. A bill rides on which
//     of those it is.

import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  ArrowDown, ArrowUp, ChevronsUpDown, CreditCard, Receipt,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { ApiError, billingApi } from "@/lib/api"
import { dayLabel, inrAuto, inrExact, stageMeta } from "@/lib/billing"
import type { BillingConsoleOrg } from "@/lib/types"
import { Reading } from "@/components/reading"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ConnCell, FlagCell, Outstanding, WinningSide } from "./billing-console/cells"
import { OrgDrawer } from "./billing-console/org-drawer"
import {
  FILTERS, FILTER_ALWAYS, FILTER_HINT, FILTER_HOT, FILTER_KEYS, FILTER_LABEL,
  LISTED, bySort, digestOf, filterCounts,
  type ConsoleFilter, type SortKey,
} from "./billing-console/rows"

// Organization · Status · Outstanding, then four more columns from lg up. The
// hidden cells are display:none, so the narrow grid genuinely has three tracks
// rather than four squeezed ones.
//
// The fixed tracks are measured against the LONGEST thing each can print
// ("Overdue 999d", "₹1,23,456.78", "ONU roster", "device floor") and no wider,
// because this panel sits on the narrow page measure (66rem) and every rem
// spent on a fixed column comes out of the org name and the meter note.
const COLS = "grid grid-cols-[minmax(0,1fr)_6.5rem_7rem] items-center gap-3 px-3 sm:px-4 lg:grid-cols-[minmax(0,1.3fr)_6.5rem_7rem_6.5rem_4.5rem_6rem_minmax(0,1fr)]"

/** Why a row has no meter reading today. Three different findings, and the one
 *  that matters ("the sweep has not run") must not hide behind the two that are
 *  deliberate. */
function meterReason(r: BillingConsoleOrg): string {
  if (r.exempt) return "Not metered. This org is exempt from billing."
  if (r.deactivated) return "Not metered. This org is deactivated."
  return "Today's meter has not run for this org yet."
}

/** "+N more", the digest's own collapse (dunning.py:_more). */
const more = (n: number) => (n > LISTED ? ` +${n - LISTED} more` : "")

// ───────────────────────────────────────────────────────── the daily digest

/** One clause of the digest. Clicking it opens the chip that holds exactly the
 *  rows the clause counted, which is only safe because both come from the same
 *  pass over the same array. */
function Clause({ filter, lead, detail, hot, onPick }: {
  filter: ConsoleFilter
  lead: string
  detail: string
  hot?: boolean
  onPick: (f: ConsoleFilter) => void
}) {
  return (
    <button type="button" onClick={() => onPick(filter)}
      title={`Show the ${FILTER_LABEL[filter].toLowerCase()} rows`}
      className="wisp-row flex w-full cursor-pointer flex-wrap items-baseline gap-x-2 gap-y-0.5 px-4 py-2 text-left transition-colors hover:bg-foreground/5">
      <span className={cn("text-xs font-medium",
        hot ? "text-destructive" : "text-foreground")}>
        {lead}
      </span>
      <span className="min-w-0 text-2xs text-muted-foreground">{detail}</span>
    </button>
  )
}

/** The superadmin's one WhatsApp digest a day, mirrored on screen and composed
 *  from the SAME rows the table filters (central/dunning.py:_digest builds the
 *  message from the same facts). A screen that recomputed this from a second
 *  endpoint would be a second answer.
 *
 *  A source clause names the ladder rung with the ENGINE's word for it
 *  ("radius", "onu", "declared"), not the SPA's prettier label, because this is
 *  the line the superadmin already read on WhatsApp that morning and the point
 *  of mirroring it is that they recognise it. The pretty label belongs where it
 *  says what a COUNT was measured from, which is the ONUs column. */
function DigestBlock({ rows, today, onPick }: {
  rows: BillingConsoleOrg[]
  today: string
  onPick: (f: ConsoleFilter) => void
}) {
  const d = useMemo(() => digestOf(rows), [rows])
  const nameOf = (r: BillingConsoleOrg) => r.name || r.org_id

  return (
    <div className="wisp-panel">
      <div className="wisp-panel-head">
        <span className="wisp-eyebrow">Daily digest</span>
        <span className="text-2xs text-faint-foreground">{dayLabel(today)}</span>
      </div>

      {d.quiet ? (
        // Nothing to say gets a calm sentence, not three zeroes. A daily
        // all-clear trains the operator to ignore the channel, which is why
        // the sweep sends no message at all on a day like this.
        <div className="px-4 py-3">
          <p className="text-xs font-medium">Nothing needs attention today.</p>
          <p className="mt-0.5 text-2xs text-muted-foreground">
            No organization is overdue, and every meter read from its usual source.
          </p>
        </div>
      ) : (
        <>
          {d.overdue.length > 0 && (
            <Clause filter="overdue" hot onPick={onPick}
              lead={`${d.overdue.length} overdue · ${inrAuto(d.owedPaise)} owed`}
              detail={d.overdue.slice(0, LISTED).map((r) =>
                `${nameOf(r)} ${inrAuto(r.outstanding_paise)} (${r.days_overdue}d)`)
                .join(" · ") + more(d.overdue.length)} />
          )}
          {d.candidates.length > 0 && (
            <Clause filter="candidates" hot onPick={onPick}
              lead={`${d.candidates.length} on the deactivation list`}
              detail={d.candidates.slice(0, LISTED).map((r) =>
                `${nameOf(r)} (${r.days_overdue}d)`).join(" · ")
                + more(d.candidates.length)} />
          )}
          {d.downgraded.length > 0 && (
            <Clause filter="flagged" onPick={onPick}
              lead={`${d.downgraded.length} downgraded`}
              detail={d.downgraded.slice(0, LISTED).map((s) =>
                `${nameOf(s.row)} ${s.from} to ${s.to}`)
                .join(" · ") + more(d.downgraded.length)} />
          )}
          {d.held.length > 0 && (
            <Clause filter="flagged" onPick={onPick}
              lead={`${d.held.length} holding a stale source`}
              detail={d.held.slice(0, LISTED).map((s) =>
                `${nameOf(s.row)} ${s.source}`).join(" · ")
                + more(d.held.length)} />
          )}
          {d.moved.length > 0 && (
            <Clause filter="flagged" onPick={onPick}
              lead={`${d.moved.length} changed source`}
              detail={d.moved.slice(0, LISTED).map((s) =>
                `${nameOf(s.row)} ${s.from} to ${s.to}`)
                .join(" · ") + more(d.moved.length)} />
          )}
        </>
      )}

      <p className="border-t px-4 py-2 text-2xs text-faint-foreground">
        The same facts go to the ops WhatsApp number once a day, and only when
        there is something to say. The message also names any overdue org with no
        owner number on file, which is the one thing this screen cannot show you.
      </p>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────── the chips

function Chips({ all, picked, setPicked }: {
  all: BillingConsoleOrg[]
  picked: ConsoleFilter | null
  setPicked: (f: ConsoleFilter | null) => void
}) {
  // Recounted from `all` on every render of the list it filters. Never a
  // server total: the chip and the rows it opens are one pass.
  const counts = useMemo(() => filterCounts(all), [all])
  const box = (on: boolean) => cn(
    "inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors",
    on ? "border-border-strong bg-popover text-foreground"
      : "border-border bg-card text-muted-foreground hover:text-foreground")

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <button type="button" aria-pressed={picked === null} className={box(picked === null)}
        onClick={() => setPicked(null)}>
        All <span className="font-mono text-2xs text-faint-foreground">{all.length}</span>
      </button>
      {FILTER_KEYS.map((f) => {
        const n = counts[f]
        if (n === 0 && !FILTER_ALWAYS.has(f)) return null
        const hot = FILTER_HOT.has(f) && n > 0
        return (
          <button key={f} type="button" aria-pressed={picked === f}
            title={FILTER_HINT[f]}
            onClick={() => setPicked(picked === f ? null : f)}
            className={cn(box(picked === f),
              hot && picked !== f && "text-destructive hover:text-destructive")}>
            {FILTER_LABEL[f]}
            <span className={cn("font-mono text-2xs",
              hot ? "font-semibold text-destructive" : "text-faint-foreground")}>
              {n}
            </span>
          </button>
        )
      })}
    </div>
  )
}

// ────────────────────────────────────────────────────────────────── the table

interface Sort { key: SortKey; desc: boolean }

// Money, day counts and quantities all read "biggest first" on the first
// click; a name reads A to Z. Nobody sorts a fleet to find the smallest bill.
const DESC_FIRST: ReadonlySet<SortKey> =
  new Set<SortKey>(["outstanding", "overdue", "conns", "devices", "today"])

function SortHead({ label, k, sort, setSort, className, title }: {
  label: string
  k: SortKey
  sort: Sort
  setSort: (s: Sort) => void
  className?: string
  title: string
}) {
  const on = sort.key === k
  return (
    <button type="button" title={title}
      onClick={() => setSort(on
        ? { key: k, desc: !sort.desc }
        : { key: k, desc: DESC_FIRST.has(k) })}
      className={cn("inline-flex items-center gap-1 rounded px-1 transition-colors hover:text-foreground",
        on && "text-foreground", className)}>
      {label}
      {on
        ? (sort.desc ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />)
        : <ChevronsUpDown className="size-3 opacity-50" />}
    </button>
  )
}

function OrgRow({ r, onOpen }: { r: BillingConsoleOrg; onOpen: () => void }) {
  const stage = stageMeta(r.stage, r.days_overdue)
  const reason = meterReason(r)
  const t = r.today

  return (
    <button type="button" onClick={onOpen}
      className={cn(COLS, "wisp-row h-11 w-full cursor-pointer text-left transition-colors hover:bg-foreground/5")}>
      <span className="min-w-0">
        <span className="block truncate text-xs font-medium">{r.name || r.org_id}</span>
        <span className="block truncate text-2xs text-faint-foreground">
          <span className="font-mono">{r.org_id}</span>
          {r.open_invoice && ` · invoice ${r.open_invoice.month}`}
        </span>
      </span>

      {/* A healthy account gets no fill. The overwhelming majority of a fleet
          is up to date and none of it is news, so the chip's colour is spent
          only where the ladder has something to say (the map's "ok gets no
          band" rule). The WORD still prints: a blank cell would read as
          "not computed". */}
      <span className="min-w-0">
        {r.stage === "clear" ? (
          <span className="text-2xs text-faint-foreground">{stage.label}</span>
        ) : (
          <span className={cn("inline-block rounded-4xl px-2 py-0.5 text-2xs font-medium",
            stage.className)}>
            {stage.label}
          </span>
        )}
      </span>

      <Outstanding paise={r.outstanding_paise} />

      <span className="hidden lg:flex lg:justify-end">
        <ConnCell count={t?.conn_count} source={t?.conn_source} absentReason={reason} />
      </span>

      <span className="hidden text-right lg:block">
        {t?.device_count != null ? (
          <span className="font-mono text-xs tabular-nums text-muted-foreground">
            {t.device_count}
          </span>
        ) : (
          <Reading value={null} state="absent" reason={reason} />
        )}
      </span>

      <span className="hidden flex-col items-end leading-tight lg:flex">
        {t?.paise != null ? (
          <>
            <span className="font-mono text-xs tabular-nums">{inrExact(t.paise)}</span>
            <WinningSide side={t.winning_side} />
          </>
        ) : (
          <Reading value={null} state="absent" reason={reason} />
        )}
      </span>

      <span className="hidden min-w-0 lg:block">
        <FlagCell flags={t?.flags} />
      </span>
    </button>
  )
}

// ──────────────────────────────────────────────────────────────── the surface

export function BillingConsolePanel() {
  const [picked, setPicked] = useState<ConsoleFilter | null>(null)
  const [sort, setSort] = useState<Sort>({ key: "outstanding", desc: true })
  const [open, setOpen] = useState<string | null>(null)

  const q = useQuery({
    queryKey: ["billing-console"],
    queryFn: () => billingApi.console(),
  })

  const all = useMemo(() => q.data?.orgs ?? [], [q.data])
  const rows = useMemo(
    () => all.filter((r) => !picked || FILTERS[picked](r)).sort(bySort(sort.key, sort.desc)),
    [all, picked, sort])

  const rates = q.data?.rates

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Receipt className="size-4 text-muted-foreground" /> Billing console
        </h2>
        {q.data && (
          <span className="text-2xs text-faint-foreground">
            Meter day {dayLabel(q.data.today)}
            {rates && ` · ${inrAuto(rates.conn_paise)} per ONU or `
              + `${inrAuto(rates.floor_paise)} per monitored device, whichever is larger`}
          </span>
        )}
      </div>
      <p className="max-w-prose text-xs text-muted-foreground">
        Metered postpaid. Every organization accrues daily and is invoiced on the
        1st. Outstanding is everything accrued less everything paid, so a negative
        balance is credit. Deactivation is never automatic: however overdue an
        account gets, standing it down is your click.
      </p>

      {q.isLoading && (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-28 w-full rounded-xl" />
          <Skeleton className="h-7 w-96 rounded-md" />
          <div className="wisp-panel">
            <div className={cn(COLS, "wisp-thead h-9")}><span>Organization</span></div>
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className={cn(COLS, "wisp-row h-11")}>
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 w-16" />
                <Skeleton className="h-4 w-14 justify-self-end" />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* A 404 here is a DEPLOY fact, not a fault: the SPA ships instantly off
          disk while central keeps the code it started with, so a box that has
          not been restarted since metered billing landed has no
          /api/admin/billing to answer with. Saying "HTTP 404" would send the
          operator hunting for a bug that is a service restart. */}
      {q.isError && (
        <p className="wisp-panel p-8 text-center text-xs text-destructive">
          {q.error instanceof ApiError && q.error.status === 404
            ? "This server is still running the code from before metered billing. Restart central and the console appears."
            : q.error instanceof ApiError ? q.error.message
              : "Could not load the billing console"}
        </p>
      )}

      {q.isSuccess && all.length === 0 && (
        <div className="wisp-panel px-4 py-10 text-center">
          <p className="text-xs font-medium">No organizations to bill yet.</p>
          <p className="mx-auto mt-1 max-w-md text-2xs text-muted-foreground">
            The meter starts the day an organization exists. Create one under
            Organizations and its first accrual row lands on the next sweep.
          </p>
        </div>
      )}

      {q.isSuccess && all.length > 0 && (
        <>
          <DigestBlock rows={all} today={q.data.today} onPick={setPicked} />
          <Chips all={all} picked={picked} setPicked={setPicked} />

          <div className="wisp-panel">
            <div className={cn(COLS, "wisp-thead h-9")}>
              <SortHead label="Organization" k="name" sort={sort} setSort={setSort}
                className="-ml-1 justify-self-start" title="Sort by name" />
              <SortHead label="Status" k="overdue" sort={sort} setSort={setSort}
                className="-ml-1 justify-self-start"
                title="Sort by days overdue. The chip prints the count." />
              <SortHead label="Outstanding" k="outstanding" sort={sort} setSort={setSort}
                className="-mr-1 justify-self-end"
                title="Sort by the balance. Credit sorts below zero, where it belongs." />
              <SortHead label="ONUs" k="conns" sort={sort} setSort={setSort}
                className="-mr-1 hidden justify-self-end lg:inline-flex"
                title="Sort by today's ONU count" />
              <SortHead label="Gear" k="devices" sort={sort} setSort={setSort}
                className="-mr-1 hidden justify-self-end lg:inline-flex"
                title="Sort by today's monitored device count" />
              <SortHead label="Today" k="today" sort={sort} setSort={setSort}
                className="-mr-1 hidden justify-self-end lg:inline-flex"
                title="Sort by what today accrued" />
              <span className="hidden lg:block">Meter</span>
            </div>

            {rows.length === 0 ? (
              <p className="p-8 text-center text-xs text-faint-foreground">
                Nothing matches the current filter.
              </p>
            ) : rows.map((r) => (
              <OrgRow key={r.org_id} r={r} onOpen={() => setOpen(r.org_id)} />
            ))}
          </div>
        </>
      )}

      {open && <OrgDrawer orgId={open} onClose={() => setOpen(null)} />}
    </section>
  )
}

/** The org list's per-row entry point into the same drawer. Kept so a
 *  superadmin can reach one org's ledger from Organizations without going
 *  through the console, and so routes/organizations-page.tsx keeps a live
 *  import. Deliberately a thin trigger: there is ONE billing drawer. */
export function BillingAdminDialog({ org, name }: { org: string; name: string | null }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}
        title={`Billing ledger for ${name || org}`}>
        <CreditCard className="size-3.5" /> Billing
      </Button>
      {open && <OrgDrawer orgId={org} onClose={() => setOpen(false)} />}
    </>
  )
}
