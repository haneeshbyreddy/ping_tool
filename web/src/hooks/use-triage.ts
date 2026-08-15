// The one derivation of "what needs a human right now", shared by the nav
// badge, Home's verdict band and the Triage page — three surfaces quoting one
// number, so they can never disagree about how deep the queue is (the
// count-agreement rule). The query keys match Home's originals ("outages",
// "nodes"), both in the SSE live set, so every mount dedupes into the same
// cache entries and the badge stays live without its own polling.
import { useQuery } from "@tanstack/react-query"
import { useAuth } from "@/hooks/use-auth"
import { nodesApi, outagesApi } from "@/lib/api"
import { isStale } from "@/lib/format"
import type { NodeToken, Outage } from "@/lib/types"

export interface Triage {
  loading: boolean
  nodes: NodeToken[]
  activeNodes: NodeToken[]
  staleNodes: NodeToken[]
  activeOutages: Outage[]
  postmortems: Outage[]
  // What needs a response now: open outages + probes gone dark. Pending
  // post-mortems are paperwork and deliberately not part of this number.
  urgent: number
  total: number
}

export function useTriage(): Triage {
  const { scopeOrg } = useAuth()
  const outagesQ = useQuery({
    queryKey: ["outages", scopeOrg],
    queryFn: () => outagesApi.list(scopeOrg),
    enabled: !!scopeOrg,
  })
  const nodesQ = useQuery({
    queryKey: ["nodes", scopeOrg],
    queryFn: () => nodesApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })

  const outages = outagesQ.data?.outages ?? []
  const nodes = nodesQ.data?.nodes ?? []
  const activeNodes = nodes.filter((n) => n.registered && !n.revoked_at)
  const staleNodes = activeNodes.filter((n) => n.last_seen && isStale(n.last_seen))
  const activeOutages = outages.filter((o) => o.status !== "pending_postmortem")
  const postmortems = outages.filter((o) => o.status === "pending_postmortem")
  const urgent = activeOutages.length + staleNodes.length

  return {
    loading: outagesQ.isLoading || nodesQ.isLoading,
    nodes, activeNodes, staleNodes, activeOutages, postmortems,
    urgent, total: urgent + postmortems.length,
  }
}
