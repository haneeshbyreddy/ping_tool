import type { QueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { snmpApi, ApiError } from "@/lib/api"

const SYSTEM_OID = "1.3.6.1.2.1.1"
const POLL_MS = 5_000       // matches the walk dialog's while-pending cadence
const MAX_WAIT_MS = 180_000 // ~3 report cycles; past that the probe isn't picking up

const inflight = new Set<number>()

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function sysDescrOf(rows: Array<[string, string]> | null): string | null {
  if (!rows?.length) return null
  const hit = rows.find(([oid]) => oid.startsWith("1.3.6.1.2.1.1.1")) ?? rows[0]
  const text = (hit[1] || "").replace(/\s+/g, " ").trim()
  return text ? (text.length > 90 ? `${text.slice(0, 90)}…` : text) : null
}

export async function runSnmpTest(
  device: { id: number; name: string; ip_address: string; snmp_port?: number },
  queryClient?: QueryClient,
): Promise<void> {
  if (inflight.has(device.id)) return
  inflight.add(device.id)
  const tid = `snmp-test-${device.id}`
  const target = `${device.ip_address}:${device.snmp_port || 161}`
  try {
    toast.loading(`Testing SNMP on ${device.name}…`, {
      id: tid, duration: Infinity,
      description: "The probe runs a tiny system walk on its next report, usually under 2 minutes.",
    })
    let walkId: number
    try {
      walkId = (await snmpApi.startWalk(device.id, SYSTEM_OID, 10)).id
    } catch (e) {
      toast.error(`Couldn't queue the SNMP test for ${device.name}`, {
        id: tid, description: e instanceof ApiError ? e.message : "request failed",
      })
      return
    }
    queryClient?.invalidateQueries({ queryKey: ["snmp-walks", device.id] })

    const started = Date.now()
    for (;;) {
      await sleep(POLL_MS)
      let walk
      try {
        walk = (await snmpApi.walkResult(walkId)).walk
      } catch {
        continue // transient fetch hiccup — the walk row is still there
      }
      if (!walk || walk.status === "pending") {
        if (Date.now() - started > MAX_WAIT_MS) {
          toast.error(`SNMP test on ${device.name} never ran`, {
            id: tid,
            description: "The probe hasn't picked it up. Is it online and reporting?",
          })
          return
        }
        continue
      }
      queryClient?.invalidateQueries({ queryKey: ["snmp-walks", device.id] })
      if (walk.status === "done") {
        const descr = sysDescrOf(walk.result)
        if ((walk.varbind_count ?? 0) > 0) {
          toast.success(`SNMP OK on ${device.name}`, {
            id: tid, duration: 10_000,
            description: descr ?? "The device answered the system walk.",
          })
        } else {
          toast.warning(`SNMP answered on ${device.name}, but with nothing`, {
            id: tid, duration: 10_000,
            description: "The agent responded but its system table is empty. Unusual firmware; try a full walk.",
          })
        }
        return
      }
      const err = walk.error || "walk failed"
      const noAnswer = /timeout|no (snmp )?response/i.test(err)
      toast.error(`SNMP test failed on ${device.name}`, {
        id: tid, duration: 15_000,
        description: noAnswer
          ? `No response from ${target}. Check that UDP ${device.snmp_port || 161} reaches the device ` +
            "(port-forward?) and the community string is right. In SNMP v2c a wrong " +
            "community looks identical to no response."
          : err,
      })
      return
    }
  } finally {
    inflight.delete(device.id)
  }
}
