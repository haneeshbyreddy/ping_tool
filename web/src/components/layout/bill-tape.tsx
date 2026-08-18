// THE RUNNING BILL, in the top bar. The quietest thing in the header by
// design: the operator is here to watch a network, and the bill is background
// until it isn't.
//
// IT IS A METER, NOT A BADGE. The ledger accrues one row a day, so the honest
// shape for it is a figure whose own bottom edge fills as the month runs, not
// a pill with a number in it. The TRACK IS DAYS ELAPSED, never the amount:
// filling by amount needs a maximum, and the only maximum available is the
// projection below, which is an estimate. Days elapsed is a fact, and it is
// what makes this read as a meter running rather than a debt growing.
//
// NO HUE AT REST. Money is not an alarm axis (the rule hero.tsx states and
// this obeys), and billing is not one of the five identity planes either, so
// the resting state gets plain neutral ink and the only channels left are
// type, fill and the once-a-day tick. Overdue IS an alarm and takes
// destructive outright, in this same slot: one component with two states, so
// an unpaid invoice can never grow a second nag somewhere else in the chrome.
//
// The card is where the personality goes, because it is opt-in. Its sparkline
// is hand-drawn divs rather than the chart kit on purpose: this renders in the
// shell of every page, and pulling d3 scales into the shell chunk to draw
// thirty-one 2px columns would undo the route code-splitting.
import { useMemo, type ReactNode } from "react"
import { Link } from "react-router-dom"
import { Reading } from "@/components/reading"
import { Button } from "@/components/ui/button"
import {
  Popover, PopoverClose, PopoverContent, PopoverTrigger,
} from "@/components/ui/popover"
import {
  connSourceMeta, dayLabel, inr, inrExact, inrSigned, monthLabel,
} from "@/lib/billing"
import type { Accrual, BillingInfo } from "@/lib/types"
import { cn } from "@/lib/utils"

/** The month the ledger is filling, as the operator's own day.
 *
 *  `today.day` is stamped by the server in WISP_DISPLAY_TZ, which is the same
 *  clock the accrual rows are keyed on. The browser's midnight is a different
 *  moment and would tick the track early or late; the local fallback is only
 *  for an org whose first day has not been metered yet. */
function operatorDayNumber(b: BillingInfo): number {
  const day = b.today?.day ?? new Date().toLocaleDateString("en-CA")
  const n = Number(day.slice(8, 10))
  return Number.isFinite(n) ? Math.min(Math.max(n, 0), b.days_in_month) : 0
}

/** What is owed EXCLUDING the month still accruing: the invoice part. Same
 *  arithmetic BillingBanner and the lock screen bill on, so the three surfaces
 *  cannot disagree about whether there is anything to settle. */
function invoicedDuePaise(b: BillingInfo): number {
  return Math.max(0, b.outstanding_paise - b.month_to_date_paise)
}

/** Which question the figure is answering. The card's rows are chosen from
 *  this too, so a row can never sit on a different basis from the headline
 *  above it (a ₹599 outstanding with "on track for ₹387" underneath it is two
 *  bases stacked, and the reader cannot reconcile them). */
type Basis = "month" | "outstanding" | "credit"

interface Tape {
  /** Integer paise. SIGNED: credit is negative and renders as one. */
  amount: number
  basis: Basis
  /** What that figure IS, for the card's eyebrow and the screen reader. */
  what: string
  /** Overdue: the one state that spends a status colour here. */
  alarm: boolean
}

