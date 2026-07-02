import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { tq } from "@/lib/api"

// GET /api/events (server.py:292) is a tenant-scoped SSE stream that pushes a cheap
// version fingerprint (`{events_max_id}.{outages_max_id}.{switch_ports_updated_at}`)
// whenever new data lands — see central/store.py:data_version. We don't care about the
// fingerprint's value, just that it changed, so every "changed" event just invalidates
// the query keys the live views read. Reconnects automatically (native EventSource
// behavior) if the tenant scope is stable; rebuilt whenever it changes.
const LIVE_QUERY_KEYS = ["summary", "outages", "inventory", "logs", "team", "attendance"]

export function useEventStream(tenant: string | null) {
  const queryClient = useQueryClient()

  useEffect(() => {
    const source = new EventSource(`/api/events${tq(tenant)}`)
    source.addEventListener("changed", () => {
      for (const key of LIVE_QUERY_KEYS) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    })
    return () => source.close()
  }, [tenant, queryClient])
}
