import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ChevronRight, GitMerge, MapPin, MapPinned, Search, Split, TriangleAlert } from "lucide-react"
import { inventoryApi, ApiError } from "@/lib/api"
import { isPassiveType, type OnuOptic, type OrgDevice, type SubscriberDrop } from "@/lib/types"
import { useAuth } from "@/hooks/use-auth"
import { onuName } from "@/lib/format"
import { portKey } from "@/lib/fiber"
import { fmtKm, distanceKm, polyKm } from "@/map/geometry"
import { cumulativeSplit, feedChain } from "@/map/plant"
import { deviceRatioLabel, hasProtectionInput } from "@/map/drops"
import { SplitRatioField, type SplitRatio } from "@/components/split-ratio-field"
import { SubscriberDialog } from "@/components/subscriber-detail"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

function feedLength(
  device: OrgDevice, byId: Map<number, OrgDevice>,
  routeByKey: Map<string, Array<[number, number]>>,
): { km: number; traced: boolean } | null {
  const { passives, head } = feedChain(device, byId)
  if (!head) return null
  const hops = [device, ...passives, head]
  let km = 0
  let traced = true
  for (let i = 0; i < hops.length - 1; i++) {
    const child = hops[i], parent = hops[i + 1]
    if (child.lat == null || child.lng == null
      || parent.lat == null || parent.lng == null) return null
    const wps = routeByKey.get(`${child.id}:${parent.id}`)
    if (wps && wps.length) {
      km += polyKm([[parent.lat, parent.lng], ...wps, [child.lat, child.lng]])
    } else {
      km += distanceKm(child.lat, child.lng, parent.lat, parent.lng)
      traced = false
    }
  }
  return { km, traced }
}

