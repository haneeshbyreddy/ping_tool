// Why an OLT shows no per-ONU dBm, in a sentence an operator can act on.
//
// The optical counterpart of SnmpDiagnosis, and it exists for the same reason:
// a blank column is not a diagnosis. Several completely different causes render
// identically as an empty Rx column, and they take OPPOSITE actions —
//
//   * the vendor publishes no per-ONU Rx over SNMP at all (C-Data/DBC, proven
//     exhaustively twice) and no web-UI recipe covers it yet -> write a profile;
//   * a recipe exists but nobody has stored the OLT's web login -> type it;
//   * everything is configured and the scrape is failing -> read the detail;
//   * it works fine and this PON's ONUs are simply dark -> nothing to do.
//
// Reading the first as "this hardware has no optics" is the exact false
// negative the whole web-scrape subsystem was built to kill, and it is worth
// noticing that the way it used to get made was a human looking at a blank
// column. Facts come from the server; the sentence is composed here.
import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Gauge, RotateCw } from "lucide-react"
import { ApiError, webOpticsApi } from "@/lib/api"
import type { OrgDevice, RxStatusResponse } from "@/lib/types"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

interface Verdict {
  cause: string
  steps: string[]
  /** how loud to be: a coverage gap is not an alarm */
  tone?: "muted" | "warning"
}

function verdict(rx: RxStatusResponse): Verdict {
  const scrape = rx.scrape
  // The vendor is the hinge: without one, nothing downstream can even be asked.
  if (!rx.vendor) {
    return {
      cause: "This OLT's vendor hasn't been identified, so neither the SNMP "
        + "optics profile nor a web-UI recipe can be matched to it.",
      steps: [
        "Set the GPON vendor in the device's settings, or wait for the probe's "
          + "SNMP sweep to match it from the box's sysObjectID.",
        "If its SNMP walk isn't landing at all, fix that first. The roster has "
          + "to exist before a reading has anywhere to attach.",
      ],
    }
  }
  if (!rx.web_profile) {
    return {
      cause: `Neither this OLT's SNMP agent nor any configured web-UI recipe `
        + `provides per-ONU optical power for “${rx.vendor}”. On this hardware `
        + `class the readings often exist only on the OLT's own web page, which `
        + `needs a recipe before it can be read.`,
      steps: [
        "Open the OLT's optical page once through the web proxy. That records "
          + "its path, which is what a recipe is written from.",
        "Add the vendor under Settings → Monitoring → Web-UI optics vendors. "
          + "No probe update is needed; central does the reading.",
        rx.known_vendors.length
          ? `Recipes exist today for: ${rx.known_vendors.join(", ")}.`
          : "No web-UI recipes are configured yet.",
      ],
    }
  }
  // A recipe exists — so from here on, everything is a configuration or a fault.
  if (!rx.has_node) {
    return {
      cause: "No probe is assigned to this OLT, and the reading is fetched "
        + "through the probe's tunnel.",
      steps: ["Assign a probe in the device's settings."],
    }
  }
  if (!rx.web_proxy) {
    return {
      cause: `A recipe for “${rx.web_profile}” is configured, but this `
        + "organization doesn't have the web-proxy capability, which is how "
        + "central reaches the OLT's page.",
      steps: ["Ask the platform admin to enable web proxy for this organization."],
    }
  }
  if (!rx.has_credentials) {
    return {
      cause: `A recipe for “${rx.web_profile}” is configured, but it has no login `
        + "for this OLT, so it has never asked the box for a reading.",
      steps: [
        "Store the OLT's web-UI username and password in this device's panel "
          + "(Health tab → device web UI).",
        "Readings appear on the next sweep, within about 15 minutes.",
      ],
      tone: "warning",
    }
  }
  if (!scrape) {
    return {
      cause: `Everything is configured for “${rx.web_profile}”, but no reading `
        + "attempt has been recorded yet.",
      steps: ["The sweep runs about every 15 minutes; give it one pass."],
    }
  }
  switch (scrape.state) {
    case "ok":
    case "partial":
      // The scrape worked, yet nothing landed on the roster. Almost always the
      // merge, not the read: readings attach to ONLINE slots by MAC, and a MAC
      // on two live slots is dropped rather than pinned to the wrong drop.
      return {
        cause: `The last read of this OLT's optical page succeeded `
          + `(${scrape.rows} row${scrape.rows === 1 ? "" : "s"}, ${ago(scrape.updated_at)}) `
          + "but none of it matched a slot in the SNMP roster.",
        steps: [
          "Readings attach to ONLINE ONUs by MAC address. If the roster is "
            + "stale, or a MAC appears on two live slots, the reading is dropped "
            + "rather than attributed to the wrong subscriber.",
          "Check the SNMP optical walk is landing. The roster is what a "
            + "reading merges onto.",
        ],
        tone: "warning",
      }
    case "login":
      return {
        cause: `The OLT refused the stored login: ${scrape.detail ?? "no detail"}.`,
        steps: [
          "Re-enter the OLT's web password in this device's panel.",
          "This firmware allows ONE web session, so a password that works in a "
            + "browser can still fail here if someone is logged in.",
        ],
        tone: "warning",
      }
    case "unreachable":
      return {
        cause: `The OLT's web page couldn't be reached: ${scrape.detail ?? "no detail"}.`,
        steps: [
          "Check the web address override in the device's settings. The page "
            + "isn't always on the probe address, port 80.",
          "Confirm the probe's tunnel is up (Network → Probes).",
        ],
        tone: "warning",
      }
    case "no_credentials":
    case "no_profile":
      return {
        cause: scrape.detail ?? "The reader is not configured for this OLT.",
        steps: ["Set it up under Settings → Monitoring → Web-UI optics vendors."],
      }
    case "skipped":
      return {
        cause: `The last pass skipped this OLT: ${scrape.detail ?? "no detail"}.`,
        steps: [
          "This is normal and self-correcting. The reader waits rather than "
            + "competing with an operator or a dormant tunnel.",
        ],
      }
    default:
      return {
        cause: `The last read failed: ${scrape.detail ?? "unknown error"}.`,
        steps: ["A persistent failure usually means the address or the "
          + "credentials, not the fibre."],
        tone: "warning",
      }
  }
}

