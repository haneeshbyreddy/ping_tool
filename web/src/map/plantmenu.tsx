// The map's right-click menu: where recording plant, cable and customers starts.
//
// A context menu is the right control here for one reason — the click carries a
// coordinate, and a coordinate is most of what a splitter, a cable vertex or a
// customer pin is. Everything else this menu offers was already reachable; what
// it removes is the trip to another page to type in facts the click already knew.
//
// Two things it must keep:
//
//   * IT IS NOT THE ONLY WAY IN. Context menus are undiscoverable, so the map's
//     control column carries a `+` that arms the same menu on the next click.
//     Nobody should have to be told this exists to find it.
//   * IT NAMES WHAT WILL HAPPEN, BEFORE THE CLICK. It used to do that about an
//     INHERITED FEEDER ("Splitter below SPL-4"), which is gone: placing a box no
//     longer guesses what feeds it, because the answer is the fibre and the
//     fibre is recorded later. What is named now is the ACT — a box is placed
//     and nothing is drawn to it; a cable is traced and joins nothing until a
//     core is pulled into something.
import { useEffect, useRef } from "react"
import { Spline, Split, Trash2, UserPlus, Waypoints } from "lucide-react"
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

/** The operator's own word for each kind of passive, for the one string that has
 *  to name it. `PLANT_LABEL` can't serve: that covers what may be CREATED here
 *  (splitter alone), and a menu opened on a coupler still has to say "coupler".
 *  An unlisted type falls back rather than printing a raw column value. */
const PASSIVE_WORD: Record<string, string> = {
  splitter: "splitter", coupler: "coupler", fdb: "FDB", closure: "closure",
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
  anchor, near, dropOn, width, height, onClose, onPlant, onArm, onCustomer,
  onCable, onDelete, onOpenDevice,
}: {
  anchor: PlantMenuAnchor
  /** nearest placed box, for ORIENTATION only — it is named in the context line
   *  so the operator knows where they clicked, and nothing inherits from it. */
  near: Feeder | null
  /** nearest splitter a customer here would hang off (ground clicks only).
   *
   *  This one IS still an inheritance, and deliberately: a tech standing at a
   *  drop knows both facts in the same instant — where the box is and which
   *  splitter the fibre comes off — whereas the person placing the splitter
   *  itself usually does not yet know which core will feed it. */
  dropOn: Feeder | null
  width: number
  height: number
  onClose: () => void
  /** create HERE, at the clicked coordinate */
  onPlant: (kind: PlantKind) => void
  /** arm the NEXT click — a pin has its own coordinate, so creating at it would
   *  stack two boxes on one point. `passiveId` is for the CUSTOMER kind alone
   *  (see `dropOn`); plant inherits nothing. */
  onArm: (kind: ArmKind, passiveId: number | null) => void
  onCustomer: (passiveId: number | null) => void
  /** start tracing a NEW cable, first vertex here */
  /** `on` is the pin the trace STARTS on, when it starts on one — so the
   *  cable's first end is recorded as that box rather than worked out later
   *  from a coordinate that happens to be near it. */
  onCable: (lat: number, lng: number,
            on?: { id: number; name: string; lat: number; lng: number }) => void
  /** Remove a PASSIVE outright. Offered only for plant, and only on the map,
   *  for the reason plant is CREATED here: a splitter is a box somebody stood
   *  at, and the record of it is a pin. Gear is deliberately not deletable from
   *  this menu — it has an FSM, an outage history and ports, and none of that is
   *  visible from a right-click on a coordinate. That stays on the Network page,
   *  beside the rest of a device's record. */
  onDelete: (d: OrgDevice) => void
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
  const flip = anchor.y > height - 220
  const target = anchor.device
  const passive = target != null && isPassiveType(target.device_type)

  return (
    <div ref={ref}
      style={{ left, top: flip ? undefined : anchor.y + 2, bottom: flip ? height - anchor.y + 2 : undefined, width: MENU_W }}
      className="absolute z-[1003] flex flex-col gap-0.5 rounded-lg border border-border-strong bg-popover/95 p-1 backdrop-blur dark:bg-popover/95">

      {/* The context line: WHERE you clicked, not what it will inherit. */}
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
          {/* On a PIN: creation arms the next click rather than firing here.
              The pin already occupies this coordinate, and two boxes on one
              point is a site cluster nobody meant to make. */}
          <Item icon={<Split className="size-3.5" />}
            label="New splitter"
            hint="then click"
            tone="primary"
            onClick={() => onArm("splitter", null)} />
          {/* A cable, on the other hand, STARTS here — its first vertex is this
              box, which is the commonest thing a trunk does. */}
          <Item icon={<Spline className="size-3.5" />}
            label={<>Trace a cable from <span className="font-medium">{target.name}</span></>}
            onClick={() => (target.lat != null && target.lng != null
              // An UNPLACED box has no coordinate to start a route from, so the
              // trace starts at the click and catches nothing — the same
              // degradation everything else on this map makes for a pin that
              // isn't there.
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
          {/* LAST, and under its own rule, because it is the one item here that
              destroys something. Everything above it adds. */}
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