const fmtDbm = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toFixed(2)} dBm`

function dropTone(d: SubscriberDrop): "ok" | "warn" | "crit" | "offline" | "unknown" {
  if (!d.matched) return "unknown"
  if (d.state !== "online") return "offline"
  if (d.severity === "crit") return "crit"
  if (d.severity === "warn") return "warn"
  return "ok"
}
const DOT: Record<string, string> = {
  ok: "bg-success", warn: "bg-warning", crit: "bg-destructive",
  offline: "bg-destructive/60", unknown: "bg-muted-foreground/40",
}

export function DistributionPanel({ device, onOpenFibre }: {
  device: OrgDevice; onOpenFibre?: () => void
}) {
  const { scopeOrg, canWrite } = useAuth()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(true)
  const [attaching, setAttaching] = useState(false)
  const [openSub, setOpenSub] = useState<string | null>(null)
  useEffect(() => { setAttaching(false); setOpenSub(null) }, [device.id])

  const invQ = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 30_000,
  })
  const routesQ = useQuery({
    queryKey: ["routes", scopeOrg],
    queryFn: () => inventoryApi.routes(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 60_000,
  })
  const dropsQ = useQuery({
    queryKey: ["splitter-drops", device.id],
    queryFn: () => inventoryApi.splitterDrops(device.id),
    enabled: isPassiveType(device.device_type),
    refetchInterval: 30_000,
  })

  const setRatio = useMutation({
    mutationFn: ({ ratio, inputs }: SplitRatio) => {
      const d = invQ.data?.devices.find((x) => x.id === device.id) ?? device
      return inventoryApi.update(d.id, {
        name: d.name, ip_address: d.ip_address,
        device_type: d.device_type, region: d.region,
        tags: d.tags, parent_device_id: d.parent_device_id,
        assigned_node_id: d.assigned_node_id, gpon_vendor: d.gpon_vendor,
        pon_port: d.pon_port, split_ratio: ratio, split_inputs: inputs,
        onu_pon_limit: d.onu_pon_limit,
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      queryClient.invalidateQueries({ queryKey: ["drops"] })
    },
    onError: (e) => toast.error(
      e instanceof ApiError ? e.message : "Couldn't save the split ratio"),
  })

  if (!isPassiveType(device.device_type)) return null

  const fibreQ = useQuery({
    queryKey: ["point-fibre", scopeOrg, device.id, null],
    queryFn: () => inventoryApi.pointFibre({ device_id: device.id }, scopeOrg),
    enabled: isPassiveType(device.device_type),
  })

  const devices = invQ.data?.devices ?? []
  const byId = new Map(devices.map((d) => [d.id, d]))
  const self = byId.get(device.id) ?? device
  const routeByKey = new Map<string, Array<[number, number]>>()
  for (const r of routesQ.data?.routes ?? [])
    if (r.waypoints.length) routeByKey.set(`${r.child_id}:${r.parent_id}`, r.waypoints)

  const { passives, head } = feedChain(self, byId)
  const olt = head && (head.device_type ?? "").toUpperCase() === "OLT" ? head : null
  const totalSplit = cumulativeSplit(self, byId)
  const length = feedLength(self, byId, routeByKey)
  const drops = dropsQ.data?.drops ?? []
  const load = dropsQ.data?.load ?? null
  const outlierDb = dropsQ.data?.outlier_db ?? 3
  const ratio = self.split_ratio
  const ratioText = deviceRatioLabel(self)
  const protection = hasProtectionInput(self)
  // A SECOND INPUT IS GLASS, so the count is the `in` PORTS CARRYING A FIBRE — not
  // `backup_parents`. A backup parent is `kind='backup'` in org_device_links, which
  // is what `DeviceMeta.effective_parents()` reads: THE ENGINE'S DEPENDENCY EDGE.
  // Recording fibre must never write that, and it would be a false claim anyway —
  // "traffic reroutes this way on failure" depends on STP/routing state central
  // cannot see, and a splitter has no state, no FSM and no routing.
  const inPorts = (fibreQ.data?.ports ?? []).filter((p) => p.kind === "in")
  const inTaken = new Set((fibreQ.data?.joints ?? [])
    .filter((j) => j.port_kind === "in" && j.b_cable_id == null)
    .map((j) => portKey(j.port_ref)))
  const feeds = inPorts.filter((p) => inTaken.has(portKey(p.ref))).length
  const freeInput = inPorts.find((p) => !inTaken.has(portKey(p.ref))) ?? null
  const over = !!ratio && !!load && load.recorded > ratio

  const splits = (self.device_type ?? "") === "splitter"
  const showRatio = splits || ratio != null
  const showSubs = drops.length > 0 || (splits && !!olt)
  const showFeed = !!head
  if (!showRatio && !showSubs && !showFeed) return null

  const ponChip = (() => {
    const walked = self.fibre_pon
    const typed = (self.pon_port ?? "").trim()
    if (walked?.ambiguous) {
      return { text: "PON ambiguous", disagrees: true,
               title: "Two PONs reach this box through the fibre record — a"
                      + " protection feed, or a mis-recorded splice." }
    }
    const from = walked?.pon_no != null ? String(walked.pon_no) : null
    if (!from && !typed) return null
    if (!from) {
      return { text: `PON ${typed}`, disagrees: false,
               title: "Typed on this box. Record which port its fibre lands on"
                      + " and the PON comes from the glass instead." }
    }
    const typedNo = typed.match(/(\d+)\s*$/)?.[1] ?? null
    const disagrees = !!typedNo && typedNo !== from
    const where = walked?.source === "inherited" ? " (inherited down the chain)" : ""
    return {
      text: disagrees ? `PON ${from} · typed ${typed}` : `PON ${from}`,
      disagrees,
      title: disagrees
        ? `The fibre record says PON ${from}${where}; this box is typed as`
          + ` "${typed}". One of the two is out of date.`
        : `From the fibre landed on the OLT's port${where}.`,
    }
  })()

  const ordered = [...drops].sort((a, b) => {
    const rank = (d: SubscriberDrop) =>
      !d.matched ? 3 : d.state !== "online" ? 0 : d.severity === "crit" ? 1
        : d.severity === "warn" ? 2 : 4
    const r = rank(a) - rank(b)
    if (r !== 0) return r
    return (a.rx_dbm ?? 0) - (b.rx_dbm ?? 0)
  })

  return (
    <div className="flex flex-col rounded-lg border bg-muted/40">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className={cn("flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-foreground/5",
          open ? "rounded-t-lg" : "rounded-lg")}>
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90")} />
        {splits && <Split className="size-3.5 shrink-0 text-muted-foreground" />}
        <span className="text-2xs font-medium text-muted-foreground">
          {splits ? "Distribution" : "Feed"}
        </span>
        <span className="ml-auto flex items-center gap-2 font-mono text-2xs text-faint-foreground">
          {showRatio && ratioText && <span>{ratioText}</span>}
          {showSubs && load ? <span>{load.recorded} recorded</span> : null}
          {showSubs && load?.dark
            ? <span className="text-destructive">{load.dark} dark</span> : null}
        </span>
      </button>

      {open && (
        <div className="flex flex-col gap-3 px-3 pb-3">
          {showFeed && head && (
            <div className="flex flex-col gap-1">
              <span className="wisp-eyebrow">Fed from</span>
              <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs">
                <span className="font-medium">{head.name}</span>
                {ponChip && (
                  <span className={cn(
                    "rounded px-1 py-px font-mono text-2xs",
                    ponChip.disagrees
                      ? "bg-warning-soft text-warning" : "bg-muted text-muted-foreground")}
                    title={ponChip.title}>
                    {ponChip.text}
                  </span>
                )}
                {passives.slice().reverse().map((p) => (
                  <span key={p.id} className="flex items-center gap-1.5 text-muted-foreground">
                    <ChevronRight className="size-3" />
                    <span>{p.name}</span>
                    {deviceRatioLabel(p) && (
                      <span className="font-mono text-2xs text-faint-foreground">{deviceRatioLabel(p)}</span>
                    )}
                  </span>
                ))}
                <ChevronRight className="size-3 text-muted-foreground" />
                <span className="font-medium">{self.name}</span>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-2xs text-muted-foreground">
                {splits && totalSplit != null && (
                  <span title="Every split between the OLT and this box, multiplied. Says whether the PON has budget left.">
                    Total split <span className="font-mono text-foreground">1:{totalSplit}</span>
                  </span>
                )}
                {splits && totalSplit == null && passives.length > 0 && (
                  <span>Total split unknown · a box in this chain has no ratio recorded</span>
                )}
                {length && (
                  <span title={length.traced
                    ? "Along the drawn cable routes"
                    : "Straight-line: part of this chain has no drawn route"}>
                    {length.traced ? "Cable" : "Straight-line"} from {head?.name}{" "}
                    <span className="font-mono text-foreground">{fmtKm(length.km)}</span>
                  </span>
                )}
              </div>
            </div>
          )}

          {showRatio && (
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="wisp-eyebrow">Split ratio</span>
                {!canWrite && (
                  <span className="font-mono text-xs">{ratioText ?? "—"}</span>
                )}
              </div>
              {canWrite && (
                <SplitRatioField
                  value={{ ratio, inputs: self.split_inputs }}
                  disabled={setRatio.isPending}
                  onChange={(next) => setRatio.mutate(next)} />
              )}
              {protection && (
                <div className="flex items-start gap-1.5 rounded border border-border-subtle bg-muted/50 px-2 py-1 text-2xs text-muted-foreground">
                  <GitMerge className="mt-px size-3 shrink-0" />
                  <span className="min-w-0">
                    Two inputs ·{" "}
                    <span className="font-mono text-foreground">{feeds}</span>{" "}
                    {feeds === 1 ? "fibre" : "fibres"} recorded.{" "}
                    {feeds < 2
                      ? "The protection feed is either not connected or not recorded."
                      : "Both inputs carry a fibre."}
                    {freeInput && onOpenFibre && (
                      <>
                        {" "}
                        <button type="button" onClick={onOpenFibre}
                          className="font-medium text-primary underline-offset-2 hover:underline">
                          Connect {freeInput.label}…
                        </button>
                      </>
                    )}
                  </span>
                </div>
              )}
              {ratio ? (
                <>
                  <div className="flex h-2 gap-px overflow-hidden rounded-sm" role="img"
                    aria-label={`${load?.recorded ?? 0} of ${ratio} legs recorded`}>
                    {Array.from({ length: ratio }, (_, i) => (
                      <span key={i} className={cn("flex-1",
                        i < (load?.recorded ?? 0) ? "bg-primary/70" : "bg-muted-foreground/20")} />
                    ))}
                  </div>
                  <p className="text-2xs text-faint-foreground">
                    {load?.recorded ?? 0} of {ratio} legs recorded.{" "}
                    Legs with no recorded subscriber are unknown, not free.
                  </p>
                </>
              ) : (
                <p className="text-2xs text-faint-foreground">
                  Record the ratio to see how loaded this box is, and to get a total
                  split for the PON.
                </p>
              )}
              {over && (
                <div className="flex items-start gap-1.5 rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-2xs text-destructive">
                  <TriangleAlert className="mt-px size-3 shrink-0" />
                  <span>
                    {load!.recorded} drops recorded on {ratio} legs. Either a drop is
                    recorded against the wrong box, or there's a splitter below this
                    one that isn't on the map.
                  </span>
                </div>
              )}
            </div>
          )}

          {showSubs && (
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="wisp-eyebrow">Subscribers</span>
                {canWrite && olt && (
                  <Button variant="outline" size="sm" className="h-7"
                    onClick={() => setAttaching(true)}>
                    Record subscribers
                  </Button>
                )}
              </div>

              {dropsQ.isLoading && <Skeleton className="h-16 w-full" />}

              {!dropsQ.isLoading && drops.length === 0 && (
                <p className="text-xs text-muted-foreground">
                  No subscribers recorded on this box yet. Record them and a break
                  below this box can be pinned to one span.
                </p>
              )}

              {load && load.rx_seen > 0 && (
                <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-2xs text-muted-foreground">
                  <span>
                    Median Rx <span className="font-mono text-foreground">{fmtDbm(load.rx_median)}</span>
                    <span className="text-faint-foreground"> · {load.rx_seen} measured</span>
                  </span>
                  <span>Worst <span className="font-mono text-foreground">{fmtDbm(load.rx_worst)}</span></span>
                  {load.outliers > 0 && (
                    <span className="text-warning" title={
                      `These sit ${outlierDb} dB or more below this splitter's own median. `
                      + "Same feeder and same split loss as their neighbours, so the "
                      + "difference is in that drop: a bend, a dirty connector or a bad splice."}>
                      {load.outliers} below this box's own median
                    </span>
                  )}
                </div>
              )}

              {ordered.length > 0 && (
                <div className="flex flex-col overflow-hidden rounded border">
                  {ordered.map((d) => {
                    const tone = dropTone(d)
                    const delta = d.rx_dbm != null && load?.rx_median != null && d.state === "online"
                      ? d.rx_dbm - load.rx_median : null
                    const low = delta != null && delta <= -outlierDb
                    return (
                      <div key={d.mac}
                        className="flex items-center gap-2 border-b px-2 py-1 text-2xs last:border-b-0">
                        <span className={cn("size-1.5 shrink-0 rounded-full", DOT[tone])} />
                        <button type="button"
                          className="min-w-0 flex-1 truncate text-left underline-offset-2 hover:underline"
                          title="Open this subscriber"
                          onClick={() => setOpenSub(d.mac)}>
                          {d.name || <span className="font-mono">{d.mac}</span>}
                        </button>
                        {d.witness && (
                          <MapPinned className="size-3 shrink-0 fill-current text-primary"
                            aria-label="Reference point"
                          />
                        )}
                        {d.pon_port && (
                          <span className="shrink-0 font-mono text-faint-foreground">
                            {d.pon_port}{d.onu_id != null ? `:${d.onu_id}` : ""}
                          </span>
                        )}
                        {!d.matched ? (
                          <span className="shrink-0 text-muted-foreground"
                            title="Recorded here, but this MAC is in no current roster. An RMA'd box, or a mistyped sticker.">
                            not in roster
                          </span>
                        ) : d.state !== "online" ? (
                          <span className="shrink-0 text-destructive">{d.state}</span>
                        ) : (
                          <span className={cn("shrink-0 font-mono",
                            low ? "text-warning" : "text-muted-foreground")}>
                            {fmtDbm(d.rx_dbm)}
                            {delta != null && Math.abs(delta) >= 0.5 && (
                              <span className={cn("ml-1", low ? "text-warning" : "text-faint-foreground")}
                                title="Against this splitter's own median. Same feeder, same split loss.">
                                {delta > 0 ? "+" : ""}{delta.toFixed(1)}
                              </span>
                            )}
                          </span>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {attaching && olt && (
        <AttachDropsDialog splitter={self} head={olt} current={drops}
          onClose={() => setAttaching(false)} />
      )}
      {openSub && (
        <SubscriberDialog mac={openSub} onClose={() => setOpenSub(null)} />
      )}
    </div>
  )
}

function AttachDropsDialog({ splitter, head, current, onClose }: {
  splitter: OrgDevice; head: OrgDevice; current: SubscriberDrop[]; onClose: () => void
}) {
  const { scopeOrg } = useAuth()
  const queryClient = useQueryClient()
  const [q, setQ] = useState("")
  const [allPons, setAllPons] = useState(false)
  const mine = useMemo(() => new Set(current.map((d) => d.mac)), [current])
  const [picked, setPicked] = useState<Set<string>>(() => new Set(mine))
  useEffect(() => { setPicked(new Set(mine)) }, [mine])

  const opticsQ = useQuery({
    queryKey: ["optics", head.id],
    queryFn: () => inventoryApi.optics(head.id),
    staleTime: 30_000,
  })
  const invQ = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    staleTime: 30_000,
  })
  const byId = new Map((invQ.data?.devices ?? []).map((d) => [d.id, d]))

  const onus = opticsQ.data?.onus ?? []
  const needle = q.trim().toUpperCase().replace(/[^A-Z0-9]/g, "")
  const rows = useMemo(() => onus.filter((o: OnuOptic) => {
    if (!o.serial) return false
    if (!allPons && splitter.pon_port && o.pon_port !== splitter.pon_port) return false
    if (!needle) return true
    const hay = `${o.serial}${o.name ?? ""}${o.label ?? ""}`
      .toUpperCase().replace(/[^A-Z0-9]/g, "")
    return hay.includes(needle)
  }), [onus, needle, allPons, splitter.pon_port])

  const save = useMutation({
    mutationFn: async () => {
      const add = [...picked].filter((m) => !mine.has(m))
      const remove = [...mine].filter((m) => !picked.has(m))
      if (add.length)
        await inventoryApi.setDrops({ macs: add, passive_id: splitter.id, org_id: scopeOrg })
      if (remove.length)
        await inventoryApi.setDrops({ macs: remove, passive_id: null, org_id: scopeOrg })
      return { added: add.length, removed: remove.length }
    },
    onSuccess: ({ added, removed }) => {
      queryClient.invalidateQueries({ queryKey: ["splitter-drops"] })
      queryClient.invalidateQueries({ queryKey: ["drops"] })
      queryClient.invalidateQueries({ queryKey: ["optics"] })
      queryClient.invalidateQueries({ queryKey: ["onu-places"] })
      const bits = [added ? `${added} recorded` : null,
        removed ? `${removed} removed` : null].filter(Boolean)
      toast.success(bits.length ? bits.join(", ") : "No change")
      onClose()
    },
    onError: (e) => toast.error(
      e instanceof ApiError ? e.message : "Couldn't save the drops"),
  })

  const toggle = (mac: string) => setPicked((prev) => {
    const next = new Set(prev)
    if (next.has(mac)) next.delete(mac); else next.add(mac)
    return next
  })

  const changed = picked.size !== mine.size
    || [...picked].some((m) => !mine.has(m))

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Subscribers on {splitter.name}</DialogTitle>
          <DialogDescription>
            From {head.name}
            {splitter.pon_port && !allPons ? <> · PON {splitter.pon_port}</> : null}
            {splitter.split_ratio ? <> · 1:{splitter.split_ratio}</> : null}
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="MAC or name…" className="h-8 pl-7 text-xs" />
          </div>
          {splitter.pon_port && (
            <Button variant={allPons ? "default" : "outline"} size="sm" className="h-8"
              onClick={() => setAllPons((v) => !v)}
              title="This splitter is bound to one PON; the rest of the OLT is hidden by default">
              All PONs
            </Button>
          )}
        </div>

        <div className="max-h-[45vh] overflow-y-auto rounded border">
          {opticsQ.isLoading && <Skeleton className="h-24 w-full" />}
          {!opticsQ.isLoading && rows.length === 0 && (
            <p className="px-3 py-4 text-center text-xs text-muted-foreground">
              {onus.length === 0
                ? "No ONU roster for this OLT yet. The SNMP walk has to run first."
                : "Nothing matches."}
            </p>
          )}
          {rows.map((o: OnuOptic) => {
            const mac = (o.serial ?? "").trim().toUpperCase()
            const on = picked.has(mac)
            const elsewhere = o.drop_passive_id != null && o.drop_passive_id !== splitter.id
              ? byId.get(o.drop_passive_id) : null
            return (
              <label key={o.id}
                className="flex cursor-pointer items-center gap-2 border-b px-2 py-1.5 text-xs last:border-b-0 hover:bg-foreground/5">
                <input type="checkbox" checked={on} onChange={() => toggle(mac)}
                  className="size-3.5 shrink-0 accent-[var(--primary)]" />
                <span className={cn("size-1.5 shrink-0 rounded-full",
                  o.state === "online" ? "bg-success" : "bg-destructive/60")} />
                <span className="min-w-0 flex-1 truncate">
                  {onuName(o) || <span className="font-mono">{o.serial}</span>}
                </span>
                {o.place && (o.place.witness
                  ? <MapPinned className="size-3 shrink-0 fill-current text-primary"
                      aria-label="Reference point" />
                  : <MapPin className="size-3 shrink-0 fill-current text-muted-foreground"
                      aria-label="On the map" />)}
                <span className="shrink-0 font-mono text-2xs text-faint-foreground">
                  {o.pon_port}{o.onu_id != null ? `:${o.onu_id}` : ""}
                </span>
                {elsewhere && (
                  <span className="shrink-0 rounded bg-warning-soft px-1 py-px text-2xs text-warning"
                    title={`Currently recorded on ${elsewhere.name}. Ticking this moves the drop.`}>
                    on {elsewhere.name}
                  </span>
                )}
              </label>
            )
          })}
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <span className="self-center text-2xs text-muted-foreground">
            {picked.size} selected
          </span>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            <Button size="sm" disabled={!changed || save.isPending}
              onClick={() => save.mutate()}>
              Save
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
