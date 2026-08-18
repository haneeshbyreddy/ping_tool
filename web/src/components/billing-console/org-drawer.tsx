// One org's billing drawer: the ledger it is billed from, the two rates it is
// billed at, and the four writes a superadmin has (rate override, exempt,
// payment or adjustment, deactivate).
//
// Money discipline, everywhere in here:
//   * paise on the wire, rupees only where a human types (rows.ts owns the
//     conversion and is the only place a 100 appears);
//   * NO optimistic updates. Every write invalidates and refetches, and every
//     control is disabled while one is in flight. A ledger that renders a
//     payment the server rejected is worse than a slow one;
//   * every dangerous control states its CONSEQUENCE, and the consequences
//     stated here are the ones the server actually has.

import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Ban, Download, IndianRupee, RotateCcw, Undo2,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { ApiError, billingApi } from "@/lib/api"
import {
  dayLabel, inrAuto, inrExact, monthLabel, stageMeta,
} from "@/lib/billing"
import { fmtDateTime } from "@/lib/format"
import type { Accrual, BillingConsoleOrg, Invoice, Payment } from "@/lib/types"
import { Chip } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Segmented } from "@/components/ui/segmented"
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  ConnCell, FlagCell, Outstanding, SignedAmount, WinningSide, balanceText,
} from "./cells"
import { paiseFromRupees, rupeesFromPaise } from "./rows"

type SaveBody = Omit<Parameters<typeof billingApi.adminSave>[0], "org_id">

const PAY_KINDS = [
  { value: "manual" as const, label: "Payment received" },
  { value: "adjustment" as const, label: "Adjustment" },
]

// ───────────────────────────────────────────────────────────────── furniture

function Section({ title, hint, right, children }: {
  title: string
  hint?: React.ReactNode
  right?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-2 border-t px-4 py-4 first:border-t-0">
      <div className="flex items-center justify-between gap-3">
        <h3 className="wisp-eyebrow">{title}</h3>
        {right}
      </div>
      {hint && <p className="max-w-prose text-2xs text-muted-foreground">{hint}</p>}
      {children}
    </section>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg border border-dashed px-3 py-4 text-center text-2xs text-faint-foreground">
      {children}
    </p>
  )
}

const INVOICE_TONE = {
  paid: "success", open: "muted", void: "muted",
} as const

// ────────────────────────────────────────────────────────────── rate override

/** A rupee field over a paise value. Blank means "no override": the global
 *  default shows through as the placeholder, so the operator can always read
 *  what they are about to overrule. */
