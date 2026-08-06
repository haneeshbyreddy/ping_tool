// The map's right-click menu: where recording plant and customers now starts.
//
// A context menu is the right control here for one reason — the click carries a
// coordinate, and a coordinate is most of what a splitter or a customer pin is.
// Everything else this menu offers was already reachable; what it removes is the
// trip to another page to type in facts the click already knew.
//
// Two things it must keep:
//
//   * IT IS NOT THE ONLY WAY IN. Context menus are undiscoverable, so the map's
//     control column carries a `+` that arms the same menu on the next click.
//     Nobody should have to be told this exists to find it.
//   * IT NAMES WHAT IT WILL INHERIT, BEFORE THE CLICK. "Splitter below SPL-4"
//     rather than "New splitter" is the whole prefill contract: the operator
//     reads the feeder in the item they are about to press, so a wrong guess is
//     caught here, at zero cost, rather than in a branch-fault verdict months
//     later.
import { useEffect, useRef } from "react"
import { Split, UserPlus, Waypoints } from "lucide-react"
import type { OrgDevice } from "@/lib/types"
import { isPassiveType } from "@/lib/types"
import { fmtKm } from "@/map/geometry"
import { type Feeder, type PlantKind } from "@/map/plant"
import { cn } from "@/lib/utils"

/** What the next map click will record, once an item on a PIN has armed it.
 *
 *  A customer rides the same union as the plant kinds because from the map's
 *  point of view they are one gesture — "click where it goes" — even though they
 *  land in different tables. Keeping them apart would mean two arming states
 *  that could both be live at once, and a click that had to guess which. */
export type ArmKind = PlantKind | "customer"

/** Where the menu opened, in container px and in world coordinates. `device` is
 *  the pin it was opened ON, when it was opened on one. */
export interface PlantMenuAnchor {
  x: number
  y: number
  lat: number
  lng: number
  device: OrgDevice | null
}

const meters = (m: number) => m < 1000 ? `${Math.round(m)} m` : fmtKm(m / 1000)

function Item({ icon, label, hint, tone, onClick }: {
  icon: React.ReactNode
  label: React.ReactNode
  hint?: string
  tone?: "primary"
  onClick: () => void
}) {
  return (
    <button type="button" onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-xs hover:bg-foreground/5",
        tone === "primary" && "text-primary")}>
      <span className="flex size-4 shrink-0 items-center justify-center text-muted-foreground">
        {icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {hint && <span className="shrink-0 text-2xs text-faint-foreground">{hint}</span>}
    </button>
  )
}

export function PlantMenu({
  anchor, feeder, dropOn, width, height, onClose, onPlant, onArm, onCustomer, onOpenDevice,
}: {
  anchor: PlantMenuAnchor
  /** nearest likely feeder to the click (ground clicks only) */
  feeder: Feeder | null
  /** nearest splitter a customer here would hang off (ground clicks only) */
  dropOn: Feeder | null
  width: number
  height: number
  onClose: () => void
  /** create HERE, at the clicked coordinate */
  onPlant: (kind: PlantKind, parentId: number | null) => void
  /** arm the NEXT click to record something below this box — a pin has its own
   *  coordinate, so creating at it would stack two boxes on one point */
  onArm: (kind: ArmKind, parentId: number) => void
  onCustomer: (passiveId: number | null) => void
  onOpenDevice: (d: OrgDevice) => void
}) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    window.addEventListener("keydown", onKey)
    // capture: Leaflet stops propagation on its own container, so a bubbling
    // listener never hears the click that should dismiss this
    window.addEventListener("mousedown", onDown, true)
    return () => {
      window.removeEventListener("keydown", onKey)
      window.removeEventListener("mousedown", onDown, true)
    }
  }, [onClose])

  const MENU_W = 232
  // Clamp inside the map, and flip above the cursor near the bottom edge — a
  // menu that opens off-screen reads as a click that did nothing.
  const left = Math.min(Math.max(4, anchor.x), Math.max(4, width - MENU_W - 4))
  const flip = anchor.y > height - 200
  const target = anchor.device
  const passive = target != null && isPassiveType(target.device_type)
  const olt = target != null && (target.device_type ?? "").toUpperCase() === "OLT"

  return (
    <div ref={ref}
      style={{ left, top: flip ? undefined : anchor.y + 2, bottom: flip ? height - anchor.y + 2 : undefined, width: MENU_W }}
      className="absolute z-[1003] flex flex-col gap-0.5 rounded-lg border border-border-strong bg-popover/95 p-1 backdrop-blur dark:bg-popover/95">

      {/* The context line. It is what makes the prefill honest: the operator
          sees which box is about to be recorded as the feeder, and how far away
          it is, before pressing anything. */}
      <p className="px-2 pt-1 pb-1 text-2xs text-muted-foreground">
        {target ? (
          <span className="block truncate font-medium text-foreground">{target.name}</span>
        ) : feeder ? (
          <>
            Near <span className="text-foreground">{feeder.device.name}</span>
            {" · "}{meters(feeder.meters)}
            {feeder.device.pon_port && <> · PON {feeder.device.pon_port}</>}
          </>
        ) : (
          <>Nothing placed within reach</>
        )}
      </p>

      {target ? (
        <>
          {/* On a PIN: creation arms the next click rather than firing here.
              The pin already occupies this coordinate, and two boxes on one
              point is a site cluster nobody meant to make. */}
          {(passive || olt) && (
            <Item icon={<Split className="size-3.5" />}
              label={<>Splitter below <span className="font-medium">{target.name}</span></>}
              hint="then click"
              tone="primary"
              onClick={() => onArm("splitter", target.id)} />
          )}
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
        </>
      ) : (
        <>
          <Item icon={<Split className="size-3.5" />}
            label={feeder
              ? <>Splitter below <span className="font-medium">{feeder.device.name}</span></>
              : "New splitter here"}
            tone="primary"
            onClick={() => onPlant("splitter", feeder?.device.id ?? null)} />
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
