import { useEffect, useMemo, useState } from "react"
import { MapContainer, Marker, useMap } from "react-leaflet"
import L from "leaflet"
import { useQuery } from "@tanstack/react-query"
import { Crosshair } from "lucide-react"
import { orgsApi } from "@/lib/api"
import { useDarkMode } from "@/hooks/use-dark-mode"
import {
  AttributionPrefix, GOOGLE_BASEMAPS, GoogleLayer, StreetsTiles, type Basemap,
} from "@/map/basemaps"
import { Button } from "@/components/ui/button"

// The capture sheet's "is this actually the right spot?" panel.
//
// A GPS fix is a circle, not a point: 25 m of it is a whole compound, and the
// tech standing there can SEE which rooftop the box is on. Letting them nudge
// the pin turns an honest-but-vague reading into an exact one — this is the only
// way a worker on a phone can produce a better position than the handset's chip,
// and before it existed their only choice was to accept whatever came back.
//
// SATELLITE by default, and that is the whole point: a roadmap shows a street
// name, imagery shows the actual building, pole line and compound wall. Nobody
// identifies a drop from a road label.
//
// Deliberately NOT the map page in miniature — no clustering, no topology, no
// device pins. One marker, one job. Anything else here would be a second map
// implementation to keep in step with the first.

const ADJUST_ZOOM = 18

/** The pin being positioned. Bigger and louder than anything on the main map:
 *  it is the only object here and it is being aimed, not scanned. */
const PIN = L.divIcon({
  className: "",
  html: '<div class="wisp-adjust-pin"><span></span></div>',
  iconSize: [28, 28],
  iconAnchor: [14, 14],
})

/** Keeps the view on the point when the FIX moves (GPS converging), but never
 *  fights the operator: once they have dragged, the map is theirs. A `useMap()`
 *  child rather than a ref on MapContainer — the ref isn't set yet on the first
 *  render that has coordinates. */
function Recenter({ lat, lng, follow }: { lat: number; lng: number; follow: boolean }) {
  const map = useMap()
  useEffect(() => {
    if (follow) map.setView([lat, lng], map.getZoom() || ADJUST_ZOOM, { animate: false })
  }, [map, lat, lng, follow])
  // A map created inside a sheet that animates open measures itself at the wrong
  // size and renders one grey tile. Re-measure after the transition settles.
  useEffect(() => {
    const t = setTimeout(() => map.invalidateSize(), 260)
    return () => clearTimeout(t)
  }, [map])
  return null
}

export function PinAdjustMap({ org, lat, lng, adjusted, onAdjust, onReset }: {
  org: string | null | undefined
  lat: number
  lng: number
  /** true once the operator has moved it — drives the follow/leave-alone rule
   *  and the "reset" affordance. */
  adjusted: boolean
  onAdjust: (lat: number, lng: number) => void
  onReset: () => void
}) {
  const dark = useDarkMode()
  const [basemap, setBasemap] = useState<Basemap>("gsat")
  const [googleDown, setGoogleDown] = useState(false)

  const orgsQ = useQuery({
    queryKey: ["orgs", org],
    queryFn: () => orgsApi.list(org),
    enabled: !!org,
    staleTime: 60_000,
  })
  const googleKey = orgsQ.data?.orgs.find((o) => o.org_id === org)?.google_maps_key?.trim() || null
  const googleActive = !!googleKey && !googleDown

  const center = useMemo<[number, number]>(() => [lat, lng], [lat, lng])

  return (
    <div className="flex flex-col gap-2">
      <div className="wisp-map-wrap relative h-52 overflow-hidden rounded-xl border">
        <MapContainer
          center={center}
          zoom={ADJUST_ZOOM}
          zoomControl={false}
          // MUST stay on. Two reasons, and the first is a crash: `GoogleLayer`'s
          // ToS attribution swaps lines through `map.attributionControl`, which
          // is UNDEFINED when this is false — so a mini-map with it off threw
          // "undefined is not an object" the moment it rendered, but ONLY for an
          // org that has a Google Maps key (a keyless org falls back to
          // StreetsTiles, whose attribution is a static prop). The second is
          // that showing Google's tiles without their attribution is a terms
          // violation regardless of how small the map is.
          attributionControl
          // `wisp-map` is what styles the attribution box to the app's surfaces
          // — without it this map showed a bare white browser-default chip.
          className="wisp-map h-full w-full"
        >
          <AttributionPrefix />
          {googleActive
            ? <GoogleLayer
                apiKey={googleKey!}
                mapType={GOOGLE_BASEMAPS[basemap]}
                dark={dark}
                onFail={() => setGoogleDown(true)} />
            : <StreetsTiles dark={dark} />}
          <Recenter lat={lat} lng={lng} follow={!adjusted} />
          <Marker
            position={center}
            icon={PIN}
            draggable
            autoPan
            eventHandlers={{
              dragend: (e) => {
                const p = (e.target as L.Marker).getLatLng()
                onAdjust(p.lat, p.lng)
              },
            }}
          />
        </MapContainer>

        {/* Imagery/roadmap toggle. Imagery is the default and the reason this
            panel works, but a bare field with no landmarks is easier to place
            against a road. */}
        {googleActive && (
          <button
            type="button"
            onClick={() => setBasemap((b) => (b === "gsat" ? "google" : "gsat"))}
            className="absolute top-2 right-2 z-[1000] rounded-md border bg-popover/95 px-2 py-1 text-2xs font-medium dark:bg-popover/95"
          >
            {basemap === "gsat" ? "Map" : "Satellite"}
          </button>
        )}

        {/* Google ToS: the wordmark has to be visible wherever their tiles are,
            and this map is no exception for being small. Same treatment as the
            main map — white with a shadow, which is how Google renders it over
            both roadmap and imagery. */}
        {googleActive && (
          <span aria-hidden className="pointer-events-none absolute bottom-0.5 left-1.5 z-[1000] select-none font-medium"
            style={{
              fontFamily: "'Product Sans', Roboto, Arial, sans-serif", fontSize: "14px",
              color: "#fff", textShadow: "0 0 4px rgba(0,0,0,.55), 0 1px 2px rgba(0,0,0,.55)",
            }}>
            Google
          </span>
        )}
      </div>

      <div className="flex items-center justify-between gap-2">
        <p className="text-2xs text-faint-foreground">
          {adjusted
            ? "Pin moved by hand — saved as an exact spot, not a GPS reading."
            : "Drag the pin if the fix is off the mark."}
        </p>
        {adjusted && (
          <Button
            size="sm" variant="ghost" className="h-8 shrink-0 text-2xs"
            onClick={onReset}
          >
            <Crosshair className="size-3.5" /> Back to GPS
          </Button>
        )}
      </div>
    </div>
  )
}