function tapeState(b: BillingInfo): Tape {
  const owed = Math.max(0, b.outstanding_paise)
  const due = invoicedDuePaise(b)

  // Deactivated reaches this only for a superadmin scoped into the org: an
  // owner gets the lock screen instead and never renders a header at all.
  const dunned = b.stage === "banner" || b.stage === "locked"
    || b.stage === "deactivated"

  if (due > 0 && dunned) {
    return { amount: owed, basis: "outstanding", what: "Outstanding", alarm: true }
  }

  // CREDIT IS SHOWN SIGNED, and it outranks the month: an org that has paid
  // ahead wants to see it has paid ahead, not what today added. No green — a
  // healthy balance is still not an alarm axis, and the minus does the work a
  // colour would have done.
  if (b.outstanding_paise < 0) {
    return {
      amount: b.outstanding_paise, basis: "credit",
      what: "In credit", alarm: false,
    }
  }

  // An exempt org accrues nothing, so its month-to-date is structurally zero.
  // A balance predating the exemption is still real and still shown; it is
  // just not dunned, so it never takes the alarm tone.
  if (b.exempt) {
    return { amount: owed, basis: "outstanding", what: "Outstanding", alarm: false }
  }

  return {
    amount: b.month_to_date_paise,
    basis: "month",
    what: `${b.month_label}, to date`,
    alarm: false,
  }
}

export function BillTape({ billing }: { billing: BillingInfo }) {
  const t = tapeState(billing)
  const dayNo = operatorDayNumber(billing)
  const fill = billing.days_in_month > 0 ? dayNo / billing.days_in_month : 0

  // Nothing accrues and nothing is owed: the meter has no reading to give and
  // a permanent ₹0 in the chrome is noise, not restraint. This is not the
  // "appears when something is wrong" pattern the placement rules out — an
  // exempt account never flips into having something to say, so its absence is
  // structural rather than an event.
  if (billing.exempt && t.amount <= 0) return null

  const label = `${t.what}: ${inrSigned(t.amount)}. `
    + `Day ${dayNo} of ${billing.days_in_month}.`

  return (
    <Popover>
      <PopoverTrigger asChild>
        {/* The panel surface (`--card` + border, the .wisp-panel recipe), so
            the meter reads as an object in the chrome rather than a recess.
            Overdue takes destructive-soft, the same fill stageMeta gives the
            chip on /billing.
            `h-8` matches the search field to its right — the two objects in
            the chrome zone sit on one baseline. */}
        <button
          type="button"
          aria-label={label}
          className={cn(
            "group relative hidden h-8 min-w-18 shrink-0 items-center rounded-md border px-2.5 text-left transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50 md:flex",
            t.alarm
              ? "border-destructive/30 bg-destructive-soft hover:bg-destructive/20"
              : "bg-card hover:bg-accent aria-expanded:bg-accent",
          )}>
          <span className={cn(
            "font-mono text-sm tabular-nums transition-colors",
            t.alarm
              ? "text-destructive"
              : "text-muted-foreground group-hover:text-foreground group-aria-expanded:text-foreground",
          )}>
            {inrSigned(t.amount)}
          </span>
          {/* The meter, and THE BOTTOM BORDER IS ITS TRACK. A bar inside the
              button was a second object to fit, which is what held the figure
              down to 13px; riding the border spends no interior height, so the
              number gets the readable step (--text-sm) the wall-mounted screen
              wants. It also stops the meter being a widget in a box and makes
              it an edge of the box that fills.
              The wrapper is `-inset-px rounded-md`, i.e. the button's whole
              BORDER BOX with the button's own radius, and it clips. That is
              what lets the fill COVER the border instead of stacking a second
              rule above it (the button itself can't take overflow-hidden —
              that clips to the padding box, inside the border). Clipping on
              the FULL box and not on a 2px-tall strip is load-bearing: CSS
              scales a radius down to fit its box, so a strip that height
              rounds to ~2px and its ends jut out past the button's corner
              curve, which reads as a bar hanging under the button rather than
              as its edge. Filling the whole width also means the unfilled
              remainder is simply the border in the border's own tone: no track
              element, and nothing to keep in sync with the figure's width as
              the digits change.
              OPERATOR'S CALL (2026-08-17): the fill runs the status ramp,
              green while the account is square and red once it is overdue.
              This is a deliberate exception to "money is not an alarm axis" —
              the rule still governs the FIGURE, which stays neutral ink until
              the account is genuinely late. */}
          <span
            aria-hidden
            className="pointer-events-none absolute -inset-px overflow-hidden rounded-md">
            <span
              className={cn(
                "absolute bottom-0 left-0 h-0.5 transition-[width,background-color] duration-500",
                t.alarm ? "bg-destructive" : "bg-success",
              )}
              style={{
                // A RENDERING FLOOR, not a reading: the corner curve eats the
                // first few px, so day 1 of 31 clips away to nothing and an
                // early month is indistinguishable from a meter that isn't
                // running. 8px is the least that clears the radius. Same
                // instinct as MonthSpark's height floor below — a day that was
                // charged has to be visible as one. It only ever moves the
                // FIRST days, the exact figure is one click away in the card,
                // and zero stays zero.
                width: fill > 0 ? `max(${Math.round(fill * 100)}%, 0.5rem)` : 0,
              }} />
          </span>
        </button>
      </PopoverTrigger>

      <PopoverContent align="end" sideOffset={8} className="w-78">
        <BillCard billing={billing} tape={t} dayNo={dayNo} />
      </PopoverContent>
    </Popover>
  )
}

