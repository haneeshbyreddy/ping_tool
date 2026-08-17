import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router-dom"
import { ArrowDown, ArrowUp, ChevronsUpDown, MapPin, Search } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { useDebounced } from "@/hooks/use-debounced"
import { useNow } from "@/hooks/use-now"
import { ApiError, customersApi } from "@/lib/api"
import type { CustomerRow, CustomersReply } from "@/lib/types"
import { ago } from "@/lib/format"
import { NeedsOrg } from "@/components/needs-org"
import { Chip } from "@/components/status-badge"
import { statusLine } from "@/components/radius-card"
import { SubscriberDialog } from "@/components/subscriber-detail"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

const COLS = "grid grid-cols-[0.5rem_minmax(0,1.5fr)_minmax(0,1.3fr)_6rem] items-center gap-3.5 px-4 md:grid-cols-[0.5rem_minmax(0,1.5fr)_minmax(0,0.8fr)_6rem_minmax(0,1.3fr)]"

type Filter = "dark" | "frozen" | "expiring" | "active" | "expired"
  | "inactive" | "online" | "unmatched"

// Every chip count is a recount of the rows it filters to, so a chip and the
// list it opens can never disagree — the /issues rule, kept here.
const FILTERS: Record<Filter, (r: CustomerRow) => boolean> = {
  dark: (r) => r.status === "active" && r.net === "dark",
  frozen: (r) => r.status === "active" && r.net === "frozen",
  expiring: (r) => r.status === "active" && r.days_left != null
    && r.days_left >= 0 && r.days_left <= 7,
  active: (r) => r.status === "active",
  expired: (r) => r.status === "expired",
  inactive: (r) => r.status === "inactive",
  // The ONU is up right now. Deliberately NOT narrowed to active billing: an
  // expired customer still passing traffic is a real thing to find, and the
  // status chips already answer the billing question.
  online: (r) => r.net === "online",
  // ACTIVE-ONLY, and that is the point of the chip. An expired customer passes
  // no traffic, so their router ages out of the OLT's address table and cannot
  // match — on Hansa that is 475 of the 542 unmatched rows, i.e. the number was
  // reporting the design working as if it were a fault. What is actionable is
  // the paying customer we cannot find on the network.
  unmatched: (r) => r.status === "active" && r.net === "unlinked",
}

const FILTER_KEYS = Object.keys(FILTERS) as Filter[]

const isFilter = (raw: string | null): raw is Filter =>
  raw != null && raw in FILTERS

// Sort is a VIEW preference, so it lives in local state while the filter rides
// the URL (tiles link to a chip; nothing links to an order).
type Sort = "name" | "expiry-asc" | "expiry-desc"

// Ordered by the NUMBER THE COLUMN PRINTS, not by the expiry date behind it
// (operator's call 2026-08-15). Sorting on the raw `days_left` is chronological
// and reads as broken: an expired customer's is negative, so ascending led with
// 1262 d ago and buried every customer actually about to lapse under ~500 dead
// accounts — on Hansa, 687 of 1779 rows are expired. Ascending now means the
// SMALLEST printed figure first ("today", "in 1 d", …), which is also the one
// question this column gets sorted to answer: who runs out next.
//
// Expired rows sink BELOW live ones in both directions rather than
// interleaving: "3 d ago" and "in 3 d" print the same magnitude and mean
// opposite things, and a list alternating between them answers neither. They
// keep the same lowest-first rule among themselves, so filtering to Expired
// leads with the ones that lapsed most recently — the ones still worth a call.
// Keyed on `days_left < 0`, NOT on `status`: the rule is about what is printed,
// and the panel's own status can disagree with the date it gave us.
//
// A row with no `days_left` sorts LAST in BOTH directions: its panel declared
// no `date_format`, or the string did not parse, and neither is a claim about
// when this customer runs out. Name breaks ties, so the order is stable and
// matches the server's own default.
function bySort(sort: Sort) {
  // Sorted on what the row PRINTS BIG, which is now the username — a list
  // ordered by a string the eye cannot find reads as unsorted.
  const label = (r: CustomerRow) => (r.username || r.name || "").toLowerCase()
  return (a: CustomerRow, b: CustomerRow) => {
    if (sort !== "name") {
      const x = a.days_left, y = b.days_left
      if (x == null || y == null) {
        if (x != null || y != null) return x == null ? 1 : -1
      } else {
        const gone = (n: number) => (n < 0 ? 1 : 0)
        if (gone(x) !== gone(y)) return gone(x) - gone(y)
        if (Math.abs(x) !== Math.abs(y)) {
          return sort === "expiry-asc"
            ? Math.abs(x) - Math.abs(y)
            : Math.abs(y) - Math.abs(x)
        }
      }
    }
    return label(a).localeCompare(label(b))
  }
}

