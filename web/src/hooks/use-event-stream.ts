import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { authApi, tq } from "@/lib/api"
import { onServerBuild } from "@/lib/build-refresh"

const LIVE_QUERY_KEYS = [
  "summary", "outages", "inventory", "nodes",
  "issues",
]

// Away longer than this and the whole screen refetches on return, not just the
// live keys — pre-sleep data on every other panel reads as "the site is stale".
const LONG_AWAY_MS = 60_000

// An EventSource cannot see WHY its connection died — an expired session and a
// restarting central look identical from JS — so a dying stream probes /api/me.
// A 401 there dispatches wisp:unauthorized and lands on the login page; any
// other failure is ignored (central will be back, the stream retries itself).
let lastAuthProbe = 0
function probeAuth() {
  const now = Date.now()
  if (now - lastAuthProbe < 15_000) return
  lastAuthProbe = now
  authApi.me().catch(() => {})
}

export function useEventStream(org: string | null) {
  const queryClient = useQueryClient()

  useEffect(() => {
    let source: EventSource | null = null
    let hiddenAt: number | null = null

    const invalidateLive = () => {
      for (const key of LIVE_QUERY_KEYS) {
        queryClient.invalidateQueries({ queryKey: [key] })
      }
    }

    const open = () => {
      source = new EventSource(`/api/events${tq(org)}`)
      source.addEventListener("changed", invalidateLive)
      source.addEventListener("build", (e) => onServerBuild((e as MessageEvent).data))
      source.addEventListener("error", () => {
        if (document.visibilityState === "visible") probeAuth()
      })
    }

    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now()
        return
      }
      if (!source || source.readyState !== EventSource.OPEN) {
        source?.close()
        open()
      }
      if (hiddenAt !== null && Date.now() - hiddenAt >= LONG_AWAY_MS) {
        // One bounded pass: marks everything stale, refetches only what is
        // mounted. Not the per-event storm the live keys exist to avoid.
        queryClient.invalidateQueries()
      } else {
        invalidateLive()
      }
      hiddenAt = null
    }

    open()
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      document.removeEventListener("visibilitychange", onVisibility)
      source?.close()
    }
  }, [org, queryClient])
}
