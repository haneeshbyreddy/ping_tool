import { Suspense, lazy, type ComponentType } from "react"
import { Loader2 } from "lucide-react"

const RELOAD_KEY = "wisp:chunk-reload"

function reloadedAlready(): boolean {
  try { return sessionStorage.getItem(RELOAD_KEY) === "1" } catch { return false }
}

function markReloaded(on: boolean): void {
  try {
    if (on) sessionStorage.setItem(RELOAD_KEY, "1")
    else sessionStorage.removeItem(RELOAD_KEY)
  } catch { /* private mode / quota */ }
}

function PageFallback() {
  return (
    <div className="flex flex-1 items-center justify-center p-12">
      <Loader2 className="size-5 animate-spin text-faint-foreground" />
    </div>
  )
}

// A deploy replaces the content-hashed chunks and the server keeps only the
// current build's assets, so a tab left open across one asks for a chunk that
// is gone and would white-screen. Reload ONCE — the new index.html carries the
// new hashes — and let the sessionStorage flag stop a genuinely broken chunk
// from becoming a reload loop. The flag clears on the next successful load, so
// a second deploy in the same session is still caught.
function route(load: () => Promise<Record<string, unknown>>, name: string) {
  const Lazy = lazy(async () => {
    try {
      const mod = await load()
      markReloaded(false)
      return { default: mod[name] as ComponentType }
    } catch (err) {
      if (reloadedAlready()) throw err
      markReloaded(true)
      window.location.reload()
      return new Promise<{ default: ComponentType }>(() => {})
    }
  })
  return function LazyRoute() {
    return (
      <Suspense fallback={<PageFallback />}>
        <Lazy />
      </Suspense>
    )
  }
}

export const TopologyPage = route(() => import("@/routes/topology-page"), "TopologyPage")
export const MapPage = route(() => import("@/routes/map-page"), "MapPage")
export const SettingsPage = route(() => import("@/routes/settings-page"), "SettingsPage")
export const SurveyPage = route(() => import("@/routes/survey-page"), "SurveyPage")
// Not a heavy page itself, but the ONLY eager route reaching subscriber-detail
// → map/drops → map/pins → leaflet, so leaving it eager preloads the whole map
// graph (412 kB) on a first paint that never draws a map.
export const CustomersPage = route(() => import("@/routes/customers-page"), "CustomersPage")
// Owner-only and visited about once a month, so its ledger tables and charts
// have no business in the first paint. It ships a DEFAULT export, hence the
// "default" name — the only route here that does, and the one thing tsc cannot
// check through route()'s Record<string, unknown>.
export const BillingPage = route(() => import("@/routes/billing-page"), "default")