function expiresCell(r: CustomerRow): { text: string; tone?: "warning" } {
  if (r.days_left == null) return { text: r.expiry ?? "—" }
  if (r.days_left < 0) return { text: `${-r.days_left} d ago` }
  if (r.days_left === 0) return { text: "today", tone: "warning" }
  if (r.days_left <= 7) return { text: `in ${r.days_left} d`, tone: "warning" }
  return { text: `in ${r.days_left} d` }
}

function NetDot({ r }: { r: CustomerRow }) {
  if (r.net === "unlinked") {
    return <span className="size-2 rounded-full border border-border-strong"
      title="Not matched to any ONU" />
  }
  const cls = r.net === "online" ? "bg-success"
    : r.net === "dark" && r.status === "active" ? "bg-destructive"
    : "bg-muted-foreground/50"
  return <span className={cn("size-2 rounded-full", cls)} />
}

function NetCell({ r, reasons }: { r: CustomerRow; reasons: Record<string, string> }) {
  if (r.net === "unlinked") {
    return (
      <span className="truncate text-2xs text-faint-foreground"
        title={r.reason ? reasons[r.reason] : undefined}>
        Not matched{r.reason ? ` — ${reasons[r.reason] ?? r.reason}` : ""}
      </span>
    )
  }
  const where = [r.device_name, r.pon_port].filter(Boolean).join(" · ")
  const word = r.net === "online" ? "Online"
    : r.net === "dark" ? (r.dark_since ? `Dark ${ago(r.dark_since)}` : "Dark")
    : r.net === "frozen" ? "OLT down" : "Walk stale"
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span className={cn("truncate text-xs",
        r.net === "dark" && r.status === "active" ? "font-medium text-destructive"
          : r.net === "online" ? "text-muted-foreground" : "text-faint-foreground")}
        title={`${word}${where ? ` · ${where}` : ""}`}>
        {word}{where && <span className="text-faint-foreground"> · {where}</span>}
      </span>
      {r.located && (
        <MapPin className="size-3 shrink-0 text-faint-foreground"
          aria-label="Placed on the map" />
      )}
    </span>
  )
}

