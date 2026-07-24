import { useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { Radio, TriangleAlert } from "lucide-react"
import { inventoryApi, outagesApi, nodesApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import { useDebounced } from "@/hooks/use-debounced"
import { DOT as ONU_DOT, onuSev } from "@/components/optical-panel"
import { cn } from "@/lib/utils"
import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"

// Server-side ONU-search floor, mirrored so we don't fire a request we know comes
// back empty (central/api/devices.py:ONU_SEARCH_MIN). Must match
// `onuroster.search_key` — punctuation is stripped before the length is judged,
// so "hc_" is 2 characters and does NOT reach the server.
const ONU_SEARCH_MIN = 3
const onuSearchKey = (s: string) => s.replace(/[^a-z0-9]/gi, "")

// The Ctrl/⌘-K palette is the Network page's search box, everywhere. It matches
// devices on the SAME fields the tree does (name, IP, type, region, tags) and
// runs the SAME ONU-by-MAC/name lookup, then deep-links a hit straight into the
// device panel — the ONU into its OLT's Optical tab, focused. cmdk's own fuzzy
// filter is turned off (`shouldFilter={false}`) so this substring matching is the
// single source of truth, byte-for-byte with `filterWithAncestors`.
export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const navigate = useNavigate()
  const { scopeOrg } = useAuth()
  const [query, setQuery] = useState("")

  const devices = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: open && !!scopeOrg,
  })
  const outages = useQuery({
    queryKey: ["outages", scopeOrg],
    queryFn: () => outagesApi.list(scopeOrg),
    enabled: open && !!scopeOrg,
  })
  const nodes = useQuery({
    queryKey: ["nodes", scopeOrg],
    queryFn: () => nodesApi.list(scopeOrg),
    enabled: open && !!scopeOrg,
  })

  // ONU search, by serial/MAC or provisioned name — the same debounce (300ms) and
  // 3-char punctuation-blind floor as the Network page, because it scans the org's
  // whole onu_optics table server-side.
  const needle = useDebounced(query.trim(), 300)
  const onuFetchOn = open && onuSearchKey(needle).length >= ONU_SEARCH_MIN
  const onuHits = useQuery({
    queryKey: ["onu-search", scopeOrg, needle],
    queryFn: () => inventoryApi.onuSearch(scopeOrg, needle),
    enabled: !!scopeOrg && onuFetchOn,
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        onOpenChange(!open)
      }
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [open, onOpenChange])

  // fresh box each open
  useEffect(() => { if (!open) setQuery("") }, [open])

  const go = (path: string, state?: Record<string, unknown>) => {
    onOpenChange(false)
    setQuery("")
    navigate(path, { state })
  }

  // Substring matching identical to the Network tree's `filterWithAncestors`:
  // name, IP, type, region, tags. Empty query shows everything (the palette's
  // historical behavior), so it doubles as a quick jump list.
  const q = query.trim().toLowerCase()
  const matchedDevices = useMemo(() => {
    const all = devices.data?.devices ?? []
    if (!q) return all
    return all.filter((d) =>
      d.name.toLowerCase().includes(q)
      || d.ip_address.includes(q)
      || (d.device_type ?? "").toLowerCase().includes(q)
      || (d.region ?? "").toLowerCase().includes(q)
      || d.tags.some((t) => t.toLowerCase().includes(q)))
  }, [devices.data, q])

  const matchedOutages = useMemo(() => {
    const all = (outages.data?.outages ?? []).filter((o) => !o.resolved_at)
    if (!q) return all
    return all.filter((o) => o.device_name.toLowerCase().includes(q))
  }, [outages.data, q])

  const matchedNodes = useMemo(() => {
    const all = nodes.data?.nodes ?? []
    if (!q) return all
    return all.filter((n) => n.node_id.toLowerCase().includes(q))
  }, [nodes.data, q])

  // Flatten the ONU hits (grouped per OLT server-side) into a single list, each
  // row carrying its OLT so the pick can deep-link into that OLT's Optical tab.
  const onuData = onuFetchOn ? onuHits.data : undefined
  const onuRows = useMemo(() =>
    (onuData?.matches ?? []).flatMap((m) =>
      m.onus.map((o) => ({ ...o, device_id: m.device_id, device_name: m.device_name }))),
    [onuData])

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Search"
      description="Search devices, ONUs, outages, probes…">
      <Command shouldFilter={false}>
        <CommandInput placeholder="Search devices, ONUs, outages, probes…" value={query} onValueChange={setQuery} />
        <CommandList>
          <CommandEmpty>No results.</CommandEmpty>
          {onuRows.length > 0 && (
            <CommandGroup heading="ONUs">
              {onuRows.map((o) => (
                <CommandItem key={`onu-${o.id}`} value={`onu-${o.id}`}
                  onSelect={() => go("/topology", { deviceId: o.device_id, tab: "optical", onuId: o.id })}>
                  <span className={cn("size-2 shrink-0 rounded-full", ONU_DOT[onuSev(o)])} />
                  <span className="shrink-0 font-mono text-xs font-medium">{o.serial || o.onu_key}</span>
                  <span className="min-w-0 flex-1 truncate text-muted-foreground">
                    {o.name || <span className="text-faint-foreground">unnamed</span>}
                  </span>
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">
                    {o.device_name}{o.pon_port ? ` · PON ${o.pon_port}` : ""}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {matchedDevices.length > 0 && (
            <CommandGroup heading="Devices">
              {matchedDevices.map((d) => (
                <CommandItem key={d.id} value={`device-${d.id}`}
                  onSelect={() => go("/topology", { deviceId: d.id })}>
                  <span className="min-w-0 flex-1 truncate">{d.name}</span>
                  {d.device_type && (
                    <span className="shrink-0 text-xs text-faint-foreground">{d.device_type}</span>
                  )}
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">{d.ip_address}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {matchedOutages.length > 0 && (
            <CommandGroup heading="Outages">
              {matchedOutages.map((o) => (
                <CommandItem key={`outage-${o.id}`} value={`outage-${o.id}`} onSelect={() => go("/")}>
                  <TriangleAlert className="size-3.5 text-destructive" />
                  <span className="min-w-0 flex-1 truncate">{o.device_name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">{o.status.replace("_", " ")}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {matchedNodes.length > 0 && (
            <CommandGroup heading="Probes">
              {matchedNodes.map((n) => (
                <CommandItem key={`node-${n.node_id}`} value={`node-${n.node_id}`}
                  onSelect={() => go("/topology", { probeId: n.node_id })}>
                  <Radio className="size-3.5 text-muted-foreground" />
                  {n.node_id}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