/** How long a manual read is given before we stop watching for its outcome.
    Comfortably past the server's own per-OLT budget (120s) — the spinner has
    to outlive the work, or a slow 8-PON box looks like a failure it isn't. */
const REFRESH_WATCH_MS = 150_000

/** Last web-UI read of this OLT, plus the button that asks for another.
 *
 * Two facts the Optical panel could not show before, and the first is the one
 * that makes the second safe: a dBm on screen carries no date, so "these are
 * yesterday's numbers" and "this was measured four minutes ago" looked
 * identical while a tech decided whether a splice had helped.
 *
 * The read itself stays exactly as restrained as the sweep — same eligibility,
 * same per-OLT lock, same standing down for an operator who is browsing the
 * box. What the button changes is only WHO may ask and WHEN, because the
 * quarter-hour clock is right for a value that drifts over days and wrong for
 * the ten minutes someone is standing at a pole with the fibre in their hand.
 */
export function RxFreshness({ device, canWrite }: {
  device: OrgDevice
  /** owner/superadmin: the read spends the OLT's stored admin login */
  canWrite: boolean
}) {
  const queryClient = useQueryClient()
  // The scrape stamp we started from. A read is "landed" when the recorded
  // outcome moves off it — the SAME status the sweep writes, so a manual read
  // and a swept one are reported through one channel rather than the button
  // owning a private notion of success.
  const [awaiting, setAwaiting] = useState<string | null>(null)
  const startedAt = useRef(0)
  const pending = awaiting !== null
  const q = useQuery({
    queryKey: ["rx-status", device.id],
    queryFn: () => webOpticsApi.rxStatus(device.id),
    // Idle, this shares RxDiagnosis's slow poll — the scrape's own clock is
    // ~15 minutes and anything faster watches a value that cannot have moved.
    // While a read we asked for is in flight, watch properly: the operator is
    // standing there waiting for it. (react-query keys the cache, not the
    // interval, so the two observers coexist without a second fetch.)
    refetchInterval: pending ? 3_000 : 120_000,
  })
  const rx = q.data
  const scrape = rx?.scrape ?? null

  useEffect(() => {
    if (!pending) return
    if (scrape?.updated_at && scrape.updated_at !== awaiting) {
      setAwaiting(null)
      // New readings only reach the roster through the optics fold, so the
      // panel's own data has to be re-fetched — a landed scrape that leaves
      // the dBm column showing the old numbers is worse than no button.
      queryClient.invalidateQueries({ queryKey: ["optics", device.id] })
      queryClient.invalidateQueries({ queryKey: ["inventory"] })
      const ok = scrape.state === "ok" || scrape.state === "partial"
      if (ok) {
        toast.success(`Read ${device.name}: ${scrape.rows} reading${scrape.rows === 1 ? "" : "s"}`)
      } else {
        toast.warning(`Couldn't read ${device.name}`, {
          description: scrape.detail ?? scrape.state, duration: 10_000,
        })
      }
      return
    }
    // A read that never records an outcome (central restarted mid-scrape, say)
    // must not leave a spinner turning forever. Armed against the ELAPSED time,
    // not a fresh full delay: the poll above re-runs this effect every few
    // seconds, and a timer restarted each pass would never fire at all.
    const left = REFRESH_WATCH_MS - (Date.now() - startedAt.current)
    const t = setTimeout(() => {
      setAwaiting(null)
      toast.error(`No result from ${device.name} yet. It may still be reading.`)
    }, Math.max(0, left))
    return () => clearTimeout(t)
  }, [pending, awaiting, scrape, device.id, device.name, queryClient])

  const start = useMutation({
    mutationFn: () => webOpticsApi.refresh(device.id),
    onMutate: () => {
      startedAt.current = Date.now()
      setAwaiting(scrape?.updated_at ?? "")
    },
    onError: (e) => {
      setAwaiting(null)
      toast.error(e instanceof ApiError ? e.message : "Couldn't start the read")
    },
  })
  if (!rx) return null
  // The server is the authority on "a read is running" — the sweep's own pass
  // counts, and it is not this browser that started it.
  const busy = pending || rx.refreshing || start.isPending
  if (!rx.can_refresh && !scrape) return null   // nothing true to say yet

  return (
    <span className="flex items-center gap-1.5">
      <RxReadStamp rx={rx} busy={busy} />
      {canWrite && rx.can_refresh && (
        <Button variant="ghost" size="sm" disabled={busy}
          className="-my-1 h-6 gap-1 px-1.5 text-2xs"
          title={busy
            ? "Reading this OLT's optical page…"
            : "Read this OLT's optical page now instead of waiting for the next sweep"}
          onClick={() => start.mutate()}>
          <RotateCw className={cn("size-3", busy && "animate-spin")} />
          {busy ? "reading" : "refresh"}
        </Button>
      )}
    </span>
  )
}

