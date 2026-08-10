// THE SUBSCRIBER PANEL — one home for the leaf of the topology.
//
// Every other object in this product has one: a device has `device-detail.tsx`,
// opened identically from the Network tree, the map and an issue row. A
// subscriber had SIX partial views and no home — the map's pin card, the Optical
// tab's row, the Network page's search hit, the map search hit, the splitter
// panel's drop row and the survey's two coverage rows — each carrying a
// different subset of the same facts, none of them complete, none addressable.
// An operator had to know which screen held which fact, which is exactly what
// "customer data is spread across the app" describes.
//
// So this is the sibling of `device-detail.tsx`, not a seventh view: the lists
// stay slim (a list should be slim) and every one of them opens THIS. It renders
// in the map's right rail where the device panel renders, because a subscriber
// is an object of the same weight as a device and the product already has a
// grammar for that.
//
// WHAT IT IS NOT is as load-bearing as what it is. This is a NETWORK tool, and
// the ISPs using it keep saying so. The rule that decides what belongs here:
// store what you need to FIX the fault, never what you need to BILL for the
// service. Name, number, location, serving splitter, ONU, light, rate — every
// one of those is read by somebody during an outage. Tariff, invoices, dues,
// KYC, tickets are not, and they do not come here later.
//
// Section order is FAULT-CALL order, not schema order: who they are and how to
// ring them, where the drop hangs, whether it is working. That is the sequence
// of an actual support call, and it is why contact details lead a panel in a
// network-management product.
import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Crosshair, ExternalLink, MapPin, Pencil, Phone, Shield, ShieldOff, Spline, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { ApiError, inventoryApi } from "@/lib/api"
import type { Subscriber, SubscriberPlantHop } from "@/lib/types"
import { ago, fmtDateTime, isDownState, isFresh, onuName, onuSev } from "@/lib/format"
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

/** Make — or withdraw — the power-supply claim on this subscriber.
 *
 *  The claim is a SEPARATE act from putting them on the map, and until
 *  2026-08-04 it was not: placing WAS the only way to say it and unplacing the
 *  only way to take it back, so an operator could not express "surveyed, but I
 *  am not vouching for their power" — the state a fleet that has surveyed its
 *  drops is almost entirely in. What that cost: every desktop write asserted the
 *  claim by default, and one morning of field captures turned into 30 witnesses,
 *  each of which makes `ponfault` call a dark PON a fibre cut and roll a crew.
 *
 *  It stays a DIALOG with the contract in it and never a bare toggle — the same
 *  rule the Optical tab's reference-point dialog keeps, for the same reason:
 *  nothing detects a reliable power supply, so the act of asserting it is the
 *  whole signal, and a one-click switch is how it gets asserted by accident.
 *  Withdrawing needs no warning block: it only ever makes this product quieter,
 *  and a confirmation stating what is kept is enough. */
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
            // The contract, stated where the decision is made — verbatim in
            // substance with the Optical tab's dialog, because two surfaces
            // making the same claim in different words is how one of them ends
            // up sounding optional.
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

/** The cumulative split down a cascade — 1:4 into 1:8 is 1:32, which is what
 *  says whether a PON has budget left.
 *
 *  Null if ANY passive in the chain has no ratio recorded. A partial product
 *  UNDERSTATES the split, and understating it is how a PON gets over-built —
 *  the same refusal `drops.cumulativeSplit` makes on the map side, kept here
 *  rather than imported so this panel does not pull in the map's module graph. */
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

/** The contact form — the write that could not exist until the record was
 *  decoupled from the pin.
 *
 *  Nothing is required. That is deliberately looser than the FIELD capture,
 *  which demands name, number and location together because a survey row is
 *  only worth the walk if a crew can act on it. This is a desk filling in what
 *  it happens to know, one column at a time, and refusing a name because nobody
 *  has the number yet is how an ISP's other 2,150 subscribers stay unrecorded.
 *
 *  It posts to `onu-contact`, never `onu-place`: that call's meaning is the map
 *  pin, and clearing one retracts a power claim. Typing somebody's name must
 *  never be able to touch either. */
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
      // Every surface that names a subscriber reads one of these.
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
        {/* Uppercased AS TYPED, not only on the way into the DB: the server
            normalizes too, and a field that disagrees with what was saved is how
            a name gets re-typed by somebody who thinks it didn't stick. */}
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

