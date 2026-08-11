import { useEffect, useRef } from "react"
import { Scissors, Spline, Split, Trash2, UserPlus, Waypoints } from "lucide-react"
import type { OrgDevice } from "@/lib/types"
import { isPassiveType } from "@/lib/types"
import { fmtKm } from "@/map/geometry"
import { type Feeder, type PlantKind } from "@/map/plant"
import { cn } from "@/lib/utils"

export type ArmKind = PlantKind | "customer"

export interface PlantMenuAnchor {
  x: number
  y: number
  lat: number
  lng: number
  device: OrgDevice | null
}

const meters = (m: number) => m < 1000 ? `${Math.round(m)} m` : fmtKm(m / 1000)

const PASSIVE_WORD: Record<string, string> = {
  splitter: "splitter", fdb: "FDB", closure: "closure", coupler: "closure",
}

function Item({ icon, label, hint, tone, onClick }: {
  icon: React.ReactNode
  label: React.ReactNode
  hint?: string
  tone?: "primary" | "destructive"
  onClick: () => void
}) {
  return (
    <button type="button" onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-xs hover:bg-foreground/5",
        tone === "primary" && "text-primary",
        tone === "destructive" && "text-destructive")}>
      <span className={cn(
        "flex size-4 shrink-0 items-center justify-center",
        tone === "destructive" ? "text-destructive" : "text-muted-foreground")}>
        {icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {hint && <span className="shrink-0 text-2xs text-faint-foreground">{hint}</span>}
    </button>
  )
}

export function PlantMenu({
  anchor, near, dropOn, cut, width, height, onClose, onPlant, onArm, onCustomer,
  onCable, onCut, onDelete, onOpenDevice,
}: {
  anchor: PlantMenuAnchor
  near: Feeder | null
  dropOn: Feeder | null
  width: number
  height: number
  onClose: () => void
  onPlant: (kind: PlantKind) => void
  onArm: (kind: ArmKind, passiveId: number | null) => void
  onCustomer: (passiveId: number | null) => void
  onCable: (lat: number, lng: number,
            on?: { id: number; name: string; lat: number; lng: number }) => void
  onDelete: (d: OrgDevice) => void
  onOpenDevice: (d: OrgDevice) => void
  cut: { cableId: number; name: string; meters: number } | null
  onCut: (cableId: number, name: string) => void
}) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    window.addEventListener("keydown", onKey)
    window.addEventListener("mousedown", onDown, true)
    return () => {
      window.removeEventListener("keydown", onKey)
      window.removeEventListener("mousedown", onDown, true)
    }
  }, [onClose])

  const MENU_W = 268
  const left = Math.min(Math.max(4, anchor.x), Math.max(4, width - MENU_W - 4))
  const flip = anchor.y > height - 220
  const target = anchor.device
  const passive = target != null && isPassiveType(target.device_type)

  return (
    <div ref={ref}
      style={{ left, top: flip ? undefined : anchor.y + 2, bottom: flip ? height - anchor.y + 2 : undefined, width: MENU_W }}
      className="absolute z-[1003] flex flex-col gap-0.5 rounded-lg border border-border-strong bg-popover/95 p-1 backdrop-blur dark:bg-popover/95">

      <p className="px-2 pt-1 pb-1 text-2xs text-muted-foreground">
        {target ? (
          <span className="block truncate font-medium text-foreground">{target.name}</span>
        ) : near ? (
          <>
            Near <span className="text-foreground">{near.device.name}</span>
            {" · "}{meters(near.meters)}
          </>
        ) : (
          <>Nothing placed within reach</>
        )}
      </p>

      {target ? (
        <>
          <Item icon={<Split className="size-3.5" />}
            label="New splitter"
            hint="then click"
            tone="primary"
            onClick={() => onArm("splitter", null)} />
          <Item icon={<Spline className="size-3.5" />}
            label={<>Trace a cable from <span className="font-medium">{target.name}</span></>}
            onClick={() => (target.lat != null && target.lng != null
              ? onCable(target.lat, target.lng, {
                  id: target.id, name: target.name,
                  lat: target.lat, lng: target.lng })
              : onCable(anchor.lat, anchor.lng))} />
          {passive && (
            <Item icon={<UserPlus className="size-3.5" />}
              label="Customer on this splitter"
              hint="then click"
              onClick={() => onArm("customer", target.id)} />
          )}
          <div className="my-0.5 border-t" />
          <Item icon={<Waypoints className="size-3.5" />}
            label="Open panel"
            onClick={() => onOpenDevice(target)} />
          {passive && (
            <Item icon={<Trash2 className="size-3.5" />}
              label={`Delete ${PASSIVE_WORD[target.device_type ?? ""] ?? "this box"}`}
              tone="destructive"
              onClick={() => onDelete(target)} />
          )}
        </>
      ) : (
        <>
          <Item icon={<Split className="size-3.5" />}
            label="New splitter here"
            tone="primary"
            onClick={() => onPlant("splitter")} />
          <Item icon={<Spline className="size-3.5" />}
            label="Trace a cable from here"
            onClick={() => onCable(anchor.lat, anchor.lng)} />
          {cut && (
            <Item icon={<Scissors className="size-3.5" />}
              label="Open a closure"
              hint={cut.name}
              onClick={() => onCut(cut.cableId, cut.name)} />
          )}
          <div className="my-0.5 border-t" />
          <Item icon={<UserPlus className="size-3.5" />}
            label="Customer here"
            hint={dropOn ? `on ${dropOn.device.name}` : undefined}
            onClick={() => onCustomer(dropOn?.device.id ?? null)} />
        </>
      )}
    </div>
  )
}