/** When the dBm on screen was last actually measured.
    Three states, never collapsed: a good read, a read that USED to work (the
    single most useful line on a broken pipeline), and one that never has. */
function RxReadStamp({ rx, busy }: { rx: RxStatusResponse; busy: boolean }) {
  const scrape = rx.scrape
  if (busy) return <span className="text-faint-foreground">reading…</span>
  const ok = scrape && (scrape.state === "ok" || scrape.state === "partial")
  if (ok) {
    return (
      <span className="text-faint-foreground"
        title={`The OLT's own optical page was read ${ago(scrape.updated_at)}`}>
        dBm read {ago(scrape.updated_at)}
      </span>
    )
  }
  if (scrape?.last_ok_at) {
    return (
      <span className="font-semibold text-warning"
        title={`The last attempt failed (${scrape.detail ?? scrape.state}). These dBm figures are from the last read that worked.`}>
        dBm read {ago(scrape.last_ok_at)} · failing since
      </span>
    )
  }
  if (!rx.can_refresh) return null   // the diagnosis block says why
  return <span className="text-faint-foreground">dBm never read</span>
}

/** Rendered by the Optical panel when an OLT reports no per-ONU Rx. */
export function RxDiagnosis({ device, compact }: {
  device: OrgDevice
  /** inline note inside a PON drill-down rather than a standalone block */
  compact?: boolean
}) {
  const q = useQuery({
    queryKey: ["rx-status", device.id],
    queryFn: () => webOpticsApi.rxStatus(device.id),
    // Slower than the SNMP diagnosis: the scrape's own clock is ~15 minutes, so
    // anything faster is polling a value that cannot have changed.
    refetchInterval: 120_000,
  })
  if (q.isLoading || q.error || !q.data) return null
  const rx = q.data
  // Readings ARE landing — this component has nothing to explain.
  if (rx.onus_rx > 0 && !compact) return null
  const v = verdict(rx)

  if (compact) {
    return (
      <p className="text-2xs text-faint-foreground">
        {rx.onus_rx > 0
          ? `${rx.onus_rx} of ${rx.onus_total} ONUs report optical power.`
          : v.cause}
      </p>
    )
  }
  return (
    <div className={cn("flex flex-col gap-2 rounded-lg border px-3 py-2.5",
      v.tone === "warning" ? "border-warning/40 bg-warning-soft/30" : "bg-muted/40")}>
      <p className="flex items-start gap-2 text-xs text-foreground">
        <Gauge className={cn("mt-0.5 size-3.5 shrink-0",
          v.tone === "warning" ? "text-warning" : "text-muted-foreground")} />
        <span>
          <span className="font-semibold">No per-ONU dBm on this OLT.</span>{" "}
          {v.cause}
        </span>
      </p>
      {v.steps.length > 0 && (
        <ol className="flex list-decimal flex-col gap-0.5 pl-8 text-xs text-muted-foreground">
          {v.steps.map((s, i) => <li key={i}>{s}</li>)}
        </ol>
      )}
      {/* The facts behind the sentence, so an operator can check our reasoning
          rather than take the verdict on trust. */}
      <p className="pl-8 font-mono text-[0.6875rem] text-faint-foreground">
        {rx.vendor
          ? <>vendor {rx.vendor} ({rx.vendor_source})</>
          : <>vendor unidentified</>}
        {rx.web_profile && <> · recipe {rx.web_profile}</>}
        {rx.scrape?.last_ok_at && <> · last read {ago(rx.scrape.last_ok_at)}</>}
        {rx.onus_total > 0 && <> · {rx.onus_rx}/{rx.onus_total} ONUs measured</>}
      </p>
    </div>
  )
}
