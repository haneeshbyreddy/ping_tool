import { useQuery } from "@tanstack/react-query"
import { useAuth } from "@/hooks/use-auth"
import { outagesApi, ApiError } from "@/lib/api"
import { OutageCard } from "@/components/outage-card"
import { UserMenu } from "@/components/layout/user-menu"
import { StatusDot } from "@/components/status-badge"
import { Skeleton } from "@/components/ui/skeleton"

// The field-worker view: the single screen a `worker` session sees, whatever
// the path (require-auth routes them here). Open issues + acknowledge +
// post-mortem, nothing else — the server-side _WORKER_ROUTES whitelist
// enforces the same boundary, this is just the matching chrome.
export function WorkerPage() {
  const { user, scopeOrg } = useAuth()
  const outages = useQuery({
    queryKey: ["outages", scopeOrg],
    queryFn: () => outagesApi.list(scopeOrg),
  })

  const list = outages.data?.outages ?? []
  const active = list.filter((o) => o.status !== "pending_postmortem")
  const pending = list.filter((o) => o.status === "pending_postmortem")

  return (
    <div className="flex min-h-svh flex-col">
      <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center justify-between border-b bg-background px-4">
        <span className="truncate text-sm font-semibold tracking-tight">
          {user?.org_name || user?.org_id}
        </span>
        <UserMenu />
      </header>

      <main className="mx-auto flex w-full max-w-xl flex-1 flex-col gap-4 p-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
        {outages.isLoading && <Skeleton className="h-24 w-full" />}
        {outages.isError && (
          <p className="text-sm text-destructive">
            {outages.error instanceof ApiError ? outages.error.message : "Failed to load issues"}
          </p>
        )}

        {outages.isSuccess && list.length === 0 && (
          <div className="flex items-center gap-3 rounded-lg border bg-card px-5 py-4 text-sm text-muted-foreground">
            <StatusDot tone="success" />
            All clear. No open issues.
          </div>
        )}

        {active.length > 0 && (
          <section className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <h2 className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">Open issues</h2>
              <span className="rounded-full border bg-card px-2 py-0.5 text-xs font-semibold">{active.length}</span>
            </div>
            {active.map((o) => <OutageCard key={o.id} outage={o} />)}
          </section>
        )}

        {pending.length > 0 && (
          <section className="flex flex-col gap-3">
            <h2 className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">Needs post-mortem</h2>
            {pending.map((o) => <OutageCard key={o.id} outage={o} />)}
          </section>
        )}
      </main>
    </div>
  )
}
