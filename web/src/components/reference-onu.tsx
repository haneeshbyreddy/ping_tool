import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"
import { MapPin, MapPinOff, MapPinned } from "lucide-react"
import { inventoryApi, ApiError } from "@/lib/api"
import type { OnuOptic } from "@/lib/types"
import { useAuth } from "@/hooks/use-auth"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
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

/** A subscriber's location and details, from its row in the Optical tab — and
 *  the explicit toggle that makes it a REFERENCE POINT.
 *
 *  THE CONTRACT IS THE FEATURE. Vouching for an ONU is the operator asserting
 *  that this subscriber's power is reliable — nothing detects that, there is no
 *  power field, and the assertion is the entire signal. A vouched-for ONU that
 *  goes dark is treated as evidence of a fibre cut, and ones that stay up while
 *  their neighbours drop are treated as evidence of an area power cut. So a
 *  claim made "to complete the map" quietly corrupts PON-fault verdicts and
 *  sends a crew to the wrong place.
 *
 *  **PLACING IS NOT THE CLAIM ANY MORE, and this is the second time that rule
 *  was rewritten** (2026-08-04, operator's call). It began as "placing IS the
 *  claim", which worked while a dozen pins existed and broke the moment a fleet
 *  surveyed its drops: `o.place != null` meant every located customer rendered
 *  as a filled primary "Reference point", and the Save an operator pressed to
 *  fix a phone number asserted a power claim on them. The first fix made the
 *  flag explicit in the payload; the operator went further and took it out of
 *  the location route entirely. Recording where somebody lives is now the same
 *  act from this desk as from the handset, and the claim is a switch you flip.
 *
 *  Three states, and they must stay three: no record · on the map, not vouched
 *  for · reference point. */