// ------------------------------------------------------------------ the card

function BillCard({ billing: b, tape, dayNo }: {
  billing: BillingInfo
  tape: Tape
  dayNo: number
}) {
  const today = b.today
  const src = connSourceMeta(today?.conn_source)

  // What the month ends at IF today's charge repeats. An estimate, and it is
  // rendered as one: no <Reading> state claims a projection, so it takes the
  // approximately-sign and faint ink rather than borrowing the grammar of a
  // measurement. No accrual row today means no rate to project from.
  //
  // ONLY on the month basis. Projecting a month under an OUTSTANDING headline
  // puts a smaller number below a bigger one on a different basis, which is
  // the one thing a money card may not do; that state gets the invoice
  // breakdown instead, which is what the figure above is actually made of.
  const projection = today && tape.basis === "month"
    ? b.month_to_date_paise + today.paise * (b.days_in_month - dayNo)
    : null

  return (
    <div>
      <span className="wisp-eyebrow block">{tape.what}</span>
      <div className="mt-1 flex items-baseline gap-2">
        <span className={cn(
          "font-mono text-2xl tabular-nums",
          tape.alarm ? "text-destructive" : "text-foreground",
        )}>
          {inrSigned(tape.amount)}
        </span>
        <span className="text-2xs text-faint-foreground">
          day {dayNo} of {b.days_in_month}
        </span>
      </div>

      <MonthSpark accruals={b.accruals} days={b.days_in_month} today={dayNo} />
      <div className="flex justify-between text-2xs text-faint-foreground">
        <span>1 {b.month_label.slice(0, 3)}</span>
        <span>{b.days_in_month} {b.month_label.slice(0, 3)}</span>
      </div>

      <dl className="mt-3 grid gap-1.5 border-t pt-2.5 text-xs">
        {/* What the headline is MADE OF, when it is not just this month.
            Same two parts the /billing hero names, in the same order. */}
        {tape.basis === "outstanding" && b.open_invoice && (
          <Row label={`${monthLabel(b.open_invoice.month)} invoice`}>
            <span className="font-mono tabular-nums">
              {inr(b.open_invoice.paise)}
            </span>
          </Row>
        )}
        {tape.basis === "outstanding" && b.month_to_date_paise > 0 && (
          <Row label="Accrued since">
            <span className="font-mono tabular-nums">
              {inr(b.month_to_date_paise)}
            </span>
          </Row>
        )}
        {/* Not mono: a date is not a figure that has to align with the column
            of rupees above it, and tabular spacing pulls "14 Sep" apart. */}
        {tape.basis === "credit" && b.credit_lasts_until && (
          <Row label="Covers until">{dayLabel(b.credit_lasts_until)}</Row>
        )}
        <Row label="Today">
          {today
            ? <Reading value={inrExact(today.paise)} state="current" />
            : <Reading value={null} state="absent"
              reason="today's meter has not run yet" />}
        </Row>
        <Row label="ONUs online">
          {today
            ? <Reading value={today.conn_count.toLocaleString("en-IN")}
              state={src.reading} reason={src.detail} />
            : <Reading value={null} state="absent" reason={src.detail} />}
        </Row>
        {projection != null && (
          <Row label="On track for">
            <span className="font-mono tabular-nums text-faint-foreground"
              title="If today's charge repeats for the rest of the month.">
              ≈ {inr(projection)}
            </span>
          </Row>
        )}
      </dl>

      <UnitLine billing={b} today={today} />

      {/* Closes on the way out: Radix keeps the content open for a click
          INSIDE it, which would leave the card hanging over the page it just
          routed to. `outline` is the quietest variant that still carries a
          fill, so the one action in here reads as a control without competing
          with the figure above it. */}
      <PopoverClose asChild>
        <Button asChild variant="outline" size="sm" className="mt-3 w-full">
          <Link to="/billing">Open billing</Link>
        </Button>
      </PopoverClose>
    </div>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-foreground">{children}</dd>
    </div>
  )
}