function TriageChips({ all, picked, setPicked }: {
  all: CustomerRow[]
  picked: Filter | null; setPicked: (f: Filter | null) => void
}) {
  const counts = useMemo(() => {
    const out = Object.fromEntries(FILTER_KEYS.map((f) => [f, 0])) as Record<Filter, number>
    for (const r of all) for (const f of FILTER_KEYS) if (FILTERS[f](r)) out[f]++
    return out
  }, [all])
  const chip = (f: Filter, label: string, hot?: boolean) => {
    const n = counts[f]
    if (n === 0 && (f === "dark" || f === "frozen" || f === "expiring"
      || f === "inactive")) return null
    return (
      <button key={f} type="button" aria-pressed={picked === f}
        onClick={() => setPicked(picked === f ? null : f)}
        className={cn(
          "inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors",
          picked === f
            ? "border-border-strong bg-popover text-foreground"
            : "border-border bg-card text-muted-foreground hover:text-foreground",
          hot && n > 0 && picked !== f && "text-destructive hover:text-destructive")}>
        {label}
        <span className={cn("font-mono text-2xs",
          hot && n > 0 ? "font-semibold text-destructive" : "text-faint-foreground")}>
          {n}
        </span>
      </button>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <button type="button" aria-pressed={picked === null}
        onClick={() => setPicked(null)}
        className={cn(
          "h-7 rounded-md border px-2.5 text-xs font-medium transition-colors",
          picked === null
            ? "border-border-strong bg-popover text-foreground"
            : "border-border bg-card text-muted-foreground hover:text-foreground")}>
        All {all.length}
      </button>
      {chip("dark", "Paying & dark", true)}
      {chip("frozen", "Behind a down OLT")}
      {chip("expiring", "Expiring ≤ 7 d")}
      {chip("active", "Active")}
      {chip("expired", "Expired")}
      {chip("inactive", "Inactive")}
      {chip("online", "Online")}
      {chip("unmatched", "Active, not matched")}
    </div>
  )
}

function PanelBanners({ data }: { data: CustomersReply }) {
  const bad = data.panels.filter((p) => p.state !== "ok")
  if (!bad.length) return null
  return (
    <div className="flex flex-wrap gap-1.5">
      {bad.map((p) => {
        const st = statusLine(p)
        return (
          <Chip key={p.account_id} tone={st.tone}>
            {data.panels.length > 1 ? `${p.profile}: ` : ""}{st.text}
          </Chip>
        )
      })}
    </div>
  )
}

export function CustomersPage() {
  const { scopeOrg } = useAuth()
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState("")
  const [sort, setSort] = useState<Sort>("name")
  const [open, setOpen] = useState<string | null>(null)
  useNow()

  const raw = params.get("f")
  const picked: Filter | null = isFilter(raw) ? raw : null
  const setPicked = (f: Filter | null) => {
    const next = new URLSearchParams(params)
    if (f) next.set("f", f)
    else next.delete("f")
    setParams(next, { replace: true })
  }

  const query = useQuery({
    queryKey: ["customers", scopeOrg],
    queryFn: () => customersApi.list(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 120_000,
  })

  const all = useMemo(() => query.data?.customers ?? [], [query.data])
  const reasons = useMemo(() => query.data?.reasons ?? {}, [query.data])
  // The page re-renders on the useNow tick and on every keystroke; the row pass
  // depends on neither the clock nor the raw input, so it is memoized off the
  // DEBOUNCED needle and recomputed only when what it reads actually moves.
  const needle = useDebounced(search.trim().toLowerCase(), 200)
  const rows = useMemo(() => all.filter((r) =>
    (!picked || FILTERS[picked](r)) &&
    (!needle || [r.name, r.username, r.mobile, r.acno, r.package, r.branch,
      r.area, r.address, r.device_name, r.onu_label, r.onu_name]
      .some((v) => (v ?? "").toLowerCase().includes(needle))))
    .sort(bySort(sort)), [all, picked, needle, sort])
  const multiPanel = useMemo(
    () => new Set(all.map((r) => r.account_id)).size > 1, [all])

  if (!scopeOrg) return <NeedsOrg />

  const counts = query.data?.counts
  const panels = query.data?.panels ?? []
  // last_ok_at, never updated_at: "read Nm ago" is a claim a SUCCESSFUL read
  // happened — a panel failing hourly updates its status just as often.
  const lastRead = panels.map((p) => p.last_ok_at).filter(Boolean).sort().at(-1)

  return (
    <div className="wisp-page flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold tracking-tight">Customers</h1>
        {counts && (
          <span className="text-xs text-faint-foreground">
            {counts.customers} in billing · {counts.active} active
            · {counts.linked} on the network
            {lastRead && ` · read ${ago(lastRead)}`}
          </span>
        )}
        <div className="relative ml-auto w-full sm:w-72">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="name, number, account, OLT…"
            className="h-8 bg-muted pl-8 text-xs" />
        </div>
      </div>

      {query.data && <PanelBanners data={query.data} />}
      {query.data && <TriageChips all={all} picked={picked} setPicked={setPicked} />}

      <div className="wisp-panel">
        <div className={cn(COLS, "wisp-thead h-9")}>
          <span />
          <span>Customer</span>
          <span className="hidden md:block">Package</span>
          <span className="flex justify-end">
            <button type="button"
              onClick={() => setSort(sort === "expiry-asc" ? "expiry-desc"
                : sort === "expiry-desc" ? "name" : "expiry-asc")}
              title={sort === "expiry-asc"
                ? "Running out soonest first, expired below — click to reverse"
                : sort === "expiry-desc"
                  ? "Furthest out first — click to go back to name order"
                  : "Sort by days to expiry"}
              className={cn("-mr-1 inline-flex items-center gap-1 rounded px-1",
                "transition-colors hover:text-foreground",
                sort !== "name" && "text-foreground")}>
              Expires
              {sort === "expiry-asc" ? <ArrowUp className="size-3" />
                : sort === "expiry-desc" ? <ArrowDown className="size-3" />
                : <ChevronsUpDown className="size-3 opacity-50" />}
            </button>
          </span>
          <span>Network</span>
        </div>
        {query.isLoading && <div className="p-4"><Skeleton className="h-40 w-full" /></div>}
        {query.isError && (
          <p className="p-8 text-center text-xs text-destructive">
            {query.error instanceof ApiError ? query.error.message : "Could not load customers"}
          </p>
        )}
        {query.isSuccess && all.length === 0 && (
          <div className="p-8 text-center text-xs text-faint-foreground">
            {panels.length === 0 ? (
              <>
                No billing panel is connected, so there is no customer list to
                show.{" "}
                <Link to="/settings/monitoring" className="text-muted-foreground underline underline-offset-2 hover:text-foreground">
                  Connect one in Settings → Monitoring.
                </Link>
              </>
            ) : (
              "The billing panel has not handed over a customer list yet."
            )}
          </div>
        )}
        {query.isSuccess && all.length > 0 && rows.length === 0 && (
          <p className="p-8 text-center text-xs text-faint-foreground">
            Nothing matches the current filter.
          </p>
        )}
        {rows.map((r) => {
          const exp = expiresCell(r)
          const clickable = !!r.onu_mac
          return (
            <div key={`${r.account_id}:${r.username}`}
              role={clickable ? "button" : undefined}
              onClick={clickable ? () => setOpen(r.onu_mac) : undefined}
              className={cn(COLS, "h-11 wisp-row transition-colors",
                clickable && "cursor-pointer hover:bg-foreground/5")}>
              <NetDot r={r} />
              <span className="min-w-0">
                {/* USERNAME LEADS (the ISPs' call, 2026-08-17: it is the only
                    identifier everyone recognises; the customer name is extra
                    info, and on three of the four live books it is the weaker
                    string — 1,442 of rapidnetworks' 1,784 names are a single
                    word). MONO and verbatim: this is a key somebody retypes
                    into the billing panel, so it may not be case-folded the way
                    a survey label is, and mono is this app's mark for "an
                    identifier, not prose" (a port is GE0/5, a device row leads
                    with its name). The status word stays on this line — it
                    qualifies the account, not the person. */}
                <span className="block truncate font-mono text-xs font-medium">
                  {r.username}
                  {r.status !== "active" && (
                    <span className={cn("ml-1.5 font-sans text-2xs font-normal",
                      r.status === "expired" ? "text-warning" : "text-faint-foreground")}>
                      {r.status}
                    </span>
                  )}
                </span>
                <span className="block truncate text-2xs text-faint-foreground">
                  {r.name}
                  {multiPanel && r.account_label && ` · ${r.account_label}`}
                  {!r.in_last_read && (
                    <span title={r.last_seen_at
                      ? `Last seen in a billing read ${ago(r.last_seen_at)}`
                      : undefined}> · gone from billing</span>
                  )}
                </span>
              </span>
              <span className="hidden min-w-0 truncate text-xs text-muted-foreground md:block"
                title={r.package ?? undefined}>
                {r.package ?? "—"}
              </span>
              <span className={cn("text-right font-mono text-xs",
                exp.tone === "warning" ? "font-medium text-warning" : "text-faint-foreground")}
                title={r.expiry ?? undefined}>
                {exp.text}
              </span>
              <NetCell r={r} reasons={reasons} />
            </div>
          )
        })}
      </div>

      {counts && counts.customers > 0 && (
        <p className="text-2xs text-faint-foreground">
          {counts.linked} of {counts.customers} customers are matched to an ONU.
          An unmatched customer is usually a disconnected one — their router
          stops passing traffic, so its address ages out of the OLT's table.
          {counts.superseded > 0 && (
            <> {counts.superseded} older {counts.superseded === 1 ? "row is" : "rows are"} not
            shown: the same customer is current in another panel.</>
          )}
        </p>
      )}

      {open && <SubscriberDialog mac={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