export function ReferenceOnuButton({ o, deviceId }: { o: OnuOptic; deviceId: number }) {
  const { canWrite } = useAuth()
  const [open, setOpen] = useState(false)
  const pinned = o.place != null
  const isRef = o.place?.witness === true
  // A reference point on an ONU nobody can identify would be unreachable: the
  // placement is keyed on the MAC, so no MAC means no key.
  if (!o.serial) return null
  if (!canWrite && !isRef) return null
  return (
    <>
      <button
        type="button"
        aria-label={isRef ? "Reference point" : "Mark as reference point"}
        title={isRef
          ? `Reference point${o.place?.label ? ` · ${o.place.label}` : ""}`
          : pinned
            // Says what IS true of this subscriber before offering the claim, so
            // the muted pin can't read as "not on the map".
            ? "On the map · not a reference point. Click to vouch for its power."
            : "Mark as a power-backed reference point"}
        onClick={() => setOpen(true)}
        disabled={!canWrite && !isRef}
        className={cn(
          "shrink-0 rounded p-0.5 transition-colors",
          isRef ? "text-primary hover:text-primary/80"
            // A located drop earns a readable pin: it IS on the map, and
            // rendering it near-identically to an unrecorded subscriber hid the
            // survey's own output on the tab that lists it.
            : pinned ? "text-muted-foreground hover:text-foreground"
              // The quietest step in the scale, deliberately below `faint`:
              // this is the majority row on an unsurveyed OLT and none of them
              // is news. Widening the gap DOWNWARD is half of what makes
              // "located" legible.
              : "text-ghost-foreground hover:text-foreground",
        )}
        // FILL carries "we have a location", GLYPH + HUE carry the claim.
        //
        // All three states used to ride one hollow outline separated by a single
        // ink step (`faint` #868a93 vs `muted` #a8abb2 in dark) — a difference
        // that measured real and read as nothing at 14px, so an operator could
        // not tell a surveyed customer from an unrecorded one on the very tab
        // the survey feeds. Solid-vs-hollow is a SHAPE difference and survives
        // any size; `MapPinned`'s rings then separate the reference point from
        // an ordinary located drop on a second, independent channel, so the two
        // are never only a hue apart (which is also what keeps them apart for a
        // colour-blind reader).
        >
        {isRef
          ? <MapPinned className="size-3.5 fill-current" />
          : <MapPin className={cn("size-3.5", pinned && "fill-current")} />}
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
  // Two different questions, and conflating them is the bug this dialog had:
  // `placed` = there is a pin to prefill; `isRef` = the power claim has been
  // made. A surveyed customer is the first without the second.
  const isRef = placed?.witness === true
  const [label, setLabel] = useState(placed?.label ?? o.name ?? "")
  const [phone, setPhone] = useState(placed?.phone ?? "")
  const [coords, setCoords] = useState(
    placed ? `${placed.lat}, ${placed.lng}` : "")
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    setLabel(placed?.label ?? o.name ?? "")
    setPhone(placed?.phone ?? "")
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
  //
  // SAVE WRITES A LOCATION AND NOTHING ELSE, and the api can no longer spell
  // anything else. Adding a customer's coordinates from the desk is the same act
  // as recording them from the handset — the power claim is the toggle below,
  // pressed on purpose (operator's call, 2026-08-04).
  const save = useMutation({
    mutationFn: () => inventoryApi.setOnuPlace({
      mac: o.serial!, lat: parsed!.lat, lng: parsed!.lng, label: label.trim() || null,
      phone: phone.trim() || null,
      org_id: scopeOrg,
    }),
    // A FIRST placement leaves the dialog OPEN, and that is the whole reason
    // the two acts can live in one dialog without the claim riding on Save: the
    // toggle below is disabled until a record exists, so closing here would mean
    // "save, reopen, flip" for the one flow that actually wants both. The
    // refreshed optics row lands through the invalidation and enables the
    // switch in place. A save on an ALREADY-placed subscriber closes as before —
    // there is nothing new to reach.
    onSuccess: () => {
      if (placed) return done("Location saved")
      qc.invalidateQueries({ queryKey: ["optics", deviceId] })
      qc.invalidateQueries({ queryKey: ["onu-places"] })
      toast.success("Subscriber placed")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })

  // THE CLAIM, as its own act — immediate, not folded into Save. It applies on
  // the click rather than on submit so that "I am vouching for this customer's
  // power" can never ride along with an edit to somebody's phone number, which
  // is exactly how a morning of survey work became a fleet of witnesses.
  const claim = useMutation({
    mutationFn: (next: boolean) => inventoryApi.setOnuWitness({
      mac: o.serial!, witness: next, org_id: scopeOrg }),
    onSuccess: (_r, next) => {
      qc.invalidateQueries({ queryKey: ["optics", deviceId] })
      qc.invalidateQueries({ queryKey: ["onu-places"] })
      qc.invalidateQueries({ queryKey: ["pon-faults"] })
      qc.invalidateQueries({ queryKey: ["pon-summary"] })
      toast.success(next
        ? "Marked as a reference point"
        : "No longer a reference point. Pin and details kept.")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })

  const remove = useMutation({
    mutationFn: () => inventoryApi.setOnuPlace({
      mac: o.serial!, lat: null, lng: null, org_id: scopeOrg }),
    onSuccess: () => done("Taken off the map. Customer details kept."),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't remove"),
  })

  const who = o.name || o.serial

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Subscriber location</DialogTitle>
          <DialogDescription>
            <span className="font-mono text-foreground">{who}</span>
            {o.pon_port && <> · PON {o.pon_port}</>}
            {o.onu_id != null && <> · ONU {o.onu_id}</>}
          </DialogDescription>
        </DialogHeader>

        {/* THE CLAIM IS A TOGGLE, and it is the only thing in this product that
            can make one. Saving coordinates here does NOT — recording where a
            customer lives is the same act from the desk as from the handset
            (operator's call, 2026-08-04).

            The contract stays stated where the decision is made, and stays a
            paragraph rather than a tooltip: a reader who misses it will vouch
            for ordinary subscribers and quietly poison the power-vs-fibre
            verdict. It shows while the toggle is OFF — that is the moment it is
            being decided — and stands down once the claim has been made, where
            it would only be a warning about something already done. */}
        <div className={cn("rounded-lg border px-3 py-2",
          isRef ? "border-primary/40 bg-primary/5" : "border-warning/40 bg-warning-soft/40")}>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className={cn("text-xs font-semibold",
                isRef ? "text-primary" : "text-warning")}>
                Reference point
              </p>
              <p className="mt-0.5 text-2xs text-muted-foreground">
                {isRef
                  ? "Its power is vouched for. If it goes dark we treat that as evidence of a fibre cut."
                  : "Only for a UPS, inverter, solar or tower supply. Never plain mains."}
              </p>
            </div>
            <Switch checked={isRef} disabled={!canWrite || !placed || claim.isPending}
              onCheckedChange={(v) => claim.mutate(v)}
              aria-label="Reference point" />
          </div>
          {!isRef && (
            <p className="mt-1.5 text-2xs text-muted-foreground">
              If one goes dark, power can't explain it, so we call it a cut.
              Vouching for an ordinary subscriber sends a crew out for a DISCOM
              power cut.
            </p>
          )}
          {/* The claim is keyed on the MAC and stored on the subscriber's
              record, so there has to BE one. Saying so beats a toggle that
              silently does nothing. */}
          {!placed && (
            <p className="mt-1.5 text-2xs text-faint-foreground">
              Save a location first, then you can vouch for them.
            </p>
          )}
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
          {/* OPTIONAL here, unlike the field survey, which requires it. What
              this dialog writes is the power-supply CLAIM — the thing a PON
              mass-drop verdict reads — and refusing it because nobody has the
              customer's number would trade a fibre-cut/power-cut call for a
              paperwork field. Blank leaves a recorded number alone (the server
              COALESCEs), so a desktop edit can't quietly wipe what the field
              captured. */}
          <div className="space-y-1">
            <label className="text-2xs font-semibold tracking-wide text-muted-foreground uppercase">
              Phone number
            </label>
            <Input value={phone} onChange={(e) => setPhone(e.target.value)}
              placeholder="9876543210 (optional)"
              type="tel" disabled={!canWrite} />
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
                ? "Paste as \"lat, lng\"."
                : "Paste from a phone or Google Maps, or pick it on the map."}
            </p>
          </div>
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <div className="flex flex-wrap gap-2">
            {placed && canWrite && (
              <Button variant="outline" size="sm" onClick={() => remove.mutate()}
                disabled={remove.isPending}
                title="Takes this customer off the map. Their details are kept.">
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
              {placed ? "Save" : "Place subscriber"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
