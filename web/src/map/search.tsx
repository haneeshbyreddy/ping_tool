// Map search: instant device match + OSM Nominatim geocoding (browser-side,
// debounced 450ms + 3-char floor — stay a polite keyless client; results are
// boxed to the org's map area).
import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { MapPin, Search } from "lucide-react"
import type { OrgDevice } from "@/lib/types"
import { RowTag } from "@/components/device-detail"
import { StatusDot } from "@/components/status-badge"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { isPlaced, pinTone } from "@/map/pins"

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

export interface PlaceHit { label: string; lat: number; lng: number }

// OSM Nominatim, browser-side (CORS-open, keyless — same trust model as the tile
// CDN). Debounced + min 3 chars keeps us a polite interactive client; results are
// boxed to the org's Settings map area so "Kondapur" finds yours, not Kolkata's.
async function geocode(q: string, bounds: [number, number, number, number] | null): Promise<PlaceHit[]> {
  const params = new URLSearchParams({ q, format: "jsonv2", limit: "6" })
  if (bounds) {
    const [s, w, n, e] = bounds
    params.set("viewbox", `${w},${n},${e},${s}`)
    params.set("bounded", "1")
  }
  const res = await fetch(`https://nominatim.openstreetmap.org/search?${params.toString()}`)
  if (!res.ok) throw new Error(`geocoder replied ${res.status}`)
  const rows = (await res.json()) as Array<{ display_name: string; lat: string; lon: string }>
  return rows
    .map((r) => ({ label: r.display_name, lat: Number(r.lat), lng: Number(r.lon) }))
    .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lng))
}

export function MapSearch({ devices, bounds, onDevice, onPlace }: {
  devices: OrgDevice[]
  bounds: [number, number, number, number] | null
  onDevice: (d: OrgDevice) => void
  onPlace: (p: PlaceHit) => void
}) {
  const [q, setQ] = useState("")
  const [open, setOpen] = useState(false)
  const needle = q.trim().toLowerCase()
  const debounced = useDebounced(q.trim(), 450)

  const deviceHits = needle
    ? devices.filter((d) =>
        d.name.toLowerCase().includes(needle) || d.ip_address.includes(needle)).slice(0, 6)
    : []
  const places = useQuery({
    queryKey: ["geocode", debounced, bounds?.join(",") ?? "world"],
    queryFn: () => geocode(debounced, bounds),
    enabled: open && debounced.length >= 3,
    staleTime: 5 * 60_000,
    retry: 0,
  })
  const placeHits = debounced.length >= 3 ? places.data ?? [] : []

  const pick = (fn: () => void) => { fn(); setQ(""); setOpen(false) }
  const first = () => {
    if (deviceHits.length > 0) pick(() => onDevice(deviceHits[0]))
    else if (placeHits.length > 0) pick(() => onPlace(placeHits[0]))
  }

  return (
    <div className="pointer-events-auto relative w-56 md:w-72">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={q}
        placeholder="Find a device or place…"
        className="h-8 bg-popover/95 dark:bg-popover/95 pl-8 text-xs backdrop-blur"
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === "Enter") first()
          if (e.key === "Escape") { setQ(""); setOpen(false); (e.target as HTMLInputElement).blur() }
        }}
      />
      {open && needle && (
        <Card className="absolute top-9 right-0 left-0 z-[1001] flex max-h-80 flex-col gap-0 overflow-y-auto bg-popover py-0">
          {deviceHits.map((d) => (
            <button key={d.id}
              className="flex h-9 w-full shrink-0 items-center gap-2 border-b px-3 text-left hover:bg-foreground/5"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onDevice(d))}>
              <StatusDot tone={pinTone(d)} />
              <span className="min-w-0 truncate font-mono text-xs font-medium">{d.name}</span>
              {!isPlaced(d) && <RowTag tone="muted">not placed</RowTag>}
              <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">{d.ip_address}</span>
            </button>
          ))}
          {placeHits.map((p, i) => (
            <button key={`${p.lat},${p.lng},${i}`}
              className="flex h-9 w-full shrink-0 items-center gap-2 border-b px-3 text-left last:border-b-0 hover:bg-foreground/5"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onPlace(p))}>
              <MapPin className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate text-xs">{p.label}</span>
            </button>
          ))}
          {deviceHits.length === 0 && placeHits.length === 0 && (
            <p className="px-3 py-2.5 text-xs text-muted-foreground">
              {debounced.length < 3 ? "No matching devices. Type 3+ letters to search places too."
                : places.isFetching ? "Searching places…"
                : places.isError ? "No matching devices; place search is unreachable."
                : "Nothing found on the map or in your devices."}
            </p>
          )}
        </Card>
      )}
    </div>
  )
}
