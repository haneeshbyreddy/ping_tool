// HOW THE BILL IS COMPUTED, with this org's own numbers in it.
//
// Transparency is the trust feature: a metered bill an operator cannot
// reconstruct is a bill they dispute. So the formula is printed with the same
// figures the meter used, and the RESULT is the STORED paise, never a
// recomputation — a client-side re-derivation that disagreed with the invoice
// by one paise would undo the whole point of the exercise.
//
// The two sides are separated by the SURFACE LADDER, not by a hue: the side
// that won rises to --popover with the strong border, the side that lost stays
// flat and muted. A status colour on "this is the number you pay" would spend
// the alarm axis on arithmetic.
import type { ReactNode } from "react"
import { Reading } from "@/components/reading"
import { inr, inrExact } from "@/lib/billing"
import type { BillingInfo } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Panel } from "./shared"

function Side({ label, expr, total, won, note }: {
  label: string
  expr: ReactNode
  total: ReactNode
  won: boolean | null
  note?: string
}) {
  return (
    <div className={cn(
      "min-w-0 rounded-lg border px-3 py-2.5",
      won === true
        ? "border-border-strong bg-popover"
        : "border-border bg-transparent")}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="wisp-eyebrow">{label}</span>
        {won === true && (
          <span className="text-2xs font-medium text-muted-foreground">applies</span>
        )}
      </div>
      <p className={cn("mt-1 font-mono text-sm",
        won === false ? "text-faint-foreground" : "text-foreground")}>
        {expr}
      </p>
      <p className={cn("mt-0.5 font-mono text-xs",
        won === false ? "text-faint-foreground" : "text-muted-foreground")}>
        = {total} <span className="font-sans">per month</span>
      </p>
      {note && <p className="mt-1 text-2xs text-faint-foreground">{note}</p>}
    </div>
  )
}

export function FormulaCard({ b }: { b: BillingInfo }) {
  const t = b.today
  // Rates from the row when there is one (that is what the day was billed at),
  // otherwise the org's current rates. They differ only across a rate change,
  // and then the row is the truthful one.
  const rate = t?.conn_rate_paise ?? b.rates.conn_paise
  const floor = t?.floor_paise ?? b.rates.floor_paise
  const devices = t?.device_count ?? b.device_count
  const conns = t?.conn_count ?? null

  const connSide = conns == null ? null : conns * rate
  const floorSide = devices * floor
  // A tie goes to 'conn' server-side (metering.daily_paise: billed per ONU
  // is the headline story, the floor is the backstop). Read the stored
  // verdict rather than re-deciding it here.
  const won = t?.winning_side ?? null

  return (
    <Panel title="How this is computed"
      note="the larger side, divided by the days in the month">
      <div className="flex flex-1 flex-col gap-3 px-4 py-3">
        <div className="grid gap-2 @xl:grid-cols-2">
          <Side label="Per ONU"
            won={won == null ? null : won === "conn"}
            expr={conns == null
              ? (
                <span className="inline-flex items-baseline gap-1.5">
                  <Reading value={null} state="absent"
                    reason="no ONU count for today yet" />
                  <span>× {inr(rate)}</span>
                </span>
              )
              : <>{conns.toLocaleString("en-IN")} × {inr(rate)}</>}
            total={connSide == null ? "—" : inr(connSide)}
            note={b.rates.conn_override
              ? "a rate set for this account"
              : undefined} />
          <Side label="Device floor"
            won={won == null ? null : won === "floor"}
            expr={<>{devices.toLocaleString("en-IN")} × {inr(floor)}</>}
            total={inr(floorSide)}
            note={b.rates.floor_override
              ? "a floor set for this account"
              : undefined} />
        </div>

        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-lg bg-muted px-3 py-2.5">
          <span className="font-mono text-xs text-muted-foreground">
            {won == null
              ? <>the larger side ÷ {b.days_in_month} days</>
              : <>{inr(won === "conn" ? connSide ?? 0 : floorSide)}
                {" "}÷ {b.days_in_month} days</>}
          </span>
          <span className="text-xs text-faint-foreground">=</span>
          {t
            ? (
              <span className="font-mono text-base font-semibold tabular-nums">
                {inrExact(t.paise)}
              </span>
            )
            : <Reading value={null} state="absent"
              reason="today's meter has not run yet" />}
          <span className="text-xs text-muted-foreground">
            {t ? "charged today" : "today's meter has not run yet"}
          </span>
        </div>

        <p className="text-2xs text-muted-foreground">
          {won === "floor"
            ? <>Today your ONUs come in under the floor, so the floor
              is what you pay.</>
            : won === "conn"
              ? <>Today your ONUs are above the floor, so you are
                billed per ONU.</>
              : <>Whichever side is larger is the one you pay.</>}
          {" "}Each day is rounded once and stored on its own row.
        </p>
      </div>
    </Panel>
  )
}
