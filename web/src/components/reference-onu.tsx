import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"
import { MapPin, MapPinOff } from "lucide-react"
import { inventoryApi, ApiError } from "@/lib/api"
import type { OnuOptic } from "@/lib/types"
import { useAuth } from "@/hooks/use-auth"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

/** Parse a pasted coordinate pair.
 *
 *  A pasted "15.8497, 74.4977" is how this actually gets used: the coordinate
 *  comes off a phone at the site, or out of Google Maps' share sheet, and
 *  retyping it into two boxes is where the digits get transposed. Accepts a
 *  comma or whitespace separator and tolerates a trailing degree sign. Returns
 *  null on anything it isn't sure about — a silently mis-parsed coordinate puts
 *  a reference point in the wrong village. */
export function parseLatLng(raw: string): { lat: number; lng: number } | null {
  const parts = raw.trim().replace(/°/g, "").split(/[,\s]+/).filter(Boolean)
  if (parts.length !== 2) return null
  const lat = Number(parts[0])
  const lng = Number(parts[1])
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null
  if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null
  return { lat, lng }
}

/** Marking an ONU as a reference point, from its row in the Optical tab.
 *
 *  THE CONTRACT IS THE FEATURE. Placing an ONU here is the operator asserting
 *  that this subscriber's power is reliable — nothing detects that, there is no
 *  power field, and the act of placing is the entire signal. A placed ONU that
 *  goes dark is treated as evidence of a fibre cut, and placed ONUs that stay up
 *  while their neighbours drop are treated as evidence of an area power cut. So
 *  a pin dropped here "to complete the map" would quietly corrupt PON-fault
 *  verdicts and send a crew to the wrong place.
 *
 *  That is why this is a dialog with a sentence in it and not a one-click
 *  toggle, and why every string says "reference point", never "location". */
export function ReferenceOnuButton({ o, deviceId }: { o: OnuOptic; deviceId: number }) {
  const { canWrite } = useAuth()
  const [open, setOpen] = useState(false)
  const placed = o.place != null
  // A reference point on an ONU nobody can identify would be unreachable: the
  // placement is keyed on the MAC, so no MAC means no key.
  if (!o.serial) return null
  if (!canWrite && !placed) return null
  return (
    <>
      <button
        type="button"
        aria-label={placed ? "Reference point" : "Mark as reference point"}
        title={placed
          ? `Reference point${o.place?.label ? ` · ${o.place.label}` : ""}`
          : "Mark as a power-backed reference point"}
        onClick={() => setOpen(true)}
        disabled={!canWrite && !placed}
        className={cn(
          "shrink-0 rounded p-0.5 transition-colors",
          placed ? "text-primary hover:text-primary/80"
            : "text-faint-foreground hover:text-foreground",
        )}>
        <MapPin className={cn("size-3.5", placed && "fill-current")} />
      </button>
      {open && (
        <ReferenceOnuDialog o={o} deviceId={deviceId} onClose={() => setOpen(false)} />
      )}
    </>
  )
}