/** What one subscriber costs, or why the subscriber count is not what set the
 *  bill. The charge is max(ONUs × rate, devices × floor), so quoting the per
 *  ONU rate on a month the FLOOR won would name a price nobody was charged.
 *  Both sentences are facts off the row the day was billed on. */
function UnitLine({ billing: b, today }: {
  billing: BillingInfo
  today: Accrual | null
}) {
  if (!today) return null
  const text = today.winning_side === "conn"
    ? `${inrExact(today.conn_rate_paise)} per subscriber, per month.`
    : `The device floor sets this month: `
      + `${today.device_count.toLocaleString("en-IN")} devices `
      + `× ${inr(b.rates.floor_paise)}.`
  return (
    <p className="mt-2.5 border-t pt-2.5 text-2xs text-faint-foreground">
      {text}
    </p>
  )
}

/** One hairline per day of the month.
 *
 *  THREE STATES, and they must not collapse: a day with a row is a column, a
 *  day the meter never filed is a faint tick at the baseline, and a day that
 *  has not happened yet is the empty track. "Charged nothing", "not measured"
 *  and "not yet" take different actions, which is the same argument
 *  MissingDays makes on the full month chart one size up.
 *
 *  Drawn in the FLEET plane, matching that chart: an owner reading their own
 *  numbers should not cross hues between two surfaces answering the same
 *  question. */
function MonthSpark({ accruals, days, today }: {
  accruals: Accrual[]
  days: number
  today: number
}) {
  const rows = useMemo(() => {
    const byDay = new Map<number, Accrual>()
    for (const a of accruals) byDay.set(Number(a.day.slice(8, 10)), a)
    const peak = Math.max(1, ...accruals.map((a) => a.paise))
    return Array.from({ length: days }, (_, i) => {
      const a = byDay.get(i + 1)
      return {
        day: i + 1,
        // Floored well off zero: a quiet day is still a day that was charged,
        // and a one-pixel column beside a missing-day tick would be a
        // distinction nobody can see.
        height: a ? 0.35 + 0.65 * (a.paise / peak) : 0,
        filed: !!a,
        future: i + 1 > today,
      }
    })
  }, [accruals, days, today])

  return (
    <div className="my-3 flex h-9 items-end gap-px" aria-hidden>
      {rows.map((r) => (
        <span key={r.day} className="flex-1 rounded-[1px]"
          style={
            r.filed
              ? {
                height: `${Math.round(r.height * 100)}%`,
                background: r.day === today
                  ? "var(--chart-5)"
                  : "color-mix(in oklab, var(--chart-5) 45%, transparent)",
              }
              : {
                height: "2px",
                background: r.future
                  ? "var(--border)"
                  : "var(--faint-foreground)",
              }
          } />
      ))}
    </div>
  )
}
