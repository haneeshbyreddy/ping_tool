import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { tq } from "@/lib/api"

const LIVE_QUERY_KEYS = [
  "summary", "outages", "inventory", "logs", "nodes",
  "snmp-walks", // a queued walk's result lands on the edge's report cadence
  "issues",
]

export function useEventStream(org: string | null) {
  const queryClient = useQueryClient()

  useEffect(() => {
    let source: EventSource | null = null

    const invalidateAll = () => {
      for (const key of LIVE_QUERY_KEYS) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    }

    const open = () => {
      source = new EventSource(`/api/events${tq(org)}`)
      source.addEventListener("changed", invalidateAll)
    }

    const onVisible = () => {
      if (document.visibilityState !== "visible") return
      if (!source || source.readyState !== EventSource.OPEN) {
        source?.close()
        open()
      }
      invalidateAll()
    }

    open()
    document.addEventListener("visibilitychange", onVisible)
    return () => {
      document.removeEventListener("visibilitychange", onVisible)
      source?.close()
    }
  }, [org, queryClient])
}