function ReferenceOnuDialog({ o, deviceId, onClose }: {
  o: OnuOptic; deviceId: number; onClose: () => void
}) {
  const { canWrite, scopeOrg } = useAuth()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const placed = o.place ?? null
  const [label, setLabel] = useState(placed?.label ?? o.name ?? "")
  const [coords, setCoords] = useState(
    placed ? `${placed.lat}, ${placed.lng}` : "")
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    setLabel(placed?.label ?? o.name ?? "")
    setCoords(placed ? `${placed.lat}, ${placed.lng}` : "")
  }, [placed, o.name])

  const parsed = parseLatLng(coords)
  const badCoords = touched && coords.trim() !== "" && parsed == null

  const done = (msg: string) => {
    qc.invalidateQueries({ queryKey: ["optics", deviceId] })
    qc.invalidateQueries({ queryKey: ["onu-places"] })
    // A placement changes PON-fault verdicts, so the fault views have to re-ask
    qc.invalidateQueries({ queryKey: ["pon-faults"] })
    qc.invalidateQueries({ queryKey: ["pon-summary"] })
    toast.success(msg)
    onClose()
  }

  // org_id rides every write: a superadmin's own org_id is NULL, so the scope
  // it is viewing is the only thing that says which org owns the point.
  const save = useMutation({
    mutationFn: () => inventoryApi.setOnuPlace({
      mac: o.serial!, lat: parsed!.lat, lng: parsed!.lng, label: label.trim() || null,
      org_id: scopeOrg,
    }),
    onSuccess: () => done(placed ? "Reference point moved" : "Reference point added"),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })

  const remove = useMutation({
    mutationFn: () => inventoryApi.setOnuPlace({
      mac: o.serial!, lat: null, lng: null, org_id: scopeOrg }),
    onSuccess: () => done("Reference point removed"),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't remove"),
  })

  const who = o.name || o.serial

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {placed ? "Reference point" : "Mark as reference point"}
          </DialogTitle>
          <DialogDescription>
            <span className="font-mono text-foreground">{who}</span>
            {o.pon_port && <> · PON {o.pon_port}</>}
            {o.onu_id != null && <> · ONU {o.onu_id}</>}
          </DialogDescription>
        </DialogHeader>

        {/* The contract, stated where the decision is made. Not a tooltip: a
            reader who misses this will place ordinary subscribers and quietly
            poison the power-vs-fibre verdict. */}
        <div className="rounded-lg border border-warning/40 bg-warning-soft/40 px-3 py-2 text-xs">
          <p className="font-semibold text-warning">
            Only place ONUs whose power you can rely on
          </p>
          <p className="mt-0.5 text-muted-foreground">
            A UPS, inverter, solar or tower supply — not a plain mains connection.
            Reference ONUs are how we tell a fibre cut from an area power cut: if
            one goes dark, power can't explain it, so we call it a cut. Placing an
            ordinary subscriber here will send a crew out for a DISCOM outage.
          </p>
        </div>

        <div className="space-y-3">
          <div className="space-y-1">
            <label className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
              Site name
            </label>
            {/* Uppercased AS TYPED, not just on the way into the DB
                (`inventory._onu_label`): the server would silently rewrite what
                this field shows, and a field that disagrees with what was saved
                is how an operator ends up re-typing a name that was already
                right. */}
            <Input value={label} onChange={(e) => setLabel(e.target.value.toUpperCase())}
              placeholder="WATER TANK, BSNL TOWER, PANCHAYAT OFFICE…"
              disabled={!canWrite} />
          </div>
          <div className="space-y-1">
            <label className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
              Coordinates
            </label>
            <Input value={coords} onChange={(e) => { setCoords(e.target.value); setTouched(true) }}
              placeholder="15.8497, 74.4977"
              className={cn("font-mono", badCoords && "border-destructive")}
              disabled={!canWrite} />
            <p className={cn("text-2xs", badCoords ? "text-destructive" : "text-faint-foreground")}>
              {badCoords
                ? "Paste as \"lat, lng\" — two numbers."
                : "Paste from a phone or Google Maps, or pick it on the map."}
            </p>
          </div>
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <div className="flex gap-2">
            {placed && canWrite && (
              <Button variant="outline" size="sm" onClick={() => remove.mutate()}
                disabled={remove.isPending}>
                <MapPinOff className="size-3.5" /> Remove
              </Button>
            )}
            <Button variant="outline" size="sm"
              onClick={() => {
                // Hand the map an armed placement rather than making the
                // operator find this ONU again from the other side.
                navigate("/map", { state: { placeOnu: { mac: o.serial, label: label.trim() || who } } })
                onClose()
              }}>
              <MapPin className="size-3.5" /> Pick on map
            </Button>
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button size="sm" onClick={() => save.mutate()}
              disabled={!canWrite || parsed == null || save.isPending}>
              {placed ? "Save" : "Add reference point"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
