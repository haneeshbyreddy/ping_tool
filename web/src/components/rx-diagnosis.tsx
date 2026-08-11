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
  tone?: "muted" | "warning"
}

function verdict(rx: RxStatusResponse): Verdict {
  const scrape = rx.scrape
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

const REFRESH_WATCH_MS = 150_000

export function RxFreshness({ device, canWrite }: {
  device: OrgDevice
  canWrite: boolean
}) {
  const queryClient = useQueryClient()
  const [awaiting, setAwaiting] = useState<string | null>(null)
  const startedAt = useRef(0)
  const pending = awaiting !== null
  const q = useQuery({
    queryKey: ["rx-status", device.id],
    queryFn: () => webOpticsApi.rxStatus(device.id),
    refetchInterval: pending ? 3_000 : 120_000,
  })
  const rx = q.data
  const scrape = rx?.scrape ?? null

  useEffect(() => {
    if (!pending) return
    if (scrape?.updated_at && scrape.updated_at !== awaiting) {
      setAwaiting(null)
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

export function RxDiagnosis({ device, compact }: {
  device: OrgDevice
  compact?: boolean
}) {
  const q = useQuery({
    queryKey: ["rx-status", device.id],
    queryFn: () => webOpticsApi.rxStatus(device.id),
    refetchInterval: 120_000,
  })
  if (q.isLoading || q.error || !q.data) return null
  const rx = q.data
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
