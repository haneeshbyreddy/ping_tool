// Guided SNMP troubleshooting: turns the edge's per-subsystem sweep diagnosis
// ("snmp_status") into a plain-language WHY + the next concrete step, so a blank
// Ports/Optical/vitals panel is never a dead end. Rendered by those panels' empty
// states — it owns its own dialogs (walk, profile wizard, not-supported).
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Play, Wand2 } from "lucide-react"
import { snmpApi, ApiError } from "@/lib/api"
import type {
  DeviceCapability, OrgDevice, SnmpSubsystem, SnmpSubsystemStatus,
} from "@/lib/types"
import { ProfileWizard } from "@/components/profile-wizard"
import { SnmpWalkDialog } from "@/components/snmp-walk-dialog"
import { useAuth } from "@/hooks/use-auth"
import { ago } from "@/lib/format"
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

interface Diagnosis {
  cause: string
  steps: string[]
  /** which actions make sense for this state */
  walk?: boolean
  wizard?: boolean
  notSupported?: boolean
}

function diagnose(subsystem: SnmpSubsystem, st: SnmpSubsystemStatus | undefined): Diagnosis {
  if (!st) {
    return {
      cause: "No diagnosis from the probe yet — it reports one with every SNMP sweep.",
      steps: [
        "Wait one sweep (~2 minutes) after enabling SNMP.",
        "If this never fills in, the assigned probe is likely on an older agent build — update it from Network → Probes.",
      ],
    }
  }
  switch (st.state) {
    case "ok":
      return {
        cause: `The last sweep succeeded${st.item_count != null ? ` (${st.item_count} item${st.item_count === 1 ? "" : "s"})` : ""} ${ago(st.updated_at)} — data should appear shortly.`,
        steps: [],
      }
    case "no_response":
      return {
        cause: "The device never answered SNMP — the probe's queries go unanswered, so the fix is on the device itself.",
        steps: [
          "Check the SNMP agent is enabled on the device (many ship with it off).",
          "Check the community string matches what's configured here.",
          "Check any SNMP ACL/allowed-hosts list on the device includes the probe's IP.",
        ],
        walk: true,
      }
    case "timeout":
      return {
        cause: "The device answers, but the walk ran past its time budget — usually a very large table or a slow agent. The probe retries on every sweep.",
        steps: [
          "If this device worked before, it may be overloaded — check its CPU.",
          "Persistent timeouts on a big OLT/switch are worth reporting — the walk budget is tunable per subsystem.",
        ],
      }
    case "no_profile":
      return {
        cause: `No GPON vendor profile claims this OLT (sysObjectID ${st.sysobjectid ?? "unknown"}) — optics stay off rather than guessing another vendor's OIDs.`,
        steps: [
          "If this is a known vendor under an odd sysObjectID, set the GPON vendor override in the device's settings.",
          "Otherwise this vendor needs support added — run a walk of its enterprise tree and share the dump.",
        ],
        walk: true,
        notSupported: true,
      }
    case "empty":
      if (subsystem === "health") {
        return st.profile
          ? {
              cause: `Profile “${st.profile}” matched this device but returned no readings — its OIDs are probably wrong for this exact model.`,
              steps: [
                "Run an SNMP walk of the enterprise tree to see what the device really exposes.",
                "Fix the profile's OIDs from the walk (or create a model-specific profile — longest sysObjectID match wins).",
              ],
              walk: true,
              wizard: true,
            }
          : {
              cause: "The device answers SNMP but exposes none of the standard health OIDs — cheap gear usually hides CPU/RAM/temperature in its private vendor tree.",
              steps: [
                "Run an SNMP walk of the enterprise tree (one click below).",
                "Pick the CPU/RAM/temperature rows out of the dump with the profile wizard — the probe starts using them on its next sweep, no rollout.",
              ],
              walk: true,
              wizard: true,
              notSupported: true,
            }
      }
      if (subsystem === "ports") {
        return {
          cause: "The device answers SNMP but its interface table (ifTable) came back empty — some gear simply doesn't expose ports over SNMP.",
          steps: [
            "Run a walk of the Interfaces subtree to confirm what the agent exposes.",
            "If the hardware genuinely has no port table, mark ports as not supported so it stops showing as a gap.",
          ],
          walk: true,
          notSupported: true,
        }
      }
      return {
        cause: "The OLT answers SNMP and a vendor profile matched, but its ONU table came back empty.",
        steps: [
          "If no ONUs are registered yet this is normal.",
          "Otherwise the vendor profile may not fit this model — run a walk and share the dump.",
        ],
        walk: true,
      }
    case "error":
    default:
      return {
        cause: `The SNMP sweep failed: ${st.detail ?? "unknown error"}.`,
        steps: ["Transient errors clear on the next sweep — a persistent one usually means a network path or device problem."],
        walk: true,
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
        {cap.note && <> — {cap.note}</>}
        {cap.updated_by && <span className="text-faint-foreground"> · {cap.updated_by}, {ago(cap.updated_at)}</span>}
      </p>
      {canWrite && (
        <button className="mt-1 text-2xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          onClick={() => undo.mutate()} disabled={undo.isPending}>
          Undo — start flagging this again
        </button>
      )}
    </div>
  )
}

export function SnmpDiagnosis({ device, subsystem }: {
  device: OrgDevice
  subsystem: SnmpSubsystem
}) {
  const { canWrite } = useAuth()
  const [walkOpen, setWalkOpen] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [nsOpen, setNsOpen] = useState(false)
  const q = useQuery({
    queryKey: ["snmp-status", device.id],
    queryFn: () => snmpApi.status(device.id),
    refetchInterval: 60_000, // diagnoses move on the SNMP sweep cadence
  })

  if (device.snmp_enabled !== 1) {
    return (
      <p className="rounded-lg border bg-muted/40 px-3 py-2.5 text-xs text-muted-foreground">
        SNMP is off for this device — enable it (with a community string) in the device's
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

  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted/40 px-3 py-2.5">
      <p className="text-xs text-foreground">{d.cause}</p>
      {d.steps.length > 0 && (
        <ol className="flex list-decimal flex-col gap-0.5 pl-4 text-xs text-muted-foreground">
          {d.steps.map((s, i) => <li key={i}>{s}</li>)}
        </ol>
      )}
      {(st?.sysobjectid || st?.last_ok_at) && (
        <p className="font-mono text-[0.6875rem] text-faint-foreground">
          {st.sysobjectid && <>sysObjectID {st.sysobjectid}</>}
          {st.sysobjectid && st.last_ok_at && " · "}
          {st.last_ok_at && <>last worked {ago(st.last_ok_at)}</>}
        </p>
      )}
      {canWrite && (d.walk || d.wizard || d.notSupported) && (
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          {d.walk && (
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => setWalkOpen(true)}>
              <Play className="size-3" /> Run SNMP walk
            </Button>
          )}
          {d.wizard && (
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => setWizardOpen(true)}>
              <Wand2 className="size-3" /> Build profile from walk
            </Button>
          )}
          {d.notSupported && (
            <button className="ml-auto text-2xs text-faint-foreground hover:text-foreground"
              onClick={() => setNsOpen(true)}>
              not supported on this hardware?
            </button>
          )}
        </div>
      )}
      {walkOpen && <SnmpWalkDialog device={device} open={walkOpen} onOpenChange={setWalkOpen} />}
      {wizardOpen && (
        <ProfileWizard device={device} sysObjectId={st?.sysobjectid ?? null}
          open={wizardOpen} onOpenChange={setWizardOpen} />
      )}
      <NotSupportedDialog device={device} subsystem={subsystem} open={nsOpen} onOpenChange={setNsOpen} />
    </div>
  )
}
