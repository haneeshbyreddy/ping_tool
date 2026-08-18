import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Zap } from "lucide-react"
import { snmpApi, ApiError } from "@/lib/api"
import { runSnmpTest } from "@/components/snmp-test"
import type {
  DeviceCapability, OrgDevice, SnmpSubsystem, SnmpSubsystemStatus,
} from "@/lib/types"
import { useAuth } from "@/hooks/use-auth"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

const SUBSYSTEM_NOUN: Record<SnmpSubsystem, string> = {
  health: "CPU/RAM/temperature readings",
  ports: "port table",
  optics: "ONU optical readings",
}

// How long the last walk took, measured on the probe itself. ONE formatter, so
// the ports header, the optical header and the diagnosis card can't print the
// same measurement three ways. null in means null out: a probe on an older
// build reports no duration at all, and "we never measured it" may not render
// as "it took no time" — the panels drop the chip entirely rather than show 0s.
export function walkSecs(s: number | null | undefined): string | null {
  if (s == null || !Number.isFinite(s)) return null
  return s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`
}

interface Diagnosis {
  cause: string
  steps: string[]
  tone?: "warning"
  notSupported?: boolean
}

// Reading a new box is a platform-admin job (a vendor profile, written from a
// walk run off the CLI), so a step for it names WHO does it. There is no walk
// or wizard in the UI to point at any more, and this may not claim anybody has
// been told: nothing here sends a notification.
const ADMIN_ADDS_SUPPORT =
  "Support for a new box is added by the platform admin as a vendor profile. "
  + "It reaches your probes as data, with no update to install."

function diagnose(subsystem: SnmpSubsystem, st: SnmpSubsystemStatus | undefined): Diagnosis {
  if (!st) {
    return {
      cause: "No diagnosis from the probe yet. It reports one with every SNMP sweep.",
      steps: [
        "Wait one sweep (~2 minutes) after enabling SNMP.",
        "If this never fills in, the assigned probe is likely on an older agent build. Update it from Network → Probes.",
      ],
    }
  }
  switch (st.state) {
    case "ok":
      return {
        cause: `The last sweep succeeded${st.item_count != null ? ` (${st.item_count} item${st.item_count === 1 ? "" : "s"})` : ""} ${ago(st.updated_at)}. Data should appear shortly.`,
        steps: [],
      }
    case "partial":
      // Not "ok": the walk answered for some columns and ran out of budget
      // before the rest. What arrived is current, what didn't holds its last
      // complete reading and then blanks — never a green light over a gap.
      return {
        cause: `The last sweep ran out of budget before the whole ${subsystem === "ports" ? "interface table" : "walk"}: ${st.detail ?? "some columns didn't arrive"}. What did arrive is current ${ago(st.updated_at)}; the dropped columns hold their last complete reading and go blank once that ages out.`,
        steps: subsystem === "ports"
          ? [
              "A rate is a delta between two walks, so bandwidth stays blank until two consecutive sweeps both carry the counters.",
              "Usually a very large ifTable on a slow agent. Persistent partials on one box are worth reporting. The walk budget is tunable per subsystem.",
            ]
          : [
              "Usually a very large table on a slow agent. Persistent partials are worth reporting. The walk budget is tunable per subsystem.",
            ],
        tone: "warning",
      }
    case "no_response":
      return {
        cause: "The device never answered SNMP. The probe's queries go unanswered, so the fix is on the device itself.",
        steps: [
          "Check the SNMP agent is enabled on the device (many ship with it off).",
          "Check the community string matches what's configured here.",
          "Check any SNMP ACL/allowed-hosts list on the device includes the probe's IP.",
        ],
      }
    case "timeout":
      return {
        cause: "The device answers, but the walk ran past its time budget, usually a very large table or a slow agent. The probe retries on every sweep.",
        steps: [
          "If this device worked before, it may be overloaded. Check its CPU.",
          "Persistent timeouts on a big OLT/switch are worth reporting. The walk budget is tunable per subsystem.",
        ],
      }
    case "no_profile":
      return {
        cause: `No GPON vendor profile claims this OLT (sysObjectID ${st.sysobjectid ?? "unknown"}). Optics stay off rather than guessing another vendor's OIDs.`,
        steps: [
          "If this is a known vendor under an odd sysObjectID, set the GPON vendor override in the device's settings.",
          `Otherwise this OLT is hardware we don't read yet. ${ADMIN_ADDS_SUPPORT}`,
        ],
        notSupported: true,
      }
    case "empty":
      if (subsystem === "health") {
        return st.profile
          ? {
              cause: `Profile “${st.profile}” matched this device but returned no readings. Its OIDs are probably wrong for this exact model.`,
              steps: [
                "Nothing on the device is misconfigured. It answers, and the reading is missing at our end.",
                `The profile is corrected by the platform admin for this exact model. ${ADMIN_ADDS_SUPPORT}`,
              ],
            }
          : {
              cause: "The device answers SNMP but exposes none of the standard health OIDs. Cheap gear usually hides CPU/RAM/temperature in its private vendor tree.",
              steps: [
                `Reading vitals off a box like this needs a profile for its private tree. ${ADMIN_ADDS_SUPPORT}`,
                "If this hardware has no such sensors at all, mark it not supported so it stops counting as a gap.",
              ],
              notSupported: true,
            }
      }
      if (subsystem === "ports") {
        return {
          cause: "The device answers SNMP but its interface table (ifTable) came back empty. Some gear simply doesn't expose ports over SNMP.",
          steps: [
            "If the hardware genuinely has no port table, mark ports as not supported so it stops showing as a gap.",
          ],
          notSupported: true,
        }
      }
      return {
        cause: "The OLT answers SNMP and a vendor profile matched, but its ONU table came back empty.",
        steps: [
          "If no ONUs are registered yet this is normal.",
          `Otherwise the vendor profile may not fit this exact model. ${ADMIN_ADDS_SUPPORT}`,
        ],
      }
    case "error":
    default:
      return {
        cause: `The SNMP sweep failed: ${st.detail ?? "unknown error"}.`,
        steps: ["Transient errors clear on the next sweep. A persistent one usually means a network path or device problem."],
      }
  }
}