function RateField({ label, unit, value, onChange, globalPaise, override, disabled }: {
  label: string
  unit: string
  value: string
  onChange: (v: string) => void
  globalPaise: number
  override: boolean
  disabled: boolean
}) {
  const bad = value.trim() !== "" && paiseFromRupees(value) == null
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <Label className="text-xs">{label}</Label>
        {override && (
          <span className="text-2xs font-medium text-muted-foreground">override</span>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        <div className="relative w-36">
          <IndianRupee className="pointer-events-none absolute top-1/2 left-2.5 size-3 -translate-y-1/2 text-faint-foreground" />
          <Input
            value={value}
            disabled={disabled}
            inputMode="decimal"
            spellCheck={false}
            placeholder={rupeesFromPaise(globalPaise)}
            onChange={(e) => onChange(e.target.value)}
            className={cn("h-8 pl-7 font-mono text-xs tabular-nums",
              bad && "border-destructive")}
          />
        </div>
        <span className="text-2xs text-faint-foreground">{unit}</span>
        {value.trim() !== "" && (
          <Button variant="ghost" size="sm" className="h-7 text-2xs text-muted-foreground"
            disabled={disabled} onClick={() => onChange("")}>
            Use global
          </Button>
        )}
      </div>
      {bad && (
        <p className="text-2xs text-destructive">
          Enter an amount in rupees, or clear the box to use the global default.
        </p>
      )}
    </div>
  )
}

// ────────────────────────────────────────────────────── payment / adjustment

function PaymentDialog({ open, onOpenChange, name, outstanding, busy, onSubmit }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  name: string
  outstanding: number
  busy: boolean
  onSubmit: (p: { kind: "manual" | "adjustment"; paise: number; note?: string }) => void
}) {
  const [kind, setKind] = useState<"manual" | "adjustment">("manual")
  const [amount, setAmount] = useState("")
  const [note, setNote] = useState("")
  useEffect(() => {
    if (open) { setKind("manual"); setAmount(""); setNote("") }
  }, [open])

  // An adjustment may be signed (a dispute cuts both ways); a payment is money
  // that arrived and cannot be negative. Refused here as well as on the server
  // so the operator learns the rule from the form, not from a 422.
  const paise = paiseFromRupees(amount, { signed: kind === "adjustment" })
  const needsNote = kind === "adjustment" && !note.trim()
  const zero = paise === 0
  const armed = paise != null && !zero && !needsNote && !busy

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Record against {name}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Segmented value={kind} options={PAY_KINDS} onChange={setKind} />

          <p className="text-xs text-muted-foreground">
            {kind === "manual"
              ? "Money that reached you outside the gateway. GPay, a bank transfer, cash. It lowers what this org owes."
              : "A correction to the balance. A positive amount credits the org and lowers what they owe. A negative amount charges them. Both need a note saying why."}
          </p>

          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Amount</Label>
            <div className="relative w-44">
              <IndianRupee className="pointer-events-none absolute top-1/2 left-2.5 size-3 -translate-y-1/2 text-faint-foreground" />
              <Input autoFocus value={amount} inputMode="decimal" spellCheck={false}
                placeholder={kind === "adjustment" ? "-250 or 250" : "2500"}
                disabled={busy}
                onChange={(e) => setAmount(e.target.value)}
                className="h-8 pl-7 font-mono text-xs tabular-nums" />
            </div>
            {amount.trim() !== "" && paise == null && (
              <p className="text-2xs text-destructive">
                {kind === "manual"
                  ? "A payment is a positive amount in rupees."
                  : "Enter an amount in rupees. A minus sign is allowed."}
              </p>
            )}
            {zero && (
              <p className="text-2xs text-destructive">Zero records nothing.</p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">
              Note {kind === "adjustment"
                ? <span className="text-destructive">required</span>
                : <span className="text-faint-foreground">optional</span>}
            </Label>
            <Input value={note} disabled={busy} spellCheck={false}
              placeholder={kind === "adjustment"
                ? "why the balance is being corrected"
                : "reference, or how it arrived"}
              onChange={(e) => setNote(e.target.value)}
              className="h-8 text-xs" />
          </div>

          {/* The one number the operator is actually deciding about. Recomputed
              from the same outstanding the header prints, so the preview and
              the row it will produce cannot disagree. */}
          {paise != null && !zero && (
            <p className="rounded-lg border bg-muted px-3 py-2 text-xs">
              Outstanding{" "}
              <span className="font-mono tabular-nums">{balanceText(outstanding)}</span>
              {" → "}
              <span className="font-mono font-semibold tabular-nums">
                {balanceText(outstanding - paise)}
              </span>
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" disabled={!armed}
            onClick={() => paise != null && onSubmit({
              kind, paise, note: note.trim() || undefined,
            })}>
            Record {kind === "manual" ? "payment" : "adjustment"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ───────────────────────────────────────────────────────────────── deactivate

/** The most dangerous button in the product, so the dialog is built like one:
 *  it NAMES the org, lists what actually happens (including the thing that
 *  does NOT), and will not arm until the org id is typed.
 *
 *  The consequence list is checked against the server, not assumed. The 402
 *  gate in server.py guards `/api/*` only, so edge ingest, the FSM, the
 *  watchdog and WhatsApp paging all keep running for a deactivated org. Saying
 *  otherwise here would be the honest-copy failure this codebase keeps paying
 *  for: a lapsed bill must not silence an alarm, and an operator who believes
 *  it does will hesitate at exactly the wrong moment. */
function DeactivateDialog({ open, onOpenChange, row, busy, onConfirm }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  row: BillingConsoleOrg
  busy: boolean
  onConfirm: () => void
}) {
  const [typed, setTyped] = useState("")
  useEffect(() => { if (open) setTyped("") }, [open])
  const armed = typed.trim() === row.org_id && !busy

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Deactivate {row.name}?</DialogTitle>
        </DialogHeader>

        <ul className="flex flex-col gap-2 text-xs text-muted-foreground">
          <li>
            <span className="font-medium text-foreground">Everyone in {row.name} is
            locked out of the dashboard.</span> Every request answers payment
            required, except the billing screen itself so they can still pay.
          </li>
          <li>
            <span className="font-medium text-foreground">The meter stops today.</span>{" "}
            Nothing already accrued is erased. The balance of{" "}
            <span className="font-mono">{balanceText(row.outstanding_paise)}</span> stays
            on the account, and billing reminders stop.
          </li>
          <li>
            <span className="font-medium text-foreground">Monitoring stops.</span>{" "}
            The probes are handed an empty device list, so nothing is polled and
            nobody is paged for this org. The probe boxes stay online and stay
            updatable. This is the only thing in the product that stops an alarm,
            which is why it is a typed confirmation and can never happen on its
            own: an overdue org that is merely LOCKED is still fully monitored.
          </li>
          <li>
            Reversible. Switching it back on resumes the meter from that day, so the
            deactivated days stay a hole in the ledger and are never billed later.
          </li>
        </ul>

        <div className="flex flex-col gap-1.5">
          <label className="text-xs text-muted-foreground">
            Type <span className="font-mono font-semibold text-foreground">{row.org_id}</span> to
            confirm
          </label>
          <Input autoFocus value={typed} spellCheck={false} placeholder={row.org_id}
            className="font-mono text-xs" disabled={busy}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && armed) onConfirm() }} />
        </div>

        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" size="sm" disabled={!armed} onClick={onConfirm}>
            Deactivate {row.org_id}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ──────────────────────────────────────────────────────────────── the ledger

function InvoiceRows({ invoices, orgId, busy, onSet }: {
  invoices: Invoice[]
  orgId: string
  busy: boolean
  onSet: (month: string, status: "open" | "void") => void
}) {
  if (!invoices.length) {
    return <Empty>No month has closed into an invoice yet.</Empty>
  }
  return (
    <div className="wisp-panel">
      <div className="wisp-thead grid h-8 grid-cols-[minmax(0,1fr)_6rem_5rem_minmax(0,7rem)] items-center gap-3 px-3">
        <span>Month</span>
        <span className="text-right">Amount</span>
        <span>Status</span>
        <span className="text-right">Issued</span>
      </div>
      {invoices.map((inv) => (
        <div key={inv.month}
          className="wisp-row grid grid-cols-[minmax(0,1fr)_6rem_5rem_minmax(0,7rem)] items-center gap-3 px-3 py-2">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-xs font-medium">{monthLabel(inv.month)}</span>
            <a href={billingApi.invoiceUrl(inv.month, orgId)}
              title="Download the invoice PDF the org sees"
              className="shrink-0 text-faint-foreground transition-colors hover:text-foreground">
              <Download className="size-3" />
            </a>
          </span>
          <span className="text-right font-mono text-xs tabular-nums">
            {inrAuto(inv.paise)}
          </span>
          <Chip tone={INVOICE_TONE[inv.status]}>{inv.status}</Chip>
          <span className="flex items-center justify-end gap-1.5">
            <span className="text-2xs text-faint-foreground">
              {fmtDateTime(inv.issued_at)}
            </span>
            {inv.status !== "paid" && (
              <Button variant="ghost" size="sm" className="h-6 px-1.5 text-2xs"
                disabled={busy}
                title={inv.status === "open"
                  ? "Void this invoice. It stops driving the dunning ladder."
                  : "Put this invoice back on the books."}
                onClick={() => onSet(inv.month, inv.status === "open" ? "void" : "open")}>
                {inv.status === "open" ? "Void" : "Reopen"}
              </Button>
            )}
          </span>
        </div>
      ))}
    </div>
  )
}

const PAY_LABEL: Record<Payment["kind"], string> = {
  gateway: "Online", manual: "Manual", adjustment: "Adjustment",
}

function PaymentRows({ payments }: { payments: Payment[] }) {
  if (!payments.length) {
    return <Empty>No payment or adjustment has been recorded.</Empty>
  }
  return (
    <div className="wisp-panel">
      <div className="wisp-thead grid h-8 grid-cols-[6.5rem_5.5rem_6rem_minmax(0,1fr)] items-center gap-3 px-3">
        <span>When</span>
        <span>Kind</span>
        <span className="text-right">Amount</span>
        <span>Reference</span>
      </div>
      {payments.map((p) => (
        <div key={p.id}
          className="wisp-row grid grid-cols-[6.5rem_5.5rem_6rem_minmax(0,1fr)] items-center gap-3 px-3 py-2">
          <span className="text-2xs text-muted-foreground">{fmtDateTime(p.created_at)}</span>
          <span className="text-2xs text-muted-foreground">{PAY_LABEL[p.kind]}</span>
          <span className="text-right"><SignedAmount paise={p.paise} /></span>
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-2xs text-muted-foreground"
              title={p.note ?? p.provider_payment_id ?? undefined}>
              {p.note || p.provider_payment_id || (p.provider ? p.provider : "—")}
            </span>
            {/* Who put this number on the books. A manual ledger without an
                author is an unanswerable question three months later. */}
            <span className="truncate text-2xs text-faint-foreground">
              {p.recorded_by ? `recorded by ${p.recorded_by}` : "recorded by the gateway"}
            </span>
          </span>
        </div>
      ))}
    </div>
  )
}

const ACCRUAL_COLS =
  "grid grid-cols-[3.5rem_6rem_3.5rem_5rem_minmax(0,1fr)] items-center gap-3 px-3"

function AccrualRows({ accruals }: { accruals: Accrual[] }) {
  // Newest month first, newest day first inside it: the operator opened this
  // to see what the meter did recently, not to read January.
  const months = useMemo(() => {
    const by = new Map<string, Accrual[]>()
    for (const a of accruals) {
      const m = a.day.slice(0, 7)
      const list = by.get(m)
      if (list) list.push(a)
      else by.set(m, [a])
    }
    return [...by.entries()]
      .sort((a, b) => b[0].localeCompare(a[0]))
      .map(([month, rows]) => ({
        month,
        rows: [...rows].sort((a, b) => b.day.localeCompare(a.day)),
        // The month's total is a SUM of these rows, exactly as the invoice is.
        // Recomputing it here from the same rows is what keeps the drawer and
        // the bill from ever printing two numbers for one month.
        paise: rows.reduce((n, r) => n + r.paise, 0),
      }))
  }, [accruals])

  if (!months.length) {
    return <Empty>The meter has not written a day for this org yet.</Empty>
  }

  return (
    <div className="flex flex-col gap-3">
      {months.map((m) => (
        <div key={m.month} className="wisp-panel">
          <div className="wisp-panel-head py-2">
            <span className="text-xs font-medium">{monthLabel(m.month)}</span>
            <span className="flex items-baseline gap-2">
              <span className="text-2xs text-faint-foreground">
                {m.rows.length} day{m.rows.length === 1 ? "" : "s"}
              </span>
              <span className="font-mono text-xs font-semibold tabular-nums">
                {inrAuto(m.paise)}
              </span>
            </span>
          </div>
          <div className={cn(ACCRUAL_COLS, "wisp-thead h-8")}>
            <span>Day</span>
            <span className="text-right">ONUs</span>
            <span className="text-right">Gear</span>
            <span className="text-right">Charge</span>
            <span>Meter</span>
          </div>
          {m.rows.map((a) => (
            <div key={a.day} className={cn(ACCRUAL_COLS, "wisp-row py-1.5")}>
              <span className="font-mono text-2xs text-muted-foreground">
                {dayLabel(a.day)}
              </span>
              <ConnCell count={a.conn_count} source={a.conn_source}
                absentReason="No count was recorded for this day." />
              <span className="text-right font-mono text-xs tabular-nums text-muted-foreground">
                {a.device_count}
              </span>
              <span className="flex flex-col items-end leading-tight">
                <span className="font-mono text-xs tabular-nums">{inrExact(a.paise)}</span>
                <WinningSide side={a.winning_side} />
              </span>
              <FlagCell flags={a.flags} />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

// ────────────────────────────────────────────────────────────────── the drawer

export function OrgDrawer({ orgId, onClose }: {
  orgId: string
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [payOpen, setPayOpen] = useState(false)
  const [killOpen, setKillOpen] = useState(false)
  const [conn, setConn] = useState("")
  const [floor, setFloor] = useState("")

  const q = useQuery({
    queryKey: ["billing-console", orgId],
    queryFn: () => billingApi.console(orgId),
  })

  const row = q.data?.orgs.find((o) => o.org_id === orgId) ?? null
  const ledger = q.data?.ledger
  const globals = q.data?.rates
  const month = q.data?.month ?? ""

  // Seeded from the row and re-seeded only when the STORED value moves, not on
  // every refetch: keying the effect on the object identity would wipe what
  // the operator is halfway through typing every time the query settles.
  useEffect(() => {
    setConn(row?.conn_rate_paise != null ? rupeesFromPaise(row.conn_rate_paise) : "")
  }, [orgId, row?.conn_rate_paise])
  useEffect(() => {
    setFloor(row?.floor_paise != null ? rupeesFromPaise(row.floor_paise) : "")
  }, [orgId, row?.floor_paise])

  const save = useMutation({
    mutationFn: (v: { body: SaveBody; done: string }) =>
      billingApi.adminSave({ org_id: orgId, ...v.body }),
    onSuccess: (_r, v) => {
      // Refetch, never patch. Outstanding is computed server-side from every
      // accrual and every payment, so the only honest way to learn the new
      // balance is to ask. `billing` is the org's own screen, which a
      // superadmin write moves under its owner's feet.
      qc.invalidateQueries({ queryKey: ["billing-console"] })
      qc.invalidateQueries({ queryKey: ["billing"] })
      toast.success(v.done)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Billing write failed"),
  })
  const busy = save.isPending

  const monthToDate = useMemo(
    () => (ledger?.accruals ?? [])
      .filter((a) => a.day.startsWith(month))
      .reduce((n, a) => n + a.paise, 0),
    [ledger, month],
  )

  const saveRates = () => {
    const c = conn.trim() === "" ? null : paiseFromRupees(conn)
    const f = floor.trim() === "" ? null : paiseFromRupees(floor)
    if ((conn.trim() !== "" && c == null) || (floor.trim() !== "" && f == null)) {
      toast.error("Enter each rate in rupees, or clear the box to use the global default")
      return
    }
    save.mutate({
      body: { conn_rate_paise: c, floor_paise: f },
      done: "Rates saved. They apply from today forward.",
    })
  }

  const ratesDirty = row != null && (
    conn.trim() !== (row.conn_rate_paise != null ? rupeesFromPaise(row.conn_rate_paise) : "")
    || floor.trim() !== (row.floor_paise != null ? rupeesFromPaise(row.floor_paise) : ""))

  const stage = row ? stageMeta(row.stage, row.days_overdue) : null

  return (
    <Sheet open onOpenChange={(v) => { if (!v) onClose() }}>
      {/* Wider than the default sheet on purpose: this is a LEDGER, and a
          money column that wraps is a money column that gets misread. Full
          width below sm, capped at 48rem above it. */}
      <SheetContent side="right"
        className="gap-0 p-0 w-full! sm:max-w-3xl!">
        <SheetHeader className="border-b px-4 py-3 pr-12">
          <SheetTitle className="flex items-center gap-2 truncate text-base">
            <span className="truncate">{row?.name ?? orgId}</span>
            {stage && (
              <span className={cn("shrink-0 rounded-4xl px-2 py-0.5 text-2xs font-medium",
                stage.className)}>
                {stage.label}
              </span>
            )}
          </SheetTitle>
          <SheetDescription className="font-mono text-2xs">{orgId}</SheetDescription>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {q.isLoading && (
            <div className="flex flex-col gap-3 p-4">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          )}
          {q.isError && (
            <p className="p-8 text-center text-xs text-destructive">
              {q.error instanceof ApiError ? q.error.message : "Could not load this ledger"}
            </p>
          )}
          {!q.isLoading && !row && q.isSuccess && (
            <p className="p-8 text-center text-xs text-faint-foreground">
              This organization is no longer on the platform.
            </p>
          )}

          {row && (
            <>
              {/* The three numbers the drawer exists to answer, in the order
                  they get asked: what is owed, what this month has cost so
                  far, what today added. */}
              <div className="grid grid-cols-3 gap-3 px-4 py-4">
                <div className="flex flex-col gap-0.5">
                  <span className="wisp-eyebrow">Outstanding</span>
                  <Outstanding paise={row.outstanding_paise} big className="items-start" />
                </div>
                <div className="flex flex-col gap-0.5">
                  <span className="wisp-eyebrow">{monthLabel(month)} to date</span>
                  <span className="font-mono text-xl font-semibold tabular-nums">
                    {inrAuto(monthToDate)}
                  </span>
                </div>
                <div className="flex flex-col gap-0.5">
                  <span className="wisp-eyebrow">Today</span>
                  {row.today?.paise != null ? (
                    <>
                      <span className="font-mono text-xl font-semibold tabular-nums">
                        {inrExact(row.today.paise)}
                      </span>
                      <WinningSide side={row.today.winning_side} />
                    </>
                  ) : (
                    <span className="text-xs text-faint-foreground">
                      {row.exempt ? "Not metered. This org is exempt."
                        : row.deactivated ? "Not metered. This org is deactivated."
                        : "Today's meter has not run yet."}
                    </span>
                  )}
                </div>
              </div>

              <Section title="Invoices"
                hint="An invoice closes on the 1st and is the sum of that month's accrual rows. Paid is derived from the payments on this account, so there is no paid button: an invoice settles itself the moment the balance covers it. Voiding one takes it off the dunning ladder."
                right={
                  <Button size="sm" className="h-7 text-xs" disabled={busy}
                    onClick={() => setPayOpen(true)}>
                    Record payment
                  </Button>
                }>
                <InvoiceRows invoices={ledger?.invoices ?? []} orgId={orgId} busy={busy}
                  onSet={(m, status) => save.mutate({
                    body: { invoice: { month: m, status } },
                    done: status === "void"
                      ? `${monthLabel(m)} voided`
                      : `${monthLabel(m)} put back on the books`,
                  })} />
              </Section>

              <Section title="Payments and adjustments">
                <PaymentRows payments={ledger?.payments ?? []} />
              </Section>

              <Section title="Daily meter"
                hint="This month and last. Each row is one operator day, charged at max(ONUs × rate, gear × floor) divided by the days in that month. The invoice is the sum of these rows and is never recomputed.">
                <AccrualRows accruals={ledger?.accruals ?? []} />
              </Section>

              <Section title="Rates for this org"
                hint="Leave a box empty to use the global default. A rate change applies forward only: every accrual row already written keeps the rate it was charged at, and no invoice is rewritten.">
                <div className="flex flex-wrap gap-6">
                  <RateField label="Per ONU" unit="per month"
                    value={conn} onChange={setConn} disabled={busy}
                    globalPaise={globals?.conn_paise ?? 0}
                    override={row.conn_rate_paise != null} />
                  <RateField label="Per monitored device" unit="per month, the floor"
                    value={floor} onChange={setFloor} disabled={busy}
                    globalPaise={globals?.floor_paise ?? 0}
                    override={row.floor_paise != null} />
                </div>
                <div className="flex items-center gap-2">
                  <Button size="sm" className="h-7 w-fit text-xs"
                    disabled={busy || !ratesDirty} onClick={saveRates}>
                    Save rates
                  </Button>
                  {(row.conn_rate_paise != null || row.floor_paise != null) && (
                    <Button variant="ghost" size="sm"
                      className="h-7 w-fit text-xs text-muted-foreground" disabled={busy}
                      onClick={() => save.mutate({
                        body: { conn_rate_paise: null, floor_paise: null },
                        done: "Back on the global rates",
                      })}>
                      <RotateCcw className="size-3" /> Clear both overrides
                    </Button>
                  )}
                </div>
              </Section>

              <Section title="Not billed">
                <label className="flex max-w-prose items-center justify-between gap-4">
                  <span className="flex flex-col">
                    <span className="text-sm font-medium">Exempt this org from billing</span>
                    <span className="text-2xs text-muted-foreground">
                      The meter stops, no invoice closes and no reminder goes out.
                      Nothing already accrued is erased. Switching it back on resumes
                      the meter from that day, so the exempt days stay a hole in the
                      ledger and are never billed later.
                    </span>
                  </span>
                  <Switch checked={row.exempt} disabled={busy}
                    onCheckedChange={(v) => save.mutate({
                      body: { exempt: v },
                      done: v ? `${row.name} is no longer billed`
                        : `${row.name} is billed again from today`,
                    })} />
                </label>
              </Section>

              <Section title="Deactivation">
                {row.deactivated ? (
                  <div className="flex max-w-prose flex-col gap-2">
                    <p className="text-xs text-muted-foreground">
                      This org is deactivated. Its dashboard is locked, the meter is
                      stopped, and its probes are polling nothing. Reactivating
                      resumes both from today.
                    </p>
                    <Button variant="outline" size="sm" className="h-7 w-fit text-xs"
                      disabled={busy}
                      onClick={() => save.mutate({
                        body: { deactivated: false },
                        done: `${row.name} reactivated. The meter resumes today.`,
                      })}>
                      <Undo2 className="size-3" /> Reactivate {row.name}
                    </Button>
                  </div>
                ) : (
                  <div className="flex max-w-prose flex-col gap-2">
                    <p className="text-xs text-muted-foreground">
                      Locks everyone in this org out of the dashboard, stops the meter
                      and stands its probes down. Never automatic, however overdue an
                      account gets: a lapsed bill must not silence an alarm, so an
                      org that is merely locked stays fully monitored. Standing one
                      down is always a human decision.
                    </p>
                    <Button variant="outline" size="sm"
                      className="h-7 w-fit border-destructive/40 text-xs text-destructive hover:bg-destructive-soft hover:text-destructive"
                      disabled={busy} onClick={() => setKillOpen(true)}>
                      <Ban className="size-3" /> Deactivate {row.name}
                    </Button>
                  </div>
                )}
              </Section>

              <PaymentDialog open={payOpen} onOpenChange={setPayOpen}
                name={row.name} outstanding={row.outstanding_paise} busy={busy}
                onSubmit={(p) => {
                  setPayOpen(false)
                  save.mutate({
                    body: { payment: p },
                    done: p.kind === "manual"
                      ? `Payment of ${inrAuto(p.paise)} recorded`
                      : "Adjustment recorded",
                  })
                }} />

              <DeactivateDialog open={killOpen} onOpenChange={setKillOpen} row={row}
                busy={busy}
                onConfirm={() => {
                  setKillOpen(false)
                  save.mutate({
                    body: { deactivated: true },
                    done: `${row.name} deactivated. Its probes are standing down.`,
                  })
                }} />
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
