import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { inventoryApi, outagesApi, nodesApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"

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

  const go = (path: string, state?: { deviceId: number }) => {
    onOpenChange(false)
    setQuery("")
    navigate(path, { state })
  }

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Search" description="Search devices, outages, probes…">
      <Command>
        <CommandInput placeholder="Search devices, outages, probes…" value={query} onValueChange={setQuery} />
        <CommandList>
          <CommandEmpty>No results.</CommandEmpty>
          <CommandGroup heading="Devices">
            {devices.data?.devices.map((d) => (
              <CommandItem key={d.id} value={`${d.name} ${d.ip_address}`} onSelect={() => go("/topology", { deviceId: d.id })}>
                <span className="flex-1 truncate">{d.name}</span>
                <span className="font-mono text-xs text-muted-foreground">{d.ip_address}</span>
              </CommandItem>
            ))}
          </CommandGroup>
          <CommandGroup heading="Outages">
            {outages.data?.outages.filter((o) => !o.resolved_at).map((o) => (
              <CommandItem key={o.id} value={o.device_name} onSelect={() => go("/")}>
                <span className="flex-1 truncate">{o.device_name}</span>
                <span className="text-xs text-muted-foreground">{o.status.replace("_", " ")}</span>
              </CommandItem>
            ))}
          </CommandGroup>
          <CommandGroup heading="Probes">
            {nodes.data?.nodes.map((n) => (
              <CommandItem key={n.node_id} value={n.node_id} onSelect={() => go("/topology")}>
                {n.node_id}
              </CommandItem>
            ))}
          </CommandGroup>
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