function NotSupportedDialog({ device, subsystem, open, onOpenChange }: {
  device: OrgDevice; subsystem: SnmpSubsystem
  open: boolean; onOpenChange: (o: boolean) => void
}) {
  const qc = useQueryClient()
  const [note, setNote] = useState("")
  const save = useMutation({
    mutationFn: () => snmpApi.setCapability({
      device_id: device.id, subsystem, supported: false, note: note.trim() || null }),
    onSuccess: () => {
      toast.success("Marked not supported")
      qc.invalidateQueries({ queryKey: ["snmp-status", device.id] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to save"),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Not supported on this hardware?</DialogTitle>
          <DialogDescription>
            Records that {device.name} can't provide {SUBSYSTEM_NOUN[subsystem]} over SNMP.
            The coverage overview stops counting it as a problem. You can undo this anytime.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5">
          <Label>Why? (optional, shown to teammates)</Label>
          <Input value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. vendor confirmed no temperature sensor" />
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
            Mark not supported
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function UnsupportedNote({ device, cap }: { device: OrgDevice; cap: DeviceCapability }) {
  const qc = useQueryClient()
  const { canWrite } = useAuth()
  const undo = useMutation({
    mutationFn: () => snmpApi.setCapability({
      device_id: device.id, subsystem: cap.subsystem, supported: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snmp-status", device.id] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Failed to undo"),
  })
  return (
    <div className="rounded-lg border bg-muted/40 px-3 py-2.5 text-xs text-muted-foreground">
      <p>
        <span className="font-semibold text-foreground">Not supported on this hardware</span>
        {cap.note && <> · {cap.note}</>}
        {cap.updated_by && <span className="text-faint-foreground"> · {cap.updated_by}, {ago(cap.updated_at)}</span>}
      </p>
      {canWrite && (
        <button className="mt-1 text-2xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          onClick={() => undo.mutate()} disabled={undo.isPending}>
          Undo (start flagging this again)
        </button>
      )}
    </div>
  )
}

export function SnmpDiagnosis({ device, subsystem }: {
  device: OrgDevice
  subsystem: SnmpSubsystem
}) {
  // "Test SNMP" is the one tool left here, and it stays with the owner: it
  // answers "does this box reply to us", and every fix it points at (community
  // string, source-IP ACL, UDP 161 through NAT) is the ISP's own to make. Raw
  // OID walking and profile authoring left the UI entirely.
  const { canWrite } = useAuth()
  const [nsOpen, setNsOpen] = useState(false)
  const q = useQuery({
    queryKey: ["snmp-status", device.id],
    queryFn: () => snmpApi.status(device.id),
    refetchInterval: 60_000, // diagnoses move on the SNMP sweep cadence
  })

  if (device.snmp_enabled !== 1) {
    return (
      <p className="rounded-lg border bg-muted/40 px-3 py-2.5 text-xs text-muted-foreground">
        SNMP is off for this device. Enable it (with a community string) in the device's
        settings to collect its {SUBSYSTEM_NOUN[subsystem]}.
      </p>
    )
  }
  if (q.isLoading) return null
  if (q.error) {
    return (
      <p className="rounded-lg border border-destructive/30 bg-destructive-soft/40 px-3 py-2 text-xs text-destructive">
        Couldn't load the SNMP diagnosis ({q.error instanceof Error ? q.error.message : "request failed"}).
      </p>
    )
  }

  const cap = (q.data?.capability ?? []).find((c) => c.subsystem === subsystem && !c.supported)
  if (cap) return <UnsupportedNote device={device} cap={cap} />

  const st = (q.data?.status ?? []).find((s) => s.subsystem === subsystem)
  const d = diagnose(subsystem, st)
  // The walk duration sits with the other cold facts, not in the prose: on a
  // timeout it is the budget, and on a slow success it is the reason the next
  // sweep is late. A probe that reports none contributes no entry here.
  const took = walkSecs(st?.elapsed_s)
  const facts = [
    st?.sysobjectid && `sysObjectID ${st.sysobjectid}`,
    took && `walk took ${took}`,
    st?.last_ok_at && `last worked ${ago(st.last_ok_at)}`,
  ].filter((f): f is string => !!f)

  return (
    <div className={cn("flex flex-col gap-2 rounded-lg border px-3 py-2.5",
      d.tone === "warning" ? "border-warning/40 bg-warning-soft/30" : "bg-muted/40")}>
      <p className="text-xs text-foreground">{d.cause}</p>
      {d.steps.length > 0 && (
        <ol className="flex list-decimal flex-col gap-0.5 pl-4 text-xs text-muted-foreground">
          {d.steps.map((s, i) => <li key={i}>{s}</li>)}
        </ol>
      )}
      {facts.length > 0 && (
        <p className="font-mono text-[0.6875rem] text-faint-foreground">
          {facts.join(" · ")}
        </p>
      )}
      {canWrite && (
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <Button variant="outline" size="sm" className="h-7 text-xs"
            onClick={() => void runSnmpTest(device)}>
            <Zap className="size-3" /> Test SNMP
          </Button>
          {d.notSupported && (
            <button className="ml-auto text-2xs text-faint-foreground hover:text-foreground"
              onClick={() => setNsOpen(true)}>
              not supported on this hardware?
            </button>
          )}
        </div>
      )}
      <NotSupportedDialog device={device} subsystem={subsystem} open={nsOpen} onOpenChange={setNsOpen} />
    </div>
  )
}
