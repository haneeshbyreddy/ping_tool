import { toast } from "sonner"
import { snmpApi, ApiError } from "@/lib/api"

const POLL_MS = 5_000       // one poll per report-ish cycle; the edge only ever polls
const MAX_WAIT_MS = 180_000 // ~3 report cycles; past that the probe isn't picking up
const DESCR_MAX = 90        // toast description, not the value: keep it one line

const inflight = new Set<number>()

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

// The server pins the walk root, extracts sysDescr and ships no varbinds, so
// there is nothing to search here — only the length this toast can carry.
function shortDescr(text: string | null): string | null {
  const t = (text || "").trim()
  if (!t) return null
  return t.length > DESCR_MAX ? `${t.slice(0, DESCR_MAX)}…` : t
}

export async function runSnmpTest(
  device: { id: number; name: string; ip_address: string; snmp_port?: number },
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
    let testId: number
    try {
      testId = (await snmpApi.startTest(device.id)).id
    } catch (e) {
      toast.error(`Couldn't queue the SNMP test for ${device.name}`, {
        id: tid, description: e instanceof ApiError ? e.message : "request failed",
      })
      return
    }

    const started = Date.now()
    for (;;) {
      await sleep(POLL_MS)
      let test
      try {
        test = (await snmpApi.testResult(testId)).test
      } catch {
        continue // transient fetch hiccup — the walk row is still there
      }
      if (!test || test.status === "pending") {
        if (Date.now() - started > MAX_WAIT_MS) {
          toast.error(`SNMP test on ${device.name} never ran`, {
            id: tid,
            description: "The probe hasn't picked it up. Is it online and reporting?",
          })
          return
        }
        continue
      }
      if (test.status === "done") {
        if (test.answered) {
          toast.success(`SNMP OK on ${device.name}`, {
            id: tid, duration: 10_000,
            description: shortDescr(test.sys_descr) ?? "The device answered the system walk.",
          })
        } else {
          toast.warning(`SNMP answered on ${device.name}, but with nothing`, {
            id: tid, duration: 10_000,
            description: "The agent responded but its system table is empty. That is unusual firmware, and worth reporting to the platform admin.",
          })
        }
        return
      }
      const err = test.error || "walk failed"
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
