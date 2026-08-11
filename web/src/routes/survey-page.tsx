import { useEffect, useMemo, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  Check, ChevronRight, Crosshair, MapPin, Navigation, Plus, Search, Signal,
  TriangleAlert, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useIsMobile } from "@/hooks/use-mobile"
import { useGpsFix, GOOD_FIX_M, type GpsFix } from "@/hooks/use-gps-fix"
import { usePonOptions } from "@/hooks/use-pon-options"
import { inventoryApi, ApiError } from "@/lib/api"
import {
  isPassiveType,
  type OnuCoverageLocatedRow, type OnuCoverageRow, type OnuPlace, type OrgDevice,
} from "@/lib/types"
import {
  feederOptions, nearestFeeder, oltHead, ponFor, ponOptions, suggestPlantName,
} from "@/map/plant"
import { NeedsOrg } from "@/components/needs-org"
import { ShiftButton } from "@/components/field-tracking-card"
import { PinAdjustMap } from "@/components/pin-adjust-map"
import { SplitRatioField, type SplitRatio } from "@/components/split-ratio-field"
import { Chip, StatusDot, type Tone } from "@/components/status-badge"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"

const NO_FEEDER = "__nofeeder__"
const NO_PON = "__nopon__"
const PLANT_KIND = "splitter" as const

function fixTone(accuracy: number | null): Tone {
  if (accuracy == null) return "muted"
  if (accuracy <= GOOD_FIX_M) return "success"
  if (accuracy <= 60) return "warning"
  return "destructive"
}

function fixLabel(accuracy: number | null): string {
  if (accuracy == null) return "accuracy unknown"
  return `±${Math.round(accuracy)} m`
}

function placedNote(d: OrgDevice): string {
  if (d.lat == null) return "not placed"
  const who = d.placed_by ? ` by ${d.placed_by}` : ""
  const when = d.placed_at ? ` ${ago(d.placed_at)}` : ""
  if (d.place_source === "gps") return `GPS ${fixLabel(d.accuracy_m)}${who}${when}`
  if (d.place_source === "manual") return `set by hand${who}${when}`
  return "placed on the desktop"
}

type Target =
  | { kind: "device"; device: OrgDevice }
  | { kind: "passive" }
  | {
      kind: "onu"; mac: string; who: string; where: string; located: boolean
      label: string | null
      phone: string | null
      walked: string | null
      at: { lat: number; lng: number } | null
    }

const ONU_MIN_CHARS = 3

const normMac = (raw: string | null | undefined): string => (raw ?? "").trim().toUpperCase()

const phoneOk = (raw: string): boolean =>
  /^\+?\d{7,15}$/.test(raw.replace(/[\s\-().]/g, ""))

