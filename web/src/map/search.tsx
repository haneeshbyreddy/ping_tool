import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { MapPin, PanelRight, Search, Users } from "lucide-react"
import type { OnuPlace, OrgDevice } from "@/lib/types"
import { useDebounced } from "@/hooks/use-debounced"
import { RowTag } from "@/components/device-detail"
import { Chip, StatusDot } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { inventoryApi } from "@/lib/api"
import { onuName, onuSearchKey } from "@/lib/format"
import { isPlaced, pinTone } from "@/map/pins"
import { isRefDark, refTitle } from "@/map/refonu"

export interface PlaceHit { label: string; lat: number; lng: number }

export interface OnuHit {
  mac: string
  who: string
  where: string
  place: OnuPlace | null
}

const ONU_MIN = 3

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

export function MapSearch({ devices, org, bounds, onDevice, onOnu, onPlace }: {
  devices: OrgDevice[]
  org: string | null | undefined
  bounds: [number, number, number, number] | null
  // `openPanel` is the row's own button: picking the row goes to the device and filters the
  // map to its customers and plant, the button opens its panel.
  onDevice: (d: OrgDevice, openPanel?: boolean) => void
  onOnu: (hit: OnuHit) => void
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

  const onuKey = onuSearchKey(q)
  const onuOn = open && !!org && onuKey.length >= ONU_MIN
  const placesQ = useQuery({
    queryKey: ["onu-places", org],
    queryFn: () => inventoryApi.onuPlaces(org),
    enabled: onuOn,
    staleTime: 60_000,
  })
  const rosterQ = useQuery({
    queryKey: ["onu-search", org, debounced],
    queryFn: () => inventoryApi.onuSearch(org, debounced),
    enabled: onuOn && onuSearchKey(debounced).length >= ONU_MIN,
    staleTime: 60_000,
    retry: 0,
  })
  const onuHits = useMemo(() => {
    if (!onuOn) return []
    const byMac = new Map<string, OnuHit>()
    for (const p of placesQ.data?.places ?? []) {
      if (!(onuSearchKey(p.mac).includes(onuKey)
            || onuSearchKey(p.label).includes(onuKey)
            || onuSearchKey(p.name).includes(onuKey))) continue
      byMac.set(p.mac, {
        mac: p.mac,
        who: onuName({ label: p.label, name: p.name, serial: p.mac }),
        where: `${p.device_name ?? "no live slot"}${p.pon_port ? ` · ${p.pon_port}` : ""}`,
        place: p,
      })
    }
    for (const m of rosterQ.data?.matches ?? []) {
      for (const o of m.onus) {
        const mac = (o.serial ?? "").trim().toUpperCase()
        if (!mac || byMac.has(mac)) continue
        byMac.set(mac, {
          mac,
          who: onuName(o),
          where: `${m.device_name}${o.pon_port ? ` · ${o.pon_port}` : ""}`,
          place: null,
        })
      }
    }
    return [...byMac.values()]
      .sort((a, b) => Number(!!b.place) - Number(!!a.place))
      .slice(0, 6)
  }, [onuOn, onuKey, placesQ.data, rosterQ.data])

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
    else if (onuHits.length > 0) pick(() => onOnu(onuHits[0]))
    else if (placeHits.length > 0) pick(() => onPlace(placeHits[0]))
  }

  return (
    <div className="pointer-events-auto relative w-56 md:w-72">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={q}
        placeholder="Find a device, subscriber or place…"
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
            // A div, not a button: the row carries a button of its own and a nested one is
            // invalid markup. Keyboard reach is the Enter handler, as on the site card.
            <div key={d.id} role="button" tabIndex={0}
              className="flex h-9 w-full shrink-0 cursor-pointer items-center gap-2 border-b px-3 text-left last:border-b-0 hover:bg-foreground/5"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onDevice(d))}
              onKeyDown={(e) => { if (e.key === "Enter") pick(() => onDevice(d)) }}>
              <StatusDot tone={pinTone(d)} />
              <span className="min-w-0 truncate font-mono text-xs font-medium">{d.name}</span>
              {!isPlaced(d) && <RowTag tone="muted">not placed</RowTag>}
              <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">{d.ip_address}</span>
              {isPlaced(d) && (
                <Button variant="ghost" size="icon" className="-mr-1.5 size-6 shrink-0 text-muted-foreground"
                  title={`Open ${d.name}'s panel`}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={(e) => { e.stopPropagation(); pick(() => onDevice(d, true)) }}>
                  <PanelRight className="size-3.5" />
                </Button>
              )}
            </div>
          ))}
          {onuHits.map((h) => (
            <button key={`onu:${h.mac}`}
              className="flex w-full shrink-0 items-center gap-2 border-b px-3 py-1.5 text-left last:border-b-0 hover:bg-foreground/5"
              title={h.place ? refTitle(h.place) : `${h.who} · ${h.where} · no location recorded yet`}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(() => onOnu(h))}>
              {h.place
                ? <StatusDot tone={!h.place.matched ? "muted"
                    : isRefDark(h.place) ? "destructive" : "success"} />
                : <Users className="size-3.5 shrink-0 text-muted-foreground" />}
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5">
                  <span className="min-w-0 truncate text-xs font-medium">{h.who}</span>
                  {!h.place && <RowTag tone="muted">no pin</RowTag>}
                  {h.place?.witness ? <Chip tone="info">reference</Chip> : null}
                </span>
                <span className="block truncate font-mono text-2xs text-muted-foreground">
                  {h.where}
                </span>
              </span>
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
          {deviceHits.length === 0 && onuHits.length === 0 && placeHits.length === 0 && (
            <p className="px-3 py-2.5 text-xs text-muted-foreground">
              {debounced.length < 3
                ? "No matching devices. Type 3+ letters to search subscribers and places too."
                : (places.isFetching || rosterQ.isFetching) ? "Searching…"
                : places.isError ? "No device or subscriber matches; place search is unreachable."
                : "Nothing found. No device, subscriber or place matches."}
            </p>
          )}
        </Card>
      )}
    </div>
  )
}
