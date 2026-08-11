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

export function parseLatLng(raw: string): { lat: number; lng: number } | null {
  const parts = raw.trim().replace(/°/g, "").split(/[,\s]+/).filter(Boolean)
  if (parts.length !== 2) return null
  const lat = Number(parts[0])
  const lng = Number(parts[1])
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null
  if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null
  return { lat, lng }
}

export function ReferenceOnuButton({ o, deviceId }: { o: OnuOptic; deviceId: number }) {
  const { canWrite } = useAuth()
  const [open, setOpen] = useState(false)
  const pinned = o.place != null
  const isRef = o.place?.witness === true
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
            ? "On the map · not a reference point. Click to vouch for its power."
            : "Mark as a power-backed reference point"}
        onClick={() => setOpen(true)}
        disabled={!canWrite && !isRef}
        className={cn(
          "shrink-0 rounded p-0.5 transition-colors",
          isRef ? "text-primary hover:text-primary/80"
            : pinned ? "text-muted-foreground hover:text-foreground"
              : "text-ghost-foreground hover:text-foreground",
        )}
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
    qc.invalidateQueries({ queryKey: ["pon-faults"] })
    qc.invalidateQueries({ queryKey: ["pon-summary"] })
    toast.success(msg)
    onClose()
  }

  const save = useMutation({
    mutationFn: () => inventoryApi.setOnuPlace({
      mac: o.serial!, lat: parsed!.lat, lng: parsed!.lng, label: label.trim() || null,
      phone: phone.trim() || null,
      org_id: scopeOrg,
    }),
    onSuccess: () => {
      if (placed) return done("Location saved")
      qc.invalidateQueries({ queryKey: ["optics", deviceId] })
      qc.invalidateQueries({ queryKey: ["onu-places"] })
      toast.success("Subscriber placed")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save"),
  })

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
            <Input value={label} onChange={(e) => setLabel(e.target.value.toUpperCase())}
              placeholder="WATER TANK, BSNL TOWER, PANCHAYAT OFFICE…"
              disabled={!canWrite} />
          </div>
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
