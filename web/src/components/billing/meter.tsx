// TODAY'S METER. The four facts the day was billed on, in the order the
// formula uses them, so this row and the card below it read as one sentence.
//
// The ONU count is the one number here that can be an ESTIMATE, and it has to
// look like one: a held count is the last good read of a roster that stopped
// answering, and no count at all is a dead zone rather than a zero. Both go
// through <Reading> in the state connSourceMeta assigns, so an estimate can
// never sit on screen wearing the same ink as a measurement.
import type { ReactNode } from "react"
import { Info } from "lucide-react"
import { Reading } from "@/components/reading"
import { Chip } from "@/components/status-badge"
import { accrualFlagNote, connSourceMeta, dayLabel, inrExact } from "@/lib/billing"
import type { BillingInfo } from "@/lib/types"
import { Eyebrow, Panel } from "./shared"

function Cell({ label, note, children }: {
  label: string
  note?: string
  children: ReactNode
}) {
  return (
    <div className="min-w-0">
      <Eyebrow>{label}</Eyebrow>
      {/* Fixed height: the absent state is a dead zone and the present state
          is a figure with a chip beside it. They must occupy the same box or
          the row jumps the moment the meter runs. */}
      <div className="mt-1 flex h-6 items-center gap-2 text-sm">{children}</div>
      {/* The line is reserved whether or not there is a note, so a cell that
          gains one later doesn't push the row. Truncated, with the full text
          on hover, because these sentences are longer than a quarter row. */}
      <p className="mt-0.5 h-4 truncate text-2xs text-faint-foreground"
        title={note}>
        {note}
      </p>
    </div>
  )
}

export function TodayMeter({ b }: { b: BillingInfo }) {
  const t = b.today
  const src = connSourceMeta(t?.conn_source)
  const flag = accrualFlagNote(t)
  const last = b.accruals.length ? b.accruals[b.accruals.length - 1] : null

  // device_count on the row is what the day was BILLED on; b.device_count is
  // the fleet right now. They differ the moment somebody adds a device, and
  // the difference is a fact worth printing rather than a discrepancy to hide.
  const billedDevices = t?.device_count ?? null
  const driftedDevices = t != null && t.device_count !== b.device_count

  return (
    <Panel title="Today"
      note={t ? dayLabel(t.day) : "not counted yet"}>
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 px-4 py-3 @2xl:grid-cols-4">
        <Cell label="ONUs online"
          note={t ? src.detail : undefined}>
          {t
            ? (
              <>
                <Reading value={t.conn_count.toLocaleString("en-IN")}
                  state={src.reading} reason={src.detail} />
                <Chip tone="muted">{src.label}</Chip>
              </>
            )
            : <Reading value={null} state="absent"
              reason="today's meter has not run yet" />}
        </Cell>

        <Cell label="Devices"
          note={driftedDevices
            ? `${b.device_count.toLocaleString("en-IN")} monitored right now`
            : "monitored, passives excluded"}>
          <Reading
            value={(billedDevices ?? b.device_count).toLocaleString("en-IN")}
            state="current" />
        </Cell>

        <Cell label="Charged today"
          note={t ? `over ${b.days_in_month} days in ${b.month_label}` : undefined}>
          {t
            ? <Reading value={inrExact(t.paise)} state="current" />
            : <Reading value={null} state="absent"
              reason="no charge has been recorded for today" />}
        </Cell>

        <Cell label="Billed on"
          note={t
            ? (t.winning_side === "conn"
              ? "the larger of the two sides"
              : "ONUs came in under the floor")
            : undefined}>
          {t
            ? (
              <span className="text-sm text-foreground">
                {t.winning_side === "conn"
                  ? "Per ONU"
                  : "Device floor"}
              </span>
            )
            : <Reading value={null} state="absent"
              reason="no side has won yet today" />}
        </Cell>
      </div>

      {/* One line, and only when there is something to say about the day. */}
      {(flag || !t) && (
        <p className="flex items-start gap-1.5 border-t border-border-subtle px-4 py-2.5 text-2xs text-muted-foreground">
          <Info className="mt-px size-3 shrink-0" aria-hidden />
          <span>
            {flag ?? (
              <>
                Today's meter has not run yet.
                {last
                  ? <> The last day counted was {dayLabel(last.day)}
                    at {inrExact(last.paise)}.</>
                  : <> Nothing has been counted for this account yet.</>}
              </>
            )}
          </span>
        </p>
      )}
    </Panel>
  )
}
