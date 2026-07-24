import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { useAuth } from "@/hooks/use-auth"
import { inventoryApi } from "@/lib/api"
import { NeedsOrg } from "@/components/needs-org"
import { ProbesPanel } from "@/components/probes-panel"
import { ViewToggle, loadView, saveView, type ViewMode } from "@/components/view-toggle"

/** Probes — the fleet of edge probes, on their own page under Infrastructure.
 *
 *  This was a panel sitting atop the Network page, above the device tree. Probes
 *  are their own kind of thing — you enrol/rotate/retire one a few times, then
 *  rarely touch it — so they get their own place instead of competing with the
 *  devices for the top of the screen every visit.
 *
 *  The one interaction that spanned both — click a probe to see just its devices
 *  — is preserved as a DEEP LINK: each card's "N devices" button navigates to the
 *  Network page with that probe pre-filtered (the same `navState.probeId` the
 *  stale-probe card and the command palette already use), so the filter lands
 *  where the devices actually live rather than on a page that has none. */
export function ProbesPage() {
  const { scopeOrg, canWrite } = useAuth()
  const navigate = useNavigate()
  const [view, setView] = useState<ViewMode>(loadView)
  const changeView = (v: ViewMode) => { setView(v); saveView(v) }

  // Device count per probe drives each card's "N devices" clickthrough. Same
  // ["inventory", org] query the Network page fetches, so it's a cache hit.
  const { data } = useQuery({
    queryKey: ["inventory", scopeOrg],
    queryFn: () => inventoryApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })
  const deviceCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const d of data?.devices ?? []) {
      if (d.assigned_node_id) m.set(d.assigned_node_id, (m.get(d.assigned_node_id) ?? 0) + 1)
    }
    return m
  }, [data])

  if (!scopeOrg) return <NeedsOrg />

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-base font-semibold">Probes</h1>
        <ViewToggle view={view} onChange={changeView} />
      </div>

      {/* probeFilter stays null — this page has no devices to filter. Clicking a
          probe's device count deep-links to the Network page filtered to that
          probe instead, where the devices actually are. */}
      <ProbesPanel
        org={scopeOrg}
        canWrite={canWrite}
        view={view}
        deviceCounts={deviceCounts}
        probeFilter={null}
        onProbeFilter={(id) => { if (id) navigate("/topology", { state: { probeId: id } }) }}
        showHeading={false}
      />
    </div>
  )
}