/** The same panel, for surfaces that have no right rail to put it in.
 *
 *  The map opens `SubscriberDetail` directly, in the rail where the device panel
 *  opens, because that is the map's own grammar for "an object is selected".
 *  Everywhere else — a row in the Optical tab, a drop in the splitter panel, a
 *  hit in the Network page's ONU search — the subscriber is being opened FROM a
 *  list inside another panel, and nesting a panel in a panel is how a layout
 *  starts fighting itself. So those get a dialog.
 *
 *  Same component, same query, same actions either way. What must never happen
 *  is a second RENDERING of a subscriber: this file is the only place that
 *  decides how one looks, which is the entire point of the exercise. */
export function SubscriberDialog({ mac, onClose, actions }: {
  mac: string; onClose: () => void; actions?: SubscriberActions
}) {
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="gap-0 overflow-hidden p-0 sm:max-w-md" showCloseButton={false}>
        {/* The panel carries its own identity block, so the dialog's title is
            for screen readers only — a visible second heading would repeat the
            customer's name immediately above itself. */}
        <DialogTitle className="sr-only">Subscriber {mac}</DialogTitle>
        <div className="max-h-[75vh] overflow-y-auto">
          <SubscriberDetail mac={mac} actions={{ ...actions, onClose }} />
        </div>
      </DialogContent>
    </Dialog>
  )
}

export interface SubscriberActions {
  /** Arm map placement for this MAC. Absent off the map, where there is no map
   *  to place onto — the panel then simply doesn't offer it rather than
   *  navigating somewhere the operator didn't ask to go. */
  onPlace?: (mac: string, label: string) => void
  /** Open this subscriber's OLT (Optical tab, this row focused). */
  onOpenOlt?: (deviceId: number, mac: string) => void
  /** Open the splitter its drop comes off. */
  onOpenPassive?: (deviceId: number) => void
  /** Trace the drop cable from that splitter to this customer. Map-only, like
   *  `onPlace` — off the map there is nothing to trace onto, so the panel
   *  simply doesn't offer it rather than navigating somewhere unasked. */
  onTraceDrop?: (mac: string) => void
  onClose?: () => void
}

