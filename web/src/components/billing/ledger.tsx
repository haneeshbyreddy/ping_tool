// THE LEDGER: what was billed, and what was paid against it. Two panels
// rather than one merged feed, because they answer different questions and an
// interleaved list makes neither easy to total by eye.
//
// TONE DISCIPLINE: only an OPEN invoice takes a status colour, because only an
// open invoice is a call to action (past the banner window it locks the
// dashboard). "Paid" and "Void" stay neutral. A column of green Paid chips is
// decoration competing with the alarm axis, and this product keeps that axis
// for things that are actually wrong.
import { useMemo } from "react"
import { Download } from "lucide-react"
import { billingApi } from "@/lib/api"
import { inr, inrAuto, monthLabel } from "@/lib/billing"
import { fmtDateTime, toUtcDate } from "@/lib/format"
import type { BillingInfo, Invoice, Payment, PaymentKind } from "@/lib/types"
import { Chip } from "@/components/status-badge"
import type { Tone } from "@/components/status-badge"
import { cn } from "@/lib/utils"
import { Empty, Panel } from "./shared"

const INVOICE_COLS =
  "grid grid-cols-[minmax(0,1fr)_5.5rem_4.75rem_1.75rem] items-center gap-3 px-4"
const PAYMENT_COLS =
  "grid grid-cols-[minmax(0,1fr)_6rem] items-center gap-3 px-4"

function statusChip(inv: Invoice, b: BillingInfo): { tone: Tone; label: string } {
  if (inv.status === "paid") return { tone: "muted", label: "Paid" }
  if (inv.status === "void") return { tone: "muted", label: "Void" }
  // The oldest open invoice is the one the ladder is anchored to, so it is the
  // only row that can carry the overdue count without inventing one.
  const anchor = b.open_invoice?.month === inv.month
  if (anchor && b.stage === "locked") {
    return { tone: "destructive", label: `Overdue ${b.days_overdue}d` }
  }
  return { tone: "warning", label: "Open" }
}

export function InvoicesPanel({ b, org }: { b: BillingInfo; org: string }) {
  const rows = useMemo(
    () => [...b.invoices].sort((x, y) => y.month.localeCompare(x.month)),
    [b.invoices])

  return (
    <Panel title="Invoices"
      note={rows.length ? `${rows.length} issued` : undefined}>
      <div className={cn(INVOICE_COLS, "wisp-thead h-9")}>
        <span>Month</span>
        <span className="text-right">Amount</span>
        <span>Status</span>
        <span />
      </div>
      {rows.length === 0 && (
        <Empty>
          No invoice yet. The first one is raised when {b.month_label} closes,
          and it is the sum of that month's daily rows.
        </Empty>
      )}
      {rows.map((inv) => {
        const chip = statusChip(inv, b)
        return (
          <div key={inv.month} className={cn(INVOICE_COLS, "wisp-row h-11")}>
            <span className="min-w-0">
              <span className="block truncate text-xs font-medium">
                {monthLabel(inv.month)}
              </span>
              <span className="block truncate text-2xs text-faint-foreground">
                issued {fmtDateTime(inv.issued_at)}
              </span>
            </span>
            <span className="text-right font-mono text-xs tabular-nums">
              {inr(inv.paise)}
            </span>
            <Chip tone={chip.tone}>{chip.label}</Chip>
            {/* A plain link, not a fetch: the browser downloads it with the
                session cookie and the server names the file. */}
            <a href={billingApi.invoiceUrl(inv.month, org)}
              className="inline-flex size-6 items-center justify-center rounded-md text-faint-foreground transition-colors hover:bg-foreground/5 hover:text-foreground"
              title={`Download the ${monthLabel(inv.month)} invoice as a PDF`}>
              <Download className="size-3.5" />
              <span className="sr-only">
                Download the {monthLabel(inv.month)} invoice
              </span>
            </a>
          </div>
        )
      })}
    </Panel>
  )
}

const KIND_LABEL: Record<PaymentKind, string> = {
  gateway: "Online",
  manual: "Recorded by hand",
  adjustment: "Adjustment",
}

/** What identifies this payment to somebody chasing it. The gateway's own
 *  reference first: it is the string a bank statement can be matched against. */
function reference(p: Payment): { text: string; mono: boolean } | null {
  if (p.provider_payment_id) {
    return { text: p.provider_payment_id, mono: true }
  }
  if (p.note) return { text: p.note, mono: false }
  if (p.recorded_by) return { text: `by ${p.recorded_by}`, mono: false }
  return null
}

export function PaymentsPanel({ b }: { b: BillingInfo }) {
  const rows = useMemo(
    () => [...b.payments].sort((x, y) =>
      toUtcDate(y.created_at).getTime() - toUtcDate(x.created_at).getTime()),
    [b.payments])

  return (
    <Panel title="Payments"
      note={rows.length ? `${rows.length} recorded` : undefined}>
      <div className={cn(PAYMENT_COLS, "wisp-thead h-9")}>
        <span>Received</span>
        <span className="text-right">Amount</span>
      </div>
      {rows.length === 0 && (
        <Empty>
          No payment recorded yet. Anything paid, online or by hand, lands here
          with its own reference.
        </Empty>
      )}
      {rows.map((p) => {
        const ref = reference(p)
        return (
          <div key={p.id} className={cn(PAYMENT_COLS, "wisp-row h-11")}>
            <span className="min-w-0">
              <span className="block truncate text-xs">
                {fmtDateTime(p.created_at)}
                <span className="ml-1.5 text-2xs text-faint-foreground">
                  {KIND_LABEL[p.kind]}
                </span>
              </span>
              {ref && (
                <span className={cn("block truncate text-2xs text-faint-foreground",
                  ref.mono && "font-mono")} title={ref.text}>
                  {ref.text}
                </span>
              )}
            </span>
            <span className="text-right font-mono text-xs tabular-nums">
              {inrAuto(p.paise)}
            </span>
          </div>
        )
      })}
    </Panel>
  )
}
