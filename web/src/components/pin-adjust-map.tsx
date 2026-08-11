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

const ADJUST_ZOOM = 18

const PIN = L.divIcon({
  className: "",
  html: '<div class="wisp-adjust-pin"><span></span></div>',
  iconSize: [28, 28],
  iconAnchor: [14, 14],
})

function Recenter({ lat, lng, follow }: { lat: number; lng: number; follow: boolean }) {
  const map = useMap()
  useEffect(() => {
    if (follow) map.setView([lat, lng], map.getZoom() || ADJUST_ZOOM, { animate: false })
  }, [map, lat, lng, follow])
  useEffect(() => {
    const t = setTimeout(() => map.invalidateSize(), 260)
    return () => clearTimeout(t)
  }, [map])
  return null
}

export function PinAdjustMap({ org, lat, lng, adjusted, moved, onAdjust, onReset }: {
  org: string | null | undefined
  lat: number
  lng: number
  adjusted: boolean
  moved?: boolean
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
          attributionControl
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

        {googleActive && (
          <button
            type="button"
            onClick={() => setBasemap((b) => (b === "gsat" ? "google" : "gsat"))}
            className="absolute top-2 right-2 z-[1000] rounded-md border bg-popover/95 px-2 py-1 text-2xs font-medium dark:bg-popover/95"
          >
            {basemap === "gsat" ? "Map" : "Satellite"}
          </button>
        )}

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
          {(moved ?? adjusted)
            ? "Pin moved by hand. Saved as an exact spot, not a GPS reading."
            : adjusted
              ? "Where this was recorded. Drag it to move it, or leave it alone."
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
