// The triage queue, promoted from a Home section to its own page (2026-08-15,
// the operator's ask: Home becomes the visual overview, the queue gets a page
// and a nav badge so nothing is missed for living elsewhere). Three sections
// in response order — probes dark (the biggest blast radius), open outages,
// then pending post-mortems (paperwork, folded past a preview). The nav badge
// and Home's verdict band quote useTriage()'s numbers, so the chip that
// brought you here and the page you land on can never disagree.
import { useState } from "react"
import { ChevronDown, ChevronUp } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useTriage } from "@/hooks/use-triage"
import { ClearPostmortems } from "@/components/clear-postmortems"
import { NeedsOrg } from "@/components/needs-org"
import { OutageCard } from "@/components/outage-card"
import { StaleNodeCard } from "@/components/stale-node-card"
import { StatusDot } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

const POSTMORTEM_PREVIEW = 3

function SectionHead({ label, count, children }: {
  label: string
  count: number
  children?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <h2 className="flex items-baseline gap-2">
        <span className="text-sm font-semibold text-foreground">{label}</span>
        <span className="font-mono text-xs text-faint-foreground">{count}</span>
      </h2>
      {children}
    </div>
  )
}

export function TriagePage() {
  const { scopeOrg } = useAuth()
  const t = useTriage()
  const [showAll, setShowAll] = useState(false)

  if (!scopeOrg) return <NeedsOrg />

  const visible = showAll ? t.postmortems : t.postmortems.slice(0, POSTMORTEM_PREVIEW)
  const hidden = t.postmortems.length - visible.length
  const grid = "grid gap-3 @md:grid-cols-2 @md:items-start @4xl:grid-cols-3"

  return (
    <div className="wisp-page @container flex flex-col gap-5 p-4 md:px-8 md:py-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="flex items-center gap-3 text-lg font-semibold tracking-tight">
          Triage
          {!t.loading && t.total > 0 && (
            <span className="rounded-4xl border bg-card px-2.5 py-0.5 text-xs font-semibold">
              {t.total} open
            </span>
          )}
        </h1>
      </div>

      {t.loading ? (
        <div className={grid}>
          <Skeleton className="h-36 rounded-xl" />
          <Skeleton className="h-36 rounded-xl" />
          <Skeleton className="h-36 rounded-xl" />
        </div>
      ) : t.total === 0 ? (
        <div className="wisp-panel flex flex-col items-center gap-2.5 px-6 py-16 text-center">
          <StatusDot tone="success" />
          <p className="text-sm font-medium text-foreground">The queue is clear.</p>
          <p className="max-w-sm text-xs text-muted-foreground">
            No open outages, every probe reporting, nothing awaiting a post-mortem.
            New outages land here the moment they open.
          </p>
        </div>
      ) : (
        <>
          {t.staleNodes.length > 0 && (
            <section className="flex flex-col gap-3">
              <SectionHead label="Probes dark" count={t.staleNodes.length} />
              <div className={grid}>
                {t.staleNodes.map((n) => <StaleNodeCard key={n.node_id} node={n} />)}
              </div>
            </section>
          )}
          {t.activeOutages.length > 0 && (
            <section className="flex flex-col gap-3">
              <SectionHead label="Open outages" count={t.activeOutages.length} />
              <div className={grid}>
                {t.activeOutages.map((o) => <OutageCard key={o.id} outage={o} />)}
              </div>
            </section>
          )}
          {t.postmortems.length > 0 && (
            <section className="flex flex-col gap-3">
              <SectionHead label="Pending post-mortems" count={t.postmortems.length}>
                <ClearPostmortems org={scopeOrg} count={t.postmortems.length} />
              </SectionHead>
              <div className={grid}>
                {visible.map((o) => <OutageCard key={o.id} outage={o} />)}
              </div>
              {(hidden > 0 || showAll) && (
                <Button variant="outline" size="sm" className="gap-1.5 self-start"
                  onClick={() => setShowAll((v) => !v)}>
                  {showAll
                    ? <><ChevronUp className="size-3.5" /> Show fewer</>
                    : <><ChevronDown className="size-3.5" /> Show {hidden} more</>}
                </Button>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}