export function SurveyPage() {
  const { scopeOrg, user } = useAuth()
  const isMobile = useIsMobile()
  const isWorker = !!user && !user.is_superadmin && user.role === "worker"
  const qc = useQueryClient()
  const [search, setSearch] = useState("")
  const [target, setTarget] = useState<Target | null>(null)

  const devices = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })

  const [onuNeedle, setOnuNeedle] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setOnuNeedle(search.trim()), 350)
    return () => clearTimeout(t)
  }, [search])

  const onus = useQuery({
    queryKey: ["onu-search", scopeOrg, onuNeedle],
    queryFn: () => inventoryApi.onuSearch(scopeOrg, onuNeedle),
    enabled: !!scopeOrg && onuNeedle.length >= ONU_MIN_CHARS,
  })

  const places = useQuery({
    queryKey: ["onu-places", scopeOrg],
    queryFn: () => inventoryApi.onuPlaces(scopeOrg),
    enabled: !!scopeOrg,
  })
  const placedMacs = useMemo(() => {
    const m = new Map<string, OnuPlace>()
    for (const p of places.data?.places ?? []) m.set(p.mac, p)
    return m
  }, [places.data])

  const all = useMemo(() => devices.data?.devices ?? [], [devices.data])

  const unplaced = useMemo(
    () => all.filter((d) => d.lat == null || d.lng == null),
    [all])

  const mine = useMemo(() => {
    const today = new Date(); today.setHours(0, 0, 0, 0)
    return all
      .filter((d) => d.placed_by === user?.username && d.placed_at &&
                     new Date(d.placed_at.replace(" ", "T") + "Z") >= today)
      .sort((a, b) => (b.placed_at ?? "").localeCompare(a.placed_at ?? ""))
  }, [all, user])

  const needle = search.trim().toLowerCase()
  const results = useMemo(() => {
    if (!needle) return unplaced
    return all.filter((d) => d.name.toLowerCase().includes(needle) ||
                             (d.ip_address ?? "").includes(needle))
  }, [all, unplaced, needle])

  const onuHits = useMemo(() => {
    const out: {
      mac: string; who: string; where: string; located: boolean; witness: boolean
      label: string | null; phone: string | null; walked: string | null
      at: { lat: number; lng: number } | null
    }[] = []
    for (const m of onus.data?.matches ?? []) {
      for (const o of m.onus) {
        const mac = normMac(o.serial)
        if (!mac) continue
        const p = placedMacs.get(mac)
        out.push({
          mac,
          who: p?.label || o.name || o.serial || mac,
          where: `${m.device_name}${o.pon_port ? ` · PON ${o.pon_port}` : ""}`,
          label: p?.label ?? null,
          phone: p?.phone ?? null,
          walked: o.name ?? null,
          at: p ? { lat: p.lat, lng: p.lng } : null,
          located: !!p,
          witness: p?.witness === true,
        })
      }
    }
    return out
  }, [onus.data, placedMacs])

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["inventory", scopeOrg] })
    void qc.invalidateQueries({ queryKey: ["onu-places", scopeOrg] })
    void qc.invalidateQueries({ queryKey: ["onu-coverage", scopeOrg] })
  }

  if (!scopeOrg) return <NeedsOrg />

  const gearLeft = unplaced.length

  const fieldOnly = isWorker && isMobile

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 px-4 py-4">
      <header className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Survey</h1>
          <p className="text-2xs text-faint-foreground">
            Record where equipment physically stands. Connections stay with the owner.
          </p>
        </div>
        {devices.isSuccess && (
          <span className={cn(
            "shrink-0 rounded-full border px-2.5 py-1 text-xs font-semibold tabular-nums",
            gearLeft === 0 ? "bg-card text-faint-foreground" : "bg-card")}>
            {gearLeft === 0 ? "gear done" : `${gearLeft} gear left`}
          </span>
        )}
      </header>

      <ShiftButton className="items-start" />

      <div className="relative">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search equipment or a subscriber MAC…"
          className="h-11 pl-9"
          autoCapitalize="none"
          autoCorrect="off"
        />
      </div>

      {devices.isLoading && <Skeleton className="h-40 w-full rounded-xl" />}
      {devices.isError && (
        <p className="text-sm text-destructive">
          {devices.error instanceof ApiError ? devices.error.message : "Failed to load devices"}
        </p>
      )}

      {devices.isSuccess && (
        <section className="wisp-panel">
          <div className="wisp-panel-head">
            <span className="wisp-eyebrow">
              {needle ? "Matches" : "Not yet placed"}
            </span>
            <span className="text-2xs text-faint-foreground tabular-nums">{results.length}</span>
          </div>
          {results.length === 0 && (
            <div className="flex items-center gap-3 px-4 py-6 text-sm text-muted-foreground">
              <StatusDot tone="success" />
              {needle ? "Nothing matches that." : "Everything has a location."}
            </div>
          )}
          {results.map((d) => (
            <button
              key={d.id}
              type="button"
              onClick={() => setTarget({ kind: "device", device: d })}
              className="wisp-row flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-foreground/5"
            >
              <MapPin className={cn("size-4 shrink-0",
                d.lat == null ? "text-faint-foreground" : "text-success")} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{d.name}</span>
                <span className="block truncate text-2xs text-faint-foreground">
                  {d.device_type ?? "device"}
                  {d.ip_address ? ` · ${d.ip_address}` : ""}
                  {d.lat != null ? ` · ${placedNote(d)}` : ""}
                </span>
              </span>
            </button>
          ))}
        </section>
      )}

      {!needle && <SubscriberCoverage org={scopeOrg} onPick={setTarget}
                                      expandable={!fieldOnly} />}

      {needle.length >= ONU_MIN_CHARS && (
        <section className="wisp-panel">
          <div className="wisp-panel-head">
            <span className="wisp-eyebrow">Subscribers</span>
            <span className="text-2xs text-faint-foreground tabular-nums">
              {onus.isFetching ? "…" : onuHits.length}
            </span>
          </div>
          {onus.isFetching && onuHits.length === 0 && (
            <div className="px-4 py-6"><Skeleton className="h-4 w-40" /></div>
          )}
          {!onus.isFetching && onuHits.length === 0 && (
            <div className="px-4 py-6 text-sm text-muted-foreground">
              No ONU matches that MAC or name.
            </div>
          )}
          {onuHits.map((o) => (
            <div key={o.mac} className={cn("wisp-row flex items-center gap-1 pr-2",
              o.located && "bg-success/[0.18] dark:bg-success/[0.11]")}>
              <button
                type="button"
                onClick={() => setTarget({
                  kind: "onu", mac: o.mac, who: o.who, where: o.where,
                  located: o.located, label: o.label, phone: o.phone,
                  walked: o.walked, at: o.at,
                })}
                className={cn("flex min-w-0 flex-1 items-center gap-3 px-4 py-3 text-left",
                  o.located ? "hover:bg-success/15" : "hover:bg-foreground/5")}
              >
                {o.located
                  ? <Check className="size-4 shrink-0 text-success" />
                  : <MapPin className="size-4 shrink-0 text-faint-foreground" />}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-sm font-medium">{o.who}</span>
                  <span className="block truncate text-2xs text-faint-foreground">{o.where}</span>
                </span>
                {o.witness && <Chip tone="info">reference</Chip>}
              </button>
              {o.located && !fieldOnly && (
                <Button asChild size="icon" variant="ghost" className="size-9 shrink-0"
                        title="Show on map">
                  <Link to={`/map?onu=${encodeURIComponent(o.mac)}`}>
                    <Navigation className="size-4" />
                  </Link>
                </Button>
              )}
            </div>
          ))}
          {onus.data?.truncated && (
            <p className="px-4 py-2.5 text-2xs text-faint-foreground">
              Showing the first matches only. Type more of the MAC.
            </p>
          )}
        </section>
      )}

      {mine.length > 0 && (
        <section className="wisp-panel">
          <div className="wisp-panel-head">
            <span className="wisp-eyebrow">Placed today</span>
            <span className="text-2xs text-faint-foreground tabular-nums">{mine.length}</span>
          </div>
          {mine.map((d) => (
            <button
              key={d.id}
              type="button"
              onClick={() => setTarget({ kind: "device", device: d })}
              className="wisp-row flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-foreground/5"
            >
              <Check className="size-4 shrink-0 text-success" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{d.name}</span>
                <span className="block truncate text-2xs text-faint-foreground">{placedNote(d)}</span>
              </span>
              <Chip tone={fixTone(d.accuracy_m)}>{fixLabel(d.accuracy_m)}</Chip>
            </button>
          ))}
        </section>
      )}

      {!fieldOnly && (
        <div className="sticky bottom-0 -mx-4 mt-auto border-t bg-background/95 px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] backdrop-blur">
          <Button
            className="h-12 w-full text-sm"
            variant="outline"
            onClick={() => setTarget({ kind: "passive" })}
          >
            <Plus className="size-4" /> New splitter here
          </Button>
        </div>
      )}

      <CaptureSheet
        target={target}
        onClose={() => setTarget(null)}
        onDone={invalidate}
        placed={all.filter((d) => d.lat != null && d.lng != null)}
        devices={all}
        fieldOnly={fieldOnly}
        org={scopeOrg}
      />
    </div>
  )
}

