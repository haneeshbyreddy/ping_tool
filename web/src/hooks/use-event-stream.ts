import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { tq } from "@/lib/api"

const LIVE_QUERY_KEYS = ["summary", "outages", "inventory", "logs", "team", "attendance", "nodes"]

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

    // Mobile browsers freeze a backgrounded tab: the EventSource connection dies
    // and SSE has no replay, so any `changed` event fired while we were away is
    // lost and the UI stays on its last (stale) paint. On becoming visible again,
    // reopen the stream unless it's actively OPEN — a dropped connection usually
    // sits in CONNECTING (native retry, sometimes a long backoff), not CLOSED, so
    // keying off CLOSED alone misses the common case. Then do one immediate
    // catch-up invalidation so we resync current state rather than waiting for the
    // next server-side change.
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