export function SubscriberDetail({ mac, actions }: {
  mac: string; actions?: SubscriberActions
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
  const name = onuName({ label: rec?.label, name: r?.name, serial: sub.mac })
  // The FROZEN rule: an unreachable OLT proves every reading behind it stopped
  // being a claim about now, up to 15 minutes before the staleness gate would
  // notice. Readings are dropped and the reason is stated OUTSIDE the frozen
  // block — grey with no explanation reads as a broken panel.
  const frozen = !!sub.olt && isDownState(sub.olt.state)
  const opticsFresh = isFresh(sub.olt?.optics_updated_at)
  const dark = !!r && r.state !== "online"

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      {/* ---- identity ---------------------------------------------------- */}
      <div className="flex items-start gap-2.5 border-b px-4 py-3">
        <span className="mt-1"><StatusDot tone={TONE[sev]} /></span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="min-w-0 truncate text-sm font-semibold">
              {name || <span className="text-muted-foreground">Unnamed subscriber</span>}
            </p>
            {/* A REFERENCE ONU is a claim about a power supply that flips a PON
                verdict from "fibre cut" to "area power cut". A plain located
                drop claims nothing but a coordinate. The two must never render
                alike, here least of all. */}
            {rec?.witness && <RowTag tone="success" title="Power-backed reference point. Tells a fibre cut from an area power cut.">reference</RowTag>}
          </div>
          {/* THE MAC IS THE ONU'S, NOT THE CUSTOMER'S, and it is now labelled as
              such. It is the serial the OLT reports — the sticker on the box in
              the house — and printing it bare under a person's name invited it
              to be read as a customer identifier. */}
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

      {/* Identity refusals, before anything that would read as a fact about a
          live drop. Both are reported rather than resolved — see the endpoint. */}
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

      {/* ---- 1. WHO ------------------------------------------------------ */}
      <Section title="Customer"
        action={canWrite && !editing && (
          <Button variant="ghost" size="sm" className="h-6 px-1.5 text-2xs"
            onClick={() => setEditing(true)}>
            <Pencil className="size-3" /> {rec?.label || rec?.phone ? "Edit" : "Add"}
          </Button>
        )}>
        {editing ? (
          <ContactForm sub={sub} onDone={() => setEditing(false)} />
        ) : rec?.label || rec?.phone || rec?.notes ? (
          <div className="space-y-0">
            {rec.label && <Row label="Name">{rec.label}</Row>}
            {/* A real `tel:` link — this panel is read on a phone at the
                roadside as often as on the wall screen, and re-typing ten digits
                off a screen is how a crew rings the wrong house. */}
            {rec.phone && (
              <Row label="Phone">
                <a href={`tel:${rec.phone}`}
                  className="inline-flex items-center gap-1 font-mono underline-offset-2 hover:underline">
                  <Phone className="size-3" />{rec.phone}
                </a>
              </Row>
            )}
            {rec.notes && <Row label="Notes"><span className="text-muted-foreground">{rec.notes}</span></Row>}
          </div>
        ) : (
          // "Nothing recorded" and "nothing to record" are different sentences.
          // This one says which, and offers the way out of it.
          <p className="py-1 text-xs text-muted-foreground">
            No customer details recorded yet.
            {canWrite && " Add a name and number so a fault on this drop reaches somebody."}
          </p>
        )}
      </Section>

      {/* ---- 2. WHERE IT HANGS ------------------------------------------- */}
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
            {/* The plant a crew actually works on. A straight line to the OLT
                was never the network: a customer hangs off the nearest splitter,
                which may hang off another. */}
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

        {/* Location, with its PROVENANCE. A field capture and a hand-placed pin
            are different claims about the same two numbers, and a splitter
            pinned 40 m off is a crew walking the wrong side of a road. */}
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

      {/* ---- 3. IS IT WORKING -------------------------------------------- */}
      <Section title="Right now">
        {!r ? (
          <p className="py-1 text-xs text-faint-foreground">
            No roster row, so there is nothing live to report.
          </p>
        ) : (
          <>
            {/* Stated OUTSIDE the frozen block, always paired with it. */}
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

              {/* THE THREE REFUSALS, each a documented way this product has
                  rendered a lie. A NULL reading, a stale walk and a dark ONU all
                  produce a blank Rx column, and they take opposite actions — so
                  the panel says WHICH it is rather than printing a dash. */}
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

              {/* Its OWN interface, never the PON's — that row is the aggregate
                  of up to 64 subscribers, and printing it would put one big
                  number on every drop. Three sentences, never one blank cell. */}
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

      {/* ---- 4. ACTIONS --------------------------------------------------- */}
      {canWrite && (
        <div className="mt-auto flex flex-wrap gap-1 border-t px-2 py-2">
          {actions?.onPlace && (
            <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
              // Says what it does NOT do, because it used to do it: this button
              // forced the witness flag on, so nudging a surveyed pin promoted an
              // ordinary customer to evidence of a fibre cut. It now only moves
              // the pin, and the claim has its own button beside it.
              title="Click the map to place this subscriber. Only the pin moves."
              onClick={() => actions.onPlace!(sub.mac, rec?.label || name)}>
              <Crosshair className="size-3" />
              {rec?.lat != null ? "Move" : "Place"}
            </Button>
          )}
          {/* Tracing the last hop. Gated on BOTH ends existing, because a
              route needs two anchors to rubber-band between: the customer's own
              pin, and the splitter its drop is recorded against. Without the
              splitter the map draws to the OLT instead, and that line is an
              admitted guess — tracing it would promote "we only know the PON"
              into surveyed geometry a crew orders drum against. The server
              refuses it too; this is the half that explains why. */}
          {actions?.onTraceDrop && rec?.lat != null && sub.drop && (
            <Button variant="ghost" size="sm" className="h-7 flex-1 text-2xs"
              title="Click along the drop cable's real path. A traced drop stops being drawn as a dotted straight line."
              onClick={() => actions.onTraceDrop!(sub.mac)}>
              <Spline className="size-3" />
              Trace drop
            </Button>
          )}
          {/* The claim, as its own verb. Offered whenever there is a record to
              attach it to — deliberately NOT gated on a pin, because
              `ponfault._witness_verdict` matches by MAC and never reads lat/lng,
              so an operator can vouch for a customer nobody has stood at yet. */}
          {rec && (
            <ReferenceToggle mac={sub.mac} witness={!!rec.witness} scopeOrg={scopeOrg}
              onDone={() => {
                qc.invalidateQueries({ queryKey: ["subscriber", mac] })
                qc.invalidateQueries({ queryKey: ["onu-places"] })
                // The claim is an input to the PON verdict, so anything holding
                // one has to re-ask.
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
            // "Remove from map", NOT "Remove". This button used to run a DELETE
            // and take the customer's name and phone number with it, behind an
            // eye-off icon that read as "hide this pin". The wording and the
            // toast both now say what actually happens.
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
