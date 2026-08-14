import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Crosshair, ExternalLink, MapPin, Pencil, Phone, Shield, ShieldOff, Spline, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { ApiError, inventoryApi } from "@/lib/api"
import type {
  RadiusStatus, Subscriber, SubscriberPlantHop, UserMac, WebMacStatus,
} from "@/lib/types"
import {
  ago, fmtDateTime, isDownState, isFresh, onuName, onuSearchKey, onuSev,
} from "@/lib/format"
import { ratioLabel } from "@/map/drops"
import { RowTag } from "@/components/device-detail"
import { StatusDot } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"

const TONE = {
  ok: "success", warn: "warning", crit: "destructive", offline: "destructive",
} as const

function ReferenceToggle({ mac, witness, scopeOrg, onDone }: {
  mac: string; witness: boolean; scopeOrg: string | null; onDone: () => void
}) {
  const [open, setOpen] = useState(false)
  const m = useMutation({
    mutationFn: () => inventoryApi.setOnuWitness({ mac, witness: !witness, org_id: scopeOrg }),
    onSuccess: () => {
      setOpen(false)
      onDone()
      toast.success(witness
        ? "No longer a reference point. Pin and details kept."
        : "Marked as a reference point")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })
  return (
    <>
      <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
        title={witness
          ? "Stop treating this customer as evidence of a fibre cut. Pin and details are kept."
          : "Vouch for this customer's power supply. UPS, inverter, solar or tower only."}
        onClick={() => setOpen(true)}>
        {witness ? <ShieldOff className="size-3" /> : <Shield className="size-3" />}
        {witness ? "Withdraw" : "Reference"}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {witness ? "Withdraw reference point" : "Mark as reference point"}
            </DialogTitle>
            <DialogDescription className="font-mono">{mac}</DialogDescription>
          </DialogHeader>
          {witness ? (
            <p className="text-xs text-muted-foreground">
              This customer stays on the map with their name, number and pin. We
              simply stop treating their going dark as evidence of a fibre cut.
            </p>
          ) : (
            <div className="rounded-lg border border-warning/40 bg-warning-soft/40 px-3 py-2 text-xs">
              <p className="font-semibold text-warning">
                Only for customers whose power you can rely on
              </p>
              <p className="mt-0.5 text-muted-foreground">
                UPS, inverter, solar or tower supply. Not plain mains. If one of
                these goes dark, power can't explain it, so we call it a cut and
                send a crew.
              </p>
            </div>
          )}
          <DialogFooter className="gap-2">
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
            <Button size="sm" onClick={() => m.mutate()} disabled={m.isPending}>
              {witness ? "Withdraw" : "Yes, its power is reliable"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function cumulativeSplit(chain: SubscriberPlantHop[]): number | null {
  const passives = chain.filter((h) => h.split_ratio != null || isPassiveHop(h))
  if (!passives.length) return null
  let total = 1
  for (const h of passives) {
    if (h.split_ratio == null) return null
    total *= h.split_ratio
  }
  return total
}
const isPassiveHop = (h: SubscriberPlantHop): boolean =>
  ["splitter", "fdb", "closure"].includes((h.device_type ?? "").toLowerCase())

// Why there is no address to show. Same split SnmpDiagnosis/RxDiagnosis make: an
// empty column has several meanings that need OPPOSITE actions, and the worst of
// them is `partial` — a truncated read makes a customer who HAS an address look
// exactly like one who does not.
function noMacReason(status: WebMacStatus | null): string {
  if (!status) {
    return "This OLT's address table hasn't been read yet."
  }
  switch (status.state) {
    case "ok":
      return "The OLT currently knows no address behind this ONU. It learns "
        + "them from traffic, so an idle or offline customer drops off the table."
    case "partial":
      return "The last read of this OLT's address table was INCOMPLETE, so this "
        + "customer may well have an address we simply didn't get."
    case "no_profile":
      return "No address-table recipe is configured for this OLT's vendor."
    case "no_credentials":
      return "Nobody has stored this OLT's web-UI login, so its address table "
        + "can't be opened."
    case "login":
      return "This OLT is refusing the stored web-UI login, so its address "
        + "table can't be read."
    case "unreachable":
      return "This OLT's address table couldn't be reached on its web UI."
    case "skipped":
      return "The address table wasn't read on the last pass. "
        + (status.detail ?? "")
    default:
      return "The last attempt to read this OLT's address table failed."
  }
}

function UserMacs({ macs, status }: {
  macs: UserMac[]; status: WebMacStatus | null
}) {
  const [copied, setCopied] = useState<string | null>(null)
  const copy = (mac: string) => {
    void navigator.clipboard?.writeText(mac).then(
      () => { setCopied(mac); setTimeout(() => setCopied(null), 1200) },
      () => undefined)
  }
  if (!macs.length) {
    return (
      <span className={cn("text-faint-foreground",
        status?.state === "partial" && "text-warning")}>
        {noMacReason(status)}
      </span>
    )
  }
  return (
    <div className="space-y-1">
      {macs.map((m) => {
        const live = isFresh(m.last_seen_at)
        return (
          <div key={m.mac} className="flex flex-wrap items-baseline gap-x-2">
            <button
              type="button"
              onClick={() => copy(m.mac)}
              title="Copy this address"
              className={cn("font-mono text-xs hover:underline",
                !live && "text-muted-foreground")}
            >
              {m.mac}
            </button>
            {copied === m.mac && (
              <span className="text-2xs text-success">copied</span>
            )}
            {m.vlan && (
              <span className="text-2xs text-faint-foreground">VLAN {m.vlan}</span>
            )}
            {!live && (
              <span className="text-2xs text-faint-foreground">
                last seen {ago(m.last_seen_at)}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function Row({ label, children, className }: {
  label: string; children: React.ReactNode; className?: string
}) {
  return (
    <div className={cn("flex gap-2 py-1", className)}>
      <span className="w-[5.5rem] shrink-0 text-2xs text-faint-foreground">{label}</span>
      <div className="min-w-0 flex-1 text-xs">{children}</div>
    </div>
  )
}

// A blank billing block has four meanings and they take opposite actions, so the
// server ships facts (radius_status) and this writes the sentence — the same split
// SnmpDiagnosis and RxDiagnosis keep. An org with no panel configured gets NOTHING
// rather than an explanation of a feature it does not use.
function noBillingReason(st: RadiusStatus | null): string | null {
  if (!st) return null
  const was = st.last_ok_at ? ` It last worked ${ago(st.last_ok_at)}.` : ""
  switch (st.state) {
    case "ok":
    case "partial":
      return "This subscriber isn't in the billing panel, or nothing ties them to "
        + "this ONU yet — a disconnected customer stops passing traffic, so their "
        + "address ages out of the OLT's table."
    case "no_credentials":
      return "Nobody has stored the billing panel's sign-in details."
    case "no_profile":
      return "There's no recipe for this billing panel yet."
    case "forbidden":
      return "The billing panel signed in but would not hand over the customer "
        + "list — that login needs permission to export it."
    case "login":
      return `The billing panel is refusing the stored sign-in.${was}`
    case "unreachable":
      return `The billing panel couldn't be reached.${was}`
    default:
      return (st.detail || "The last billing sync failed.") + was
  }
}

// With several panels connected, "why is there no name here" is only answerable
// once one of them is working: a panel that read fine means the customer really
// is absent, and only when NONE did is the trouble worth naming. Picking the
// first failure would blame a second panel for a subscriber the first one covers.
function panelToBlame(panels: RadiusStatus[]): RadiusStatus | null {
  if (!panels.length) return null
  return panels.find((p) => p.state === "ok" || p.state === "partial") ?? panels[0]
}

// Only what BILLING alone can know lives here — account, package, status,
// expiry. The identity facts (name, phone, address) are composed into the
// Customer section with per-fact provenance, so one subscriber is never
// introduced twice.
function BillingSection({ sub }: { sub: Subscriber }) {
  const r = sub.radius
  const st = panelToBlame(sub.radius_panels ?? [])
  if (!st && !r) return null
  // When the ONU itself could not be resolved the card already says why, at the
  // top and in stronger terms. A second, speculative explanation down here would
  // compete with it and read as a different fault.
  if (!r && (!sub.matched || sub.ambiguous)) return null
  const reason = r ? null : noBillingReason(st)
  if (!r && !reason) return null
  return (
    <Section title="Billing">
      {r ? (
        <div className="space-y-0">
          <Row label="Account"><span className="font-mono">{r.username}</span></Row>
          {r.package && <Row label="Package">{r.package}</Row>}
          {r.status && (
            <Row label="Status">
              <span className={cn(r.status !== "active" && "text-warning")}>
                {r.status}
              </span>
            </Row>
          )}
          {r.expiry && (
            <Row label="Expiry">
              <span className="text-muted-foreground">{r.expiry}</span>
            </Row>
          )}
          <p className="pt-1 text-2xs text-faint-foreground">
            From {r.account_label || "the billing panel"}, matched on{" "}
            {r.match_by === "mac" ? "the router's MAC address" : "the ONU's name"}
            {r.updated_at ? ` · ${ago(r.updated_at)}` : ""}
          </p>
        </div>
      ) : (
        <p className="py-1 text-xs text-muted-foreground">{reason}</p>
      )}
    </Section>
  )
}

const digitsOf = (v?: string | null) => (v ?? "").replace(/\D/g, "")

function PhoneRow({ number, tag }: { number: string; tag?: "field" | "billing" }) {
  return (
    <Row label="Phone">
      <a href={`tel:${number}`}
        className="inline-flex items-center gap-1 font-mono underline-offset-2 hover:underline">
        <Phone className="size-3" />{number}
      </a>
      {tag && <span className="ml-1.5 text-2xs text-faint-foreground">{tag}</span>}
    </Row>
  )
}

// One identity, composed from both sources with per-fact provenance. The two
// phones are often DIFFERENT PEOPLE — the on-site contact a tech collected vs
// the account holder billing registered — so when they differ both render,
// tagged. A blank in one source never hides the other's fact.
// The survey label is USUALLY the billing account name — the operator's own
// convention, 137 of 164 labeled subscribers on the live fleet — so a label
// matching the USERNAME is not a competing name: the billing full name then
// renders as the plain Name row. Only a label matching neither is a real
// disagreement worth framing as one.
function IdentityRows({ sub, canWrite }: { sub: Subscriber; canWrite: boolean }) {
  const rec = sub.record
  const r = sub.radius
  const labelIsAccount = !!(rec?.label && r?.username
    && onuSearchKey(rec.label) === onuSearchKey(r.username))
  const billingName = !!(labelIsAccount && r?.name)
  const namesDiffer = !!(rec?.label && r?.name && !labelIsAccount
    && onuSearchKey(rec.label) !== onuSearchKey(r.name))
  const twoPhones = !!(rec?.phone && r?.mobile
    && digitsOf(r.mobile) !== digitsOf(rec.phone))
  const any = billingName || namesDiffer || rec?.phone || r?.mobile
    || r?.address || rec?.notes
  if (!any) {
    return (
      <p className="py-1 text-xs text-muted-foreground">
        {rec?.label || r?.name
          ? "Only a name on file."
            + (canWrite ? " Add a number so a fault on this drop reaches somebody." : "")
          : "No customer details recorded yet."
            + (canWrite ? " Add a name and number so a fault on this drop reaches somebody." : "")}
      </p>
    )
  }
  return (
    <div className="space-y-0">
      {billingName && <Row label="Name">{r!.name}</Row>}
      {namesDiffer && (
        <Row label="Name">
          {rec!.label}
          <p className="text-2xs text-faint-foreground">billing: {r!.name}</p>
        </Row>
      )}
      {rec?.phone && (
        <PhoneRow number={rec.phone} tag={twoPhones ? "field" : undefined} />
      )}
      {r?.mobile && (!rec?.phone || twoPhones) && (
        <PhoneRow number={r.mobile} tag={twoPhones ? "billing" : undefined} />
      )}
      {r?.address && (
        <Row label="Address">
          <span className="text-muted-foreground">{r.address}</span>
        </Row>
      )}
      {rec?.notes && (
        <Row label="Notes">
          <span className="text-muted-foreground">{rec.notes}</span>
        </Row>
      )}
    </div>
  )
}

function Section({ title, children, action }: {
  title: string; children: React.ReactNode; action?: React.ReactNode
}) {
  return (
    <div className="border-b px-4 py-3 last:border-b-0">
      <div className="mb-1 flex items-center justify-between gap-2">
        <p className="wisp-eyebrow">{title}</p>
        {action}
      </div>
      {children}
    </div>
  )
}

function ContactForm({ sub, onDone }: { sub: Subscriber; onDone: () => void }) {
  const { scopeOrg } = useAuth()
  const qc = useQueryClient()
  const [label, setLabel] = useState(sub.record?.label ?? "")
  const [phone, setPhone] = useState(sub.record?.phone ?? "")
  const [notes, setNotes] = useState(sub.record?.notes ?? "")

  const save = useMutation({
    mutationFn: () => inventoryApi.setOnuContact({
      mac: sub.mac, label: label.trim() || null, phone: phone.trim() || null,
      notes: notes.trim() || null, org_id: scopeOrg,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subscriber", sub.mac] })
      qc.invalidateQueries({ queryKey: ["optics"] })
      qc.invalidateQueries({ queryKey: ["onu-places"] })
      qc.invalidateQueries({ queryKey: ["onu-coverage"] })
      qc.invalidateQueries({ queryKey: ["onu-search"] })
      toast.success("Subscriber details saved")
      onDone()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <label className="text-2xs text-faint-foreground">Customer name</label>
        <Input value={label} onChange={(e) => setLabel(e.target.value.toUpperCase())}
          placeholder="RAMESH KULKARNI" className="h-8 text-xs" />
      </div>
      <div className="space-y-1">
        <label className="text-2xs text-faint-foreground">Phone</label>
        <Input value={phone} onChange={(e) => setPhone(e.target.value)}
          placeholder="9876543210" type="tel" className="h-8 font-mono text-xs" />
      </div>
      <div className="space-y-1">
        <label className="text-2xs text-faint-foreground">Notes</label>
        <Textarea value={notes} onChange={(e) => setNotes(e.target.value)}
          placeholder="Gate code, landmark, who to ask for…"
          className="min-h-[3.5rem] text-xs" />
      </div>
      <div className="flex justify-end gap-2 pt-0.5">
        <Button variant="ghost" size="sm" className="h-7 text-2xs" onClick={onDone}>
          Cancel
        </Button>
        <Button size="sm" className="h-7 text-2xs"
          onClick={() => save.mutate()} disabled={save.isPending}>
          Save
        </Button>
      </div>
    </div>
  )
}

export function SubscriberDialog({ mac, onClose, actions }: {
  mac: string; onClose: () => void; actions?: SubscriberActions
}) {
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="gap-0 overflow-hidden p-0 sm:max-w-md" showCloseButton={false}>
        <DialogTitle className="sr-only">Subscriber {mac}</DialogTitle>
        <div className="max-h-[75vh] overflow-y-auto">
          <SubscriberDetail mac={mac} actions={{ ...actions, onClose }} />
        </div>
      </DialogContent>
    </Dialog>
  )
}

export interface SubscriberActions {
  onPlace?: (mac: string, label: string) => void
  onOpenOlt?: (deviceId: number, mac: string) => void
  onOpenPassive?: (deviceId: number) => void
  onTraceDrop?: (mac: string) => void
  onClose?: () => void
}

export function SubscriberDetail({ mac, actions, fibre }: {
  mac: string; actions?: SubscriberActions; fibre?: React.ReactNode
}) {
  const { canWrite, scopeOrg } = useAuth()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  useEffect(() => { setEditing(false) }, [mac])

  const q = useQuery({
    queryKey: ["subscriber", mac, scopeOrg],
    queryFn: () => inventoryApi.subscriber(mac, scopeOrg),
    enabled: !!mac,
  })

  const unpin = useMutation({
    mutationFn: () => inventoryApi.setOnuPlace({
      mac, lat: null, lng: null, org_id: scopeOrg }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["subscriber", mac] })
      qc.invalidateQueries({ queryKey: ["onu-places"] })
      qc.invalidateQueries({ queryKey: ["onu-coverage"] })
      qc.invalidateQueries({ queryKey: ["pon-faults"] })
      qc.invalidateQueries({ queryKey: ["pon-summary"] })
      toast.success("Taken off the map. Customer details kept.")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't remove"),
  })

  if (q.isLoading) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-4 w-40" /><Skeleton className="h-3 w-56" />
        <Skeleton className="h-3 w-32" />
      </div>
    )
  }
  if (!q.data) {
    return <div className="p-4 text-xs text-muted-foreground">Couldn't load this subscriber.</div>
  }

  const sub = q.data
  const rec = sub.record
  const r = sub.roster
  const sev = r ? onuSev(r) : "offline"
  const name = onuName({
    label: rec?.label, radius_name: sub.radius?.name, name: r?.name,
    serial: sub.mac,
  })
  const frozen = !!sub.olt && isDownState(sub.olt.state)
  const opticsFresh = isFresh(sub.olt?.optics_updated_at)
  const dark = !!r && r.state !== "online"

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex items-start gap-2.5 border-b px-4 py-3">
        <span className="mt-1"><StatusDot tone={TONE[sev]} /></span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="min-w-0 truncate text-sm font-semibold">
              {name || <span className="text-muted-foreground">Unnamed subscriber</span>}
            </p>
            {rec?.witness && <RowTag tone="success" title="Power-backed reference point. Tells a fibre cut from an area power cut.">reference</RowTag>}
          </div>
          <p className="mt-0.5 truncate font-mono text-2xs text-muted-foreground"
            title="The ONU's serial as the OLT reports it. The sticker on the box, not the customer's own device.">
            ONU {sub.mac}
          </p>
        </div>
        {actions?.onClose && (
          <Button variant="ghost" size="icon" className="size-6 shrink-0"
            onClick={actions.onClose}><X className="size-3.5" /></Button>
        )}
      </div>

      {!sub.matched && (
        <div className="border-b bg-warning-soft/40 px-4 py-2 text-xs text-warning">
          In no current roster. The box was probably swapped. Details below are
          kept; re-record them under the new ONU.
        </div>
      )}
      {sub.ambiguous && (
        <div className="border-b bg-warning-soft/40 px-4 py-2 text-xs text-warning">
          Registered on {sub.slots} live slots, so we can't say which OLT this
          drop is on. Clearing the stale registration on the OLT fixes it.
        </div>
      )}

      <Section title="Customer"
        action={canWrite && !editing && (
          <Button variant="ghost" size="sm" className="h-6 px-1.5 text-2xs"
            onClick={() => setEditing(true)}>
            <Pencil className="size-3" /> {rec?.label || rec?.phone ? "Edit" : "Add"}
          </Button>
        )}>
        {editing ? (
          <ContactForm sub={sub} onDone={() => setEditing(false)} />
        ) : (
          <IdentityRows sub={sub} canWrite={canWrite} />
        )}
      </Section>

      <BillingSection sub={sub} />

      <Section title="Where it hangs">
        {sub.olt ? (
          <>
            <Row label="OLT">
              <button className="underline-offset-2 hover:underline disabled:no-underline"
                disabled={!actions?.onOpenOlt}
                onClick={() => actions?.onOpenOlt?.(sub.olt!.id, sub.mac)}>
                <span className="font-mono">{sub.olt.name}</span>
              </button>
              {r?.pon_port && <span className="text-muted-foreground"> · PON {r.pon_port}</span>}
              {r?.onu_id != null && <span className="text-muted-foreground"> · ONU {r.onu_id}</span>}
            </Row>
            {sub.drop ? (
              <Row label="Drop from">
                <div className="flex flex-wrap items-center gap-x-1 gap-y-0.5">
                  {sub.drop.chain.filter(isPassiveHop).map((hop, i) => (
                    <span key={hop.id} className="flex items-center gap-1">
                      {i > 0 && <span className="text-faint-foreground">←</span>}
                      <button className="underline-offset-2 hover:underline disabled:no-underline"
                        disabled={!actions?.onOpenPassive}
                        onClick={() => actions?.onOpenPassive?.(hop.id)}>
                        {hop.name}
                      </button>
                      {hop.split_ratio != null && (
                        <span className="text-faint-foreground">{ratioLabel(hop.split_ratio, hop.split_inputs)}</span>
                      )}
                    </span>
                  ))}
                </div>
                {cumulativeSplit(sub.drop.chain) != null && (
                  <p className="mt-0.5 text-2xs text-faint-foreground">
                    1:{cumulativeSplit(sub.drop.chain)} total split to this drop
                  </p>
                )}
              </Row>
            ) : (
              <Row label="Drop from">
                <span className="text-faint-foreground">
                  Not recorded. Add it on the splitter's own panel.
                </span>
              </Row>
            )}
          </>
        ) : (
          <p className="py-1 text-xs text-faint-foreground">
            Not resolvable to an OLT{sub.matched ? "" : ". This ONU is in no roster"}.
          </p>
        )}

        <Row label="Location">
          {rec?.lat != null && rec?.lng != null ? (
            <div>
              <span className="font-mono">{rec.lat.toFixed(5)}, {rec.lng.toFixed(5)}</span>
              <p className="mt-0.5 text-2xs text-faint-foreground">
                {rec.place_source === "gps" && rec.accuracy_m != null
                  ? `GPS fix ±${Math.round(rec.accuracy_m)} m`
                  : rec.place_source === "manual" ? "Placed by hand"
                  : "Placed on the map"}
                {rec.placed_by && ` · ${rec.placed_by}`}
                {rec.placed_at && ` · ${ago(rec.placed_at)}`}
              </p>
            </div>
          ) : (
            <span className="text-faint-foreground">
              Not located yet. A fault here has no coordinate to send anyone to.
            </span>
          )}
        </Row>
      </Section>

      {fibre}

      {/* Deliberately NOT inside the frozen block below, and not suppressed when
          the ONU is dark: an address is a fact about the customer's own router,
          not a live measurement of the light, and it is MOST wanted exactly when
          they are down — it is what the RADIUS lookup is keyed on. Every row
          carries its own date instead, so "the OLT still sees this" and "this is
          the last one we saw" can never read alike. */}
      <Section title="Customer equipment">
        <Row label="User MAC">
          <UserMacs macs={sub.user_macs ?? []} status={sub.user_mac_status} />
        </Row>
      </Section>

      <Section title="Right now">
        {!r ? (
          <p className="py-1 text-xs text-faint-foreground">
            No roster row, so there is nothing live to report.
          </p>
        ) : (
          <>
            {frozen && (
              <p className="mb-1 text-xs text-warning">
                Its OLT is down. Readings below are frozen at the last walk.
              </p>
            )}
            <div className={cn(frozen && "wisp-frozen")}>
              <Row label="State">
                <span className={cn("font-medium",
                  dark ? "text-destructive" : "text-success")}>
                  {dark ? `Dark · ${r.state}` : "Online"}
                </span>
                {dark && r.last_online_at && (
                  <span className="text-muted-foreground"> · since {ago(r.last_online_at)}</span>
                )}
              </Row>

              <Row label="Signal">
                {frozen ? (
                  <span className="text-faint-foreground">—</span>
                ) : dark ? (
                  <span className="text-faint-foreground">
                    Not measured while the ONU is dark.
                  </span>
                ) : !opticsFresh ? (
                  <span className="text-faint-foreground">
                    This OLT's optics walk is stale, so there is no current reading.
                  </span>
                ) : r.rx_dbm == null ? (
                  <span className="text-faint-foreground">
                    This OLT reports no per-ONU receive power.
                  </span>
                ) : (
                  <>
                    <span className={cn("font-mono font-semibold",
                      sev === "crit" ? "text-destructive"
                        : sev === "warn" ? "text-warning" : "")}>
                      {r.rx_dbm.toFixed(2)} dBm
                    </span>
                    {sub.thresholds && (
                      <span className="text-2xs text-faint-foreground">
                        {" "}· warn {sub.thresholds.warn_dbm}, crit {sub.thresholds.crit_dbm}
                      </span>
                    )}
                  </>
                )}
              </Row>

              <Row label="Traffic">
                {frozen ? (
                  <span className="text-faint-foreground">—</span>
                ) : !sub.rate ? (
                  <span className="text-faint-foreground">
                    This OLT's firmware publishes no per-ONU interface.
                  </span>
                ) : !isFresh(sub.rate.updated_at) ? (
                  <span className="text-faint-foreground">
                    No recent rate. This OLT's port walk is stale.
                  </span>
                ) : (
                  <span className="font-mono">
                    ↓ {((sub.rate.out_bps ?? 0) / 1e6).toFixed(1)} Mb/s
                    {" · "}↑ {((sub.rate.in_bps ?? 0) / 1e6).toFixed(1)} Mb/s
                    {sub.rate.if_name && (
                      <span className="text-faint-foreground">
                        {" "}· {sub.rate.if_name.split(" ")[0]}
                      </span>
                    )}
                  </span>
                )}
              </Row>

              {r.distance_m != null && r.distance_m > 0 && (
                <Row label="Ranging">
                  <span className="font-mono">{(r.distance_m / 1000).toFixed(2)} km</span>
                  <span className="text-2xs text-faint-foreground"> · optical path</span>
                </Row>
              )}
            </div>
            {sub.olt?.optics_updated_at && (
              <p className="mt-1 text-2xs text-faint-foreground">
                Optics walked {ago(sub.olt.optics_updated_at)}
                {r.name && r.name !== rec?.label && ` · the OLT calls it "${r.name}"`}
              </p>
            )}
          </>
        )}
      </Section>

      {canWrite && (
        <div className="mt-auto flex flex-wrap gap-1 border-t px-2 py-2">
          {actions?.onPlace && (
            <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
              title="Click the map to place this subscriber. Only the pin moves."
              onClick={() => actions.onPlace!(sub.mac, rec?.label || name)}>
              <Crosshair className="size-3" />
              {rec?.lat != null ? "Move" : "Place"}
            </Button>
          )}
          {actions?.onTraceDrop && rec?.lat != null && sub.drop && (
            <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
              title="Click along the drop cable's real path. A traced drop stops being drawn as a dotted straight line."
              onClick={() => actions.onTraceDrop!(sub.mac)}>
              <Spline className="size-3" />
              Trace drop
            </Button>
          )}
          {rec && (
            <ReferenceToggle mac={sub.mac} witness={!!rec.witness} scopeOrg={scopeOrg}
              onDone={() => {
                qc.invalidateQueries({ queryKey: ["subscriber", mac] })
                qc.invalidateQueries({ queryKey: ["onu-places"] })
                qc.invalidateQueries({ queryKey: ["pon-faults"] })
                qc.invalidateQueries({ queryKey: ["pon-summary"] })
              }} />
          )}
          {sub.olt && actions?.onOpenOlt && (
            <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
              onClick={() => actions.onOpenOlt!(sub.olt!.id, sub.mac)}>
              <ExternalLink className="size-3" /> Its OLT
            </Button>
          )}
          {rec?.lat != null && (
            <Button variant="ghost" size="sm"
              className="h-7 flex-1 text-2xs text-muted-foreground hover:text-destructive"
              title={rec.witness
                ? "Takes it off the map and withdraws the power-backed claim. The customer's details are kept."
                : "Takes it off the map. The customer's details are kept."}
              onClick={() => unpin.mutate()} disabled={unpin.isPending}>
              <MapPin className="size-3" /> Unpin
            </Button>
          )}
        </div>
      )}
      {rec && (
        <p className="px-4 pb-2 text-2xs text-faint-foreground">
          Recorded {fmtDateTime(rec.created_at)}
        </p>
      )}
    </div>
  )
}