function SubscriberCoverage({ org, onPick, expandable = true }: {
  org: string | null | undefined
  onPick: (t: Target) => void
  expandable?: boolean
}) {
  const [openOlt, setOpenOlt] = useState<number | null>(null)

  const coverage = useQuery({
    queryKey: ["onu-coverage", org],
    queryFn: () => inventoryApi.onuCoverage(org),
    enabled: !!org,
  })
  const drill = useQuery({
    queryKey: ["onu-coverage", org, openOlt],
    queryFn: () => inventoryApi.onuCoverage(org, openOlt!),
    enabled: !!org && expandable && openOlt != null,
  })

  const located: OnuCoverageLocatedRow[] = drill.data?.located ?? []
  const unplaced: OnuCoverageRow[] = drill.data?.unplaced ?? []

  const c = coverage.data
  if (coverage.isLoading) return <Skeleton className="h-32 w-full rounded-xl" />
  if (!c || c.total === 0) return null

  const pct = Math.round((c.placed / c.total) * 100)

  return (
    <section className="wisp-panel">
      <div className="wisp-panel-head">
        <span className="wisp-eyebrow">Subscribers</span>
        <span className="text-2xs text-faint-foreground tabular-nums">
          {c.placed} of {c.total} located
        </span>
      </div>

      <div className="px-4 py-3">
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-success transition-[width]"
               style={{ width: `${Math.max(pct, c.placed > 0 ? 1 : 0)}%` }} />
        </div>
        <p className="mt-2 text-2xs text-faint-foreground">
          {c.placed === 0
            ? expandable
              ? "No subscriber locations recorded yet. Pick an OLT to start."
              : "No subscriber locations recorded yet. Search a MAC or a customer name to start."
            : expandable
              ? `${pct}% recorded · pick an OLT to continue`
              : `${pct}% recorded · search a MAC or a customer name to add one`}
        </p>
      </div>

      {c.olts.map((o) => {
        const left = o.total - o.placed
        const open = expandable && openOlt === o.device_id
        const Row = expandable ? "button" : "div"
        return (
          <div key={o.device_id} className="wisp-row">
            <Row
              {...(expandable
                ? { type: "button" as const,
                    onClick: () => setOpenOlt(open ? null : o.device_id) }
                : {})}
              className={cn("flex w-full items-center gap-3 px-4 py-3 text-left",
                expandable && "hover:bg-foreground/5")}
            >
              {expandable && (
                <ChevronRight className={cn("size-4 shrink-0 text-faint-foreground transition-transform",
                  open && "rotate-90")} />
              )}
              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                {o.device_name ?? `OLT ${o.device_id}`}
              </span>
              <span className="shrink-0 text-2xs text-faint-foreground tabular-nums">
                {left === 0 ? "all located" : `${o.placed}/${o.total}`}
              </span>
            </Row>

            {open && (
              <div className="border-t bg-muted/30">
                {drill.isLoading && <div className="px-4 py-3"><Skeleton className="h-4 w-32" /></div>}

                {located.length > 0 && (
                  <div className="flex items-center justify-between gap-2 border-t border-border-subtle bg-success/10 px-4 py-1.5 dark:bg-success/[0.07]">
                    <span className="wisp-eyebrow">Located</span>
                    <span className="text-2xs text-faint-foreground tabular-nums">
                      {located.length}
                    </span>
                  </div>
                )}
                {located.map((l) => (
                  <button
                    key={l.mac}
                    type="button"
                    onClick={() => onPick({
                      kind: "onu", mac: l.mac,
                      who: l.label || l.name || l.mac,
                      where: `${l.device_name ?? ""}${l.pon_port ? ` · PON ${l.pon_port}` : ""}`,
                      located: true, label: l.label, phone: l.phone,
                      walked: l.name ?? null,
                      at: l.lat != null && l.lng != null
                        ? { lat: l.lat, lng: l.lng } : null,
                    })}
                    className="flex w-full items-center gap-3 border-t border-border-subtle bg-success/[0.18] px-4 py-2.5 text-left hover:bg-success/25 dark:bg-success/[0.11] dark:hover:bg-success/[0.17]"
                  >
                    <Check className="size-3.5 shrink-0 text-success" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-xs">
                        {l.label || l.name || l.mac}
                      </span>
                      <span className="block truncate text-2xs text-faint-foreground">
                        <span className="font-mono">{l.mac}</span>
                        {l.placed_at ? ` · ${ago(l.placed_at)}` : ""}
                        {l.placed_by ? ` by ${l.placed_by}` : ""}
                      </span>
                    </span>
                    {l.witness && <Chip tone="info">reference</Chip>}
                    <span className="shrink-0 text-2xs text-faint-foreground">
                      {l.pon_port ?? ""}
                    </span>
                  </button>
                ))}

                {drill.isSuccess && unplaced.length === 0 && (
                  <p className="border-t border-border-subtle px-4 py-3 text-2xs text-muted-foreground">
                    Every subscriber on this OLT has a location.
                  </p>
                )}
                {located.length > 0 && unplaced.length > 0 && (
                  <div className="flex items-center justify-between gap-2 border-t border-border-subtle px-4 py-1.5">
                    <span className="wisp-eyebrow">Still to visit</span>
                    <span className="text-2xs text-faint-foreground tabular-nums">
                      {unplaced.length}
                    </span>
                  </div>
                )}
                {unplaced.map((u) => (
                  <button
                    key={u.mac}
                    type="button"
                    onClick={() => onPick({
                      kind: "onu", mac: u.mac,
                      who: u.name || u.mac,
                      where: `${u.device_name ?? ""}${u.pon_port ? ` · PON ${u.pon_port}` : ""}`,
                      located: false, label: null, phone: null,
                      walked: u.name ?? null, at: null,
                    })}
                    className="flex w-full items-center gap-3 border-t border-border-subtle px-4 py-2.5 text-left hover:bg-foreground/5"
                  >
                    <MapPin className="size-3.5 shrink-0 text-faint-foreground" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-xs">
                        {u.name || u.mac}
                      </span>
                      {u.name && (
                        <span className="block truncate font-mono text-2xs text-faint-foreground">
                          {u.mac}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 text-2xs text-faint-foreground">
                      {u.pon_port ?? ""}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </section>
  )
}

function CaptureSheet({ target, onClose, onDone, placed, devices, fieldOnly, org }: {
  target: Target | null
  onClose: () => void
  onDone: () => void
  placed: OrgDevice[]
  devices: OrgDevice[]
  fieldOnly: boolean
  org: string | null | undefined
}) {
  const navigate = useNavigate()
  const { canWrite } = useAuth()
  const { fix, phase, error, start, reset } = useGpsFix()
  const [name, setName] = useState("")
  const [phone, setPhone] = useState("")
  const [split, setSplit] = useState<SplitRatio>({ ratio: null, inputs: null })
  const [feederId, setFeederId] = useState<number | null>(null)
  const [feederTouched, setFeederTouched] = useState(false)
  const [pon, setPon] = useState("")
  const [ponTouched, setPonTouched] = useState(false)
  const [sameAs, setSameAs] = useState<OrgDevice | null>(null)
  const [pin, setPin] = useState<{ lat: number; lng: number } | null>(null)
  const [moved, setMoved] = useState(false)
  const [mapOpen, setMapOpen] = useState(false)

  const open = target != null
  const isPassive = target?.kind === "passive"

  const openChange = (next: boolean) => {
    if (!next) { reset(); onClose(); return }
  }
  const onOpenAutoFocus = () => {
    reset(); setSameAs(null)
    setSplit({ ratio: target?.kind === "passive" ? 8 : null, inputs: null })
    setFeederId(null); setFeederTouched(false)
    setPon(""); setPonTouched(false)
    setName(target?.kind === "onu" ? (target.label ?? "")
      : target?.kind === "passive" ? suggestPlantName(PLANT_KIND, devices) : "")
    setPhone(target?.kind === "onu" ? (target.phone ?? "") : "")
    const seeded = target?.kind === "onu" && target.at ? target.at : null
    setPin(seeded)
    setMoved(false)
    setMapOpen(seeded != null)
    start()
  }

  const placeDevice = useMutation({
    mutationFn: (body: { id: number; lat: number; lng: number; accuracy_m: number | null; source: "gps" | "manual" }) =>
      inventoryApi.placeInField(body),
    onSuccess: () => { toast.success("Location saved"); onDone(); reset(); onClose() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save the location"),
  })

  const createPassive = useMutation({
    mutationFn: async ({ body, parentId, pon, region }: {
      body: Parameters<typeof inventoryApi.createFieldPassive>[0]
      parentId: number | null
      pon: string | null
      region: string | null
    }) => {
      const { id } = await inventoryApi.createFieldPassive(body)
      if (parentId == null) return { id, wired: false }
      try {
        await inventoryApi.update(id, {
          name: body.name, ip_address: "", device_type: body.device_type,
          region, tags: [], parent_device_id: parentId,
          pon_port: pon, split_ratio: body.split_ratio ? Number(body.split_ratio) : null,
          split_inputs: body.split_inputs ?? null,
        })
        return { id, wired: true }
      } catch {
        toast.warning(`${body.name} was recorded, but its feeder wasn't saved`, {
          description: "Set it from the map or the Network page.",
        })
        return { id, wired: false }
      }
    },
    onSuccess: () => { toast.success("Plant recorded"); onDone(); reset(); onClose() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't record this"),
  })

  const locateOnu = useMutation({
    mutationFn: (body: Parameters<typeof inventoryApi.locateOnuInField>[0]) =>
      inventoryApi.locateOnuInField(body),
    onSuccess: (_r, body) => {
      toast.success("Subscriber located", fieldOnly ? undefined : {
        action: {
          label: "View on map",
          onClick: () => navigate(`/map?onu=${encodeURIComponent(body.mac)}`),
        },
      })
      onDone(); reset(); onClose()
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save the location"),
  })

  const nameOnu = useMutation({
    mutationFn: (body: { mac: string; label: string; phone: string }) =>
      inventoryApi.nameOnuInField(body),
    onSuccess: () => { toast.success("Details saved"); onDone(); reset(); onClose() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't save the details"),
  })

  const busy = placeDevice.isPending || createPassive.isPending ||
    locateOnu.isPending || nameOnu.isPending

  const coords: { lat: number; lng: number; accuracy: number | null; source: "gps" | "manual" } | null =
    pin
      ? { lat: pin.lat, lng: pin.lng, accuracy: null, source: "manual" }
      : sameAs && sameAs.lat != null && sameAs.lng != null
        ? { lat: sameAs.lat, lng: sameAs.lng, accuracy: null, source: "manual" }
        : fix
          ? { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy, source: "gps" }
          : null

  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices])
  const suggestedFeeder = useMemo(
    () => (isPassive && canWrite && coords
      ? nearestFeeder(coords.lat, coords.lng, devices) : null),
    [isPassive, canWrite, coords, devices])
  const effectiveFeederId = feederTouched ? feederId : suggestedFeeder?.device.id ?? null
  const feeder = effectiveFeederId != null ? byId.get(effectiveFeederId) ?? null : null
  const feederPon = ponFor(feeder, byId)
  const ponOlt = oltHead(feeder, byId)
  const { pons, loading: ponsLoading } = usePonOptions(ponOlt?.id, isPassive && canWrite)
  const effectivePon = ponTouched ? pon : (feederPon.inherited ? feederPon.pon ?? "" : "")
  const feederChoices = useMemo(
    () => (isPassive && canWrite && coords
      ? feederOptions(coords.lat, coords.lng, devices).slice(0, 25) : []),
    [isPassive, canWrite, coords, devices])

  const renameOnly = target?.kind === "onu" && target.located && pin != null && !moved

  const good = renameOnly ||
    (coords != null && (coords.accuracy == null || coords.accuracy <= GOOD_FIX_M))

  const onuDetailsOk = name.trim().length > 0 && phoneOk(phone)
  const detailsChanged = target?.kind === "onu" &&
    (name.trim() !== (target.label ?? "") || phone.trim() !== (target.phone ?? ""))

  const canSave = !busy && (
    renameOnly ? onuDetailsOk && detailsChanged
      : target?.kind === "onu" ? onuDetailsOk && coords != null
        : coords != null && (!isPassive || name.trim().length > 0))

  const save = () => {
    if (renameOnly && target?.kind === "onu") {
      nameOnu.mutate({ mac: target.mac, label: name.trim(), phone: phone.trim() })
      return
    }
    if (!coords) return
    if (isPassive) {
      createPassive.mutate({
        body: {
          name: name.trim(), device_type: PLANT_KIND,
          lat: coords.lat, lng: coords.lng,
          accuracy_m: coords.accuracy, source: coords.source,
          split_ratio: split.ratio ? String(split.ratio) : null,
          split_inputs: split.inputs,
          region: feeder?.region ?? null,
          pon_port: effectivePon || null,
        },
        parentId: effectiveFeederId,
        pon: effectivePon || null,
        region: feeder?.region ?? null,
      })
    } else if (target?.kind === "device") {
      placeDevice.mutate({
        id: target.device.id, lat: coords.lat, lng: coords.lng,
        accuracy_m: coords.accuracy, source: coords.source,
      })
    } else if (target?.kind === "onu") {
      locateOnu.mutate({
        mac: target.mac, lat: coords.lat, lng: coords.lng,
        accuracy_m: coords.accuracy, source: coords.source,
        label: name.trim(), phone: phone.trim(),
      })
    }
  }

  const title = isPassive ? "New splitter here"
    : target?.kind === "device" ? target.device.name
      : target?.kind === "onu" ? target.who : ""
  const replacing =
    (target?.kind === "device" && target.device.lat != null) ||
    (target?.kind === "onu" && target.located)

  return (
    <Sheet open={open} onOpenChange={openChange}>
      <SheetContent
        side="bottom"
        onOpenAutoFocus={onOpenAutoFocus}
        className="max-h-[92svh] gap-0 overflow-y-auto rounded-t-2xl pb-[calc(1rem+env(safe-area-inset-bottom))]"
      >
        <SheetHeader className="px-4 pt-4 pb-2">
          <SheetTitle className="truncate text-base">{title}</SheetTitle>
          {target?.kind === "device" && (
            <p className="truncate text-2xs text-faint-foreground">
              {target.device.device_type ?? "device"}
              {target.device.ip_address ? ` · ${target.device.ip_address}` : ""}
            </p>
          )}
          {target?.kind === "onu" && (
            <p className="truncate text-2xs text-faint-foreground">{target.where}</p>
          )}
        </SheetHeader>

        <div className="flex flex-col gap-4 px-4">
          <FixReadout phase={phase} fix={fix} error={error} borrowed={sameAs}
                      pinned={pin != null} moved={moved} onRetry={start} />

          {coords && (
            <div className="flex flex-col rounded-xl border bg-muted/40">
              <button
                type="button"
                onClick={() => setMapOpen((v) => !v)}
                className={cn("flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-foreground/5",
                  mapOpen ? "rounded-t-xl" : "rounded-xl")}
              >
                <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
                  mapOpen && "rotate-90")} />
                <span className="text-2xs font-medium text-muted-foreground">
                  {moved ? "Pin set by hand"
                    : pin ? "Where this was recorded"
                      : "Adjust pin on map"}
                </span>
                <span className="ml-auto shrink-0 font-mono text-2xs text-faint-foreground">
                  {coords.lat.toFixed(5)}, {coords.lng.toFixed(5)}
                </span>
              </button>
              {mapOpen && (
                <div className="p-2 pt-0">
                  <PinAdjustMap
                    org={org}
                    lat={coords.lat}
                    lng={coords.lng}
                    adjusted={pin != null}
                    moved={moved}
                    onAdjust={(lat, lng) => { setPin({ lat, lng }); setMoved(true) }}
                    onReset={() => { setPin(null); setMoved(false) }}
                  />
                </div>
              )}
            </div>
          )}

          {target?.kind === "onu" && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="survey-onu-name">Customer name</Label>
              <Input
                id="survey-onu-name"
                value={name}
                onChange={(e) => setName(e.target.value.toUpperCase())}
                placeholder={target.walked || "Not recorded"}
                className="h-11"
                autoCapitalize="characters"
                autoCorrect="off"
              />
              <p className="text-2xs text-faint-foreground">
                {target.walked
                  ? <>Saved here for your records. The OLT keeps calling it{" "}
                      <span className="font-mono">{target.walked}</span>.</>
                  : "Saved here for your records. This ONU has no name on the OLT."}
              </p>
            </div>
          )}

          {target?.kind === "onu" && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="survey-onu-phone">Phone number</Label>
              <Input
                id="survey-onu-phone"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="e.g. 9876543210"
                className="h-11"
                type="tel"
                inputMode="tel"
                autoComplete="off"
                autoCorrect="off"
              />
              {phone.trim().length > 0 && !phoneOk(phone) && (
                <p className="text-2xs text-destructive">
                  That doesn't look like a phone number. 7 to 15 digits.
                </p>
              )}
            </div>
          )}

          {isPassive && (
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-1.5">
                <Label>Split ratio</Label>
                <SplitRatioField value={split} onChange={setSplit} />
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor="survey-name">Name</Label>
                <Input
                  id="survey-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. SPL-MAIN-04"
                  className="h-11 font-mono"
                  autoCapitalize="characters"
                  autoCorrect="off"
                />
              </div>

              {canWrite && (
                <div className="flex flex-col gap-1.5">
                  <Label>Fed from</Label>
                  <Select
                    value={effectiveFeederId != null ? String(effectiveFeederId) : NO_FEEDER}
                    onValueChange={(v) => {
                      setFeederTouched(true)
                      setFeederId(v === NO_FEEDER ? null : Number(v))
                      setPon(""); setPonTouched(false)
                    }}>
                    <SelectTrigger className="h-11"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_FEEDER}>No feeder recorded</SelectItem>
                      {feederChoices.map((o) => (
                        <SelectItem key={o.device.id} value={String(o.device.id)}>
                          {o.device.name}
                          <span className="ml-2 font-mono text-2xs text-faint-foreground">
                            {o.meters == null ? "not placed"
                              : o.meters < 1000 ? `${Math.round(o.meters)} m`
                                : `${(o.meters / 1000).toFixed(1)} km`}
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {feeder ? (
                    <p className="text-2xs text-faint-foreground">
                      {!feederTouched && suggestedFeeder
                        ? <>Nearest box, {Math.round(suggestedFeeder.meters)} m away. Change it if that isn't the feeder.</>
                        : <>Recorded as fed from {feeder.name}.</>}
                    </p>
                  ) : (
                    <p className="text-2xs text-faint-foreground">
                      Nothing placed within 2 km. You can set the feeder later
                      from the map.
                    </p>
                  )}
                </div>
              )}

              {canWrite && ponOlt && (
                <div className="flex flex-col gap-1.5">
                  <Label>PON on {ponOlt.name} (optional)</Label>
                  <Select
                    value={effectivePon || NO_PON}
                    onValueChange={(v) => {
                      setPonTouched(true)
                      setPon(v === NO_PON ? "" : v)
                    }}>
                    <SelectTrigger className="h-11">
                      <SelectValue placeholder="Not recorded" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_PON}>Not recorded</SelectItem>
                      {ponOptions(pons, effectivePon).map((p) => (
                        <SelectItem key={p} value={p} className="font-mono">{p}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-2xs text-faint-foreground">
                    {ponsLoading ? "Reading PON labels…"
                      : pons.length === 0
                        ? `${ponOlt.name} has no ONU roster yet, so there is nothing to pick from.`
                        : !ponTouched && feederPon.inherited && feederPon.pon
                          ? `Inherited from ${feeder?.name}. One fibre goes into a splitter, so it is on its feeder's PON.`
                          : "Which PON this box hangs off."}
                  </p>
                </div>
              )}

              {!canWrite && (
                <p className="text-2xs text-faint-foreground">
                  It joins no parent and is not monitored. The owner wires it into
                  the network on the dashboard.
                </p>
              )}
            </div>
          )}

          {replacing && !renameOnly && (
            <div className="flex items-start gap-2.5 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2.5 text-2xs">
              <TriangleAlert className="mt-px size-3.5 shrink-0 text-warning" />
              <span>
                {target?.kind === "device"
                  ? <>This already has a location, {placedNote(target.device)}. Saving replaces it.</>
                  : "This subscriber already has a location. Saving replaces it."}
              </span>
            </div>
          )}

          <SameSpot placed={placed} value={sameAs} onChange={setSameAs} />
        </div>

        <div className="sticky bottom-0 z-10 mt-4 flex flex-col gap-2 border-t bg-background/95 px-4 pt-3 pb-1 backdrop-blur">
          <Button
            className="h-12 w-full text-sm"
            disabled={!canSave || !good}
            onClick={save}
          >
            <Check className="size-4" />
            {isPassive ? "Record this plant"
              : renameOnly ? "Save details"
                : target?.kind === "onu" ? "Save subscriber location"
                  : "Save this location"}
          </Button>
          {canSave && !good && (
            <Button variant="ghost" className="h-10 w-full text-xs" onClick={save}>
              Save anyway at {fixLabel(coords?.accuracy ?? null)}
            </Button>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function FixReadout({ phase, fix, error, borrowed, pinned, moved, onRetry }: {
  phase: string
  fix: GpsFix | null
  error: string | null
  borrowed: OrgDevice | null
  pinned: boolean
  moved: boolean
  onRetry: () => void
}) {
  if (pinned) {
    return (
      <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3">
        <MapPin className="size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 text-sm">
          {moved ? "Pin placed by hand" : "Existing location"}
          <span className="block text-2xs text-faint-foreground">
            {moved
              ? "Saved as an exact spot rather than a measurement"
              : "Left where it was. Edit the details, or drag the pin to move it."}
          </span>
        </span>
      </div>
    )
  }
  if (borrowed) {
    return (
      <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3">
        <MapPin className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 text-sm">
          Using <span className="font-medium">{borrowed.name}</span>'s exact spot
          <span className="block text-2xs text-faint-foreground tabular-nums">
            {borrowed.lat?.toFixed(6)}, {borrowed.lng?.toFixed(6)}
          </span>
        </span>
      </div>
    )
  }
  if (phase === "error") {
    return (
      <div className="flex flex-col gap-2.5 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
        <span className="text-sm text-destructive">{error}</span>
        <Button size="sm" variant="outline" className="h-9 self-start" onClick={onRetry}>
          <Crosshair className="size-3.5" /> Retry
        </Button>
      </div>
    )
  }
  const acquiring = phase === "acquiring"
  return (
    <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3">
      <Signal className={cn("size-4 shrink-0",
        acquiring && "animate-pulse text-muted-foreground",
        !acquiring && fix && (fix.accuracy <= GOOD_FIX_M ? "text-success" : "text-warning"),
        !acquiring && !fix && "text-faint-foreground")} />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="text-sm font-medium">
            {acquiring ? "Getting a fix…" : fix ? "Located" : "No fix"}
          </span>
          {fix && (
            <Chip tone={fixTone(fix.accuracy)}>{fixLabel(fix.accuracy)}</Chip>
          )}
        </span>
        {fix && (
          <span className="block text-2xs text-faint-foreground tabular-nums">
            {fix.lat.toFixed(6)}, {fix.lng.toFixed(6)}
          </span>
        )}
        {!acquiring && fix && fix.accuracy > GOOD_FIX_M && (
          <span className="block text-2xs text-warning">
            Loose fix. Step into the open and retry for a tighter one.
          </span>
        )}
      </span>
      {!acquiring && (
        <Button size="icon" variant="ghost" className="size-9 shrink-0" onClick={onRetry}>
          <Crosshair className="size-4" />
        </Button>
      )}
    </div>
  )
}

function SameSpot({ placed, value, onChange }: {
  placed: OrgDevice[]
  value: OrgDevice | null
  onChange: (d: OrgDevice | null) => void
}) {
  const [q, setQ] = useState("")
  const [open, setOpen] = useState(false)
  const needle = q.trim().toLowerCase()
  const hits = useMemo(
    () => (needle ? placed.filter((d) => d.name.toLowerCase().includes(needle)).slice(0, 6) : []),
    [placed, needle])

  if (value) {
    return (
      <Button variant="ghost" className="h-9 justify-start px-2 text-xs"
              onClick={() => { onChange(null); setQ(""); setOpen(false) }}>
        <X className="size-3.5" /> Use my own fix instead
      </Button>
    )
  }
  if (!open) {
    return (
      <Button variant="ghost" className="h-9 justify-start px-2 text-xs"
              onClick={() => setOpen(true)}>
        <MapPin className="size-3.5" /> Same spot as another device…
      </Button>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <Input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Which device is it beside?"
        className="h-11"
        autoCapitalize="none"
        autoCorrect="off"
        autoFocus
      />
      {hits.map((d) => (
        <button
          key={d.id}
          type="button"
          onClick={() => { onChange(d); setOpen(false) }}
          className="flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-left text-sm hover:bg-foreground/5"
        >
          <MapPin className="size-3.5 shrink-0 text-faint-foreground" />
          <span className="min-w-0 flex-1 truncate">{d.name}</span>
          {isPassiveType(d.device_type) && (
            <span className="shrink-0 text-2xs text-faint-foreground">{d.device_type}</span>
          )}
        </button>
      ))}
    </div>
  )
}
