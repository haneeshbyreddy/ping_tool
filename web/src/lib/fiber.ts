export const FIBER_COUNTS = [1, 2, 4, 6, 8, 12, 24, 48, 96] as const
export type FiberCount = (typeof FIBER_COUNTS)[number]

export const STRAND_COLORS = [
  { name: "blue", hex: "#0d6fd1" },
  { name: "orange", hex: "#f07000" },
  { name: "green", hex: "#009a44" },
  { name: "brown", hex: "#8a5a2b" },
  { name: "slate", hex: "#8c9296" },
  { name: "white", hex: "#f5f5f2" },
  { name: "red", hex: "#e03c31" },
  { name: "black", hex: "#1c1c1e" },
  { name: "yellow", hex: "#f2c100" },
  { name: "violet", hex: "#8b4bb0" },
  { name: "rose", hex: "#f2a0b6" },
  { name: "aqua", hex: "#4ec3dd" },
] as const

export const TUBE_SIZE = STRAND_COLORS.length

export const isFiberCount = (n: unknown): n is FiberCount =>
  typeof n === "number" && (FIBER_COUNTS as readonly number[]).includes(n)

const at = (position: number) => STRAND_COLORS[(position - 1) % TUBE_SIZE]

export const strandName = (position: number): string => at(position).name
export const strandHex = (position: number): string => at(position).hex

export interface StrandLocation {
  coreNo: number
  fiber: number
  fiberColor: string
  fiberHex: string
  tube: number | null
  tubeColor: string | null
  tubeHex: string | null
}

export function strandAt(coreNo: number, cores?: number | null): StrandLocation {
  const tube = Math.floor((coreNo - 1) / TUBE_SIZE) + 1
  const within = ((coreNo - 1) % TUBE_SIZE) + 1
  const single = cores != null && cores <= TUBE_SIZE
  return {
    coreNo,
    fiber: within,
    fiberColor: strandName(within),
    fiberHex: strandHex(within),
    tube: single ? null : tube,
    tubeColor: single ? null : strandName(tube),
    tubeHex: single ? null : strandHex(tube),
  }
}

export function strandLabel(coreNo: number, cores?: number | null): string {
  const s = strandAt(coreNo, cores)
  return s.tube == null
    ? `${s.fiberColor} fibre${cores ? ` (${coreNo} of ${cores})` : ""}`
    : `${s.fiberColor} fibre in the ${s.tubeColor} tube (core ${coreNo}${cores ? ` of ${cores}` : ""})`
}

export const fiberLabel = (cores: number | null | undefined): string | null =>
  cores ? `${cores}F` : null

export const JOINT_REFUSAL_TEXT: Record<string, string> = {
  absent: "Both fibres have to end at this point — a strand can only be joined where the cable is opened.",
  self: "A fibre cannot be joined to itself.",
  taken: "That fibre is already joined to another one here. One fibre joins exactly one fibre.",
  port_taken: "Another fibre already lands on that port. One port takes exactly one fibre.",
  port_splice: "A port belongs to a fibre taken into the box, not to a splice between two cables.",
}

export const PORT_KINDS = ["pon", "leg", "in", "port"] as const
export type PortKind = (typeof PORT_KINDS)[number]

// MIRRORS fiber.py — a port's identity is the box's own string, never a number we
// derived from it. `port` refs are interface names; the numbered kinds carry their
// number as text. See fiber.py:NUMBERED_KINDS for why the two are separated.
export const NUMBERED_KINDS = ["pon", "leg", "in"] as const
export const PORT_REF_MAX = 64

// THE ONE normalizer, mirroring fiber.port_key. Refs display as typed; two spellings
// of one socket are reconciled here and nowhere else.
export function portKey(ref: string | null | undefined): string {
  return (ref ?? "").trim().split(/\s+/).join(" ").toLowerCase()
}

export function isNumberedKind(kind: string | null | undefined): boolean {
  return (NUMBERED_KINDS as readonly string[]).includes(kind ?? "")
}

export function portLabel(kind: string | null | undefined,
                          ref: string | null | undefined): string | null {
  if (!kind || !(PORT_KINDS as readonly string[]).includes(kind)) return null
  const text = (ref ?? "").trim()
  if (kind === "port") return text || "port"      // the interface name IS the label
  if (kind === "pon") return text ? `PON ${text}` : "PON"
  if (kind === "leg") return text ? `leg ${text}` : "leg"
  return text && text !== "1" ? `input ${text}` : "input"
}

// A PORT IS NAMED BY THE BOX, NOT BY US — the server resolves `TrayPort.label` to the
// walked interface name (`EPON0/1`, never `PON 1`), so anything printing a port looks
// it up in that box's own list rather than rebuilding the string. `portLabel` stays
// the CANONICAL form and the fallback: a port recorded on a box that walks nothing
// has no name but the one our ref spells. Structurally typed so this module keeps no
// imports — it is the mirror `unit/test_fiber` reads as source.
export function portName(ports: ReadonlyArray<{ kind: string; ref: string;
                                                label: string }> | undefined,
                         kind: string | null | undefined,
                         ref: string | null | undefined): string | null {
  if (!kind) return null
  const key = portKey(ref)
  const hit = (ports ?? []).find((p) => p.kind === kind && portKey(p.ref) === key)
  return hit?.label ?? portLabel(kind, ref)
}

export const ENCLOSURE_TYPES = ["coupler", "closure", "fdb"] as const

// MIRRORS fiber.port_kinds_for — A BOX HAS KINDS, PLURAL. An OLT has PONs AND the GE
// uplink the trunk lands on. Deliberately no `uplink` kind: uplink is a ROLE, decided
// by what somebody plugged in, and the first customer feed on GE0/8 makes it a lie.
export function portKindsFor(deviceType: string | null | undefined): PortKind[] {
  if (!deviceType) return []
  if ((ENCLOSURE_TYPES as readonly string[]).includes(deviceType)) return []
  if (deviceType === "splitter") return ["leg"]
  if (deviceType === "OLT") return ["pon", "port"]
  return ["port"]
}

export function portKindFor(deviceType: string | null | undefined): PortKind | null {
  return portKindsFor(deviceType)[0] ?? null
}

export function portKindWord(kind: string): string {
  return kind === "pon" ? "PON" : kind === "in" ? "input" : kind
}

export function isPlumbing(c: { name?: string | null; cores?: number | null
                                path?: unknown }): boolean {
  const traced = Array.isArray(c.path) ? c.path.length > 0 : !!c.path
  return !(c.name || "").trim() && (c.cores ?? 1) <= 1 && !traced
}

// A CUT DRUM IS ONE OBJECT. Opening a closure mid-span stores TWO cables — the
// segment model that keeps a core's name unambiguous — and both halves keep the
// drum's name, so the tray's picker offered "6F · main" twice, told apart only by a
// small far-end hint. That asks a question that does not exist at the pole (the
// operator who had just cut the closure could not read it back), and it HID facts: a
// core cut and used on one side read "+ join" on the other, with nothing saying the
// spare glass existed. Exactly two non-plumbing cables sharing a name and a count at
// one point pair into the drum they are — the pairing split() produces by
// construction, since both halves keep the drum's name and count. Three same-named
// segments cannot pair and fall back to the per-cable view. View-level only: storage
// stays two segments, the same standing the half-coupler has.
export function cutPairs(cables: ReadonlyArray<{
  id: number; name?: string | null; cores?: number | null; plumbing?: boolean
}>): Map<number, number> {
  const groups = new Map<string, number[]>()
  for (const c of cables) {
    const name = (c.name || "").trim()
    if (c.plumbing || !c.cores || !name) continue
    const key = `${name.toLowerCase()}|${c.cores}`
    groups.set(key, [...(groups.get(key) ?? []), c.id])
  }
  const out = new Map<number, number>()
  for (const g of groups.values()) {
    if (g.length !== 2) continue
    out.set(g[0], g[1])
    out.set(g[1], g[0])
  }
  return out
}

export function cableRef(c: { name?: string | null; cores?: number | null;
                              path?: unknown },
                         farName?: string | null): string {
  const name = (c.name || "").trim()
  if (name) return name
  return farName || "this fibre"
}

export function tubeRows(cores: number): Array<{ tube: number; cores: number[] }> {
  const rows: Array<{ tube: number; cores: number[] }> = []
  for (let t = 0; t * TUBE_SIZE < cores; t++) {
    const first = t * TUBE_SIZE + 1
    const count = Math.min(TUBE_SIZE, cores - t * TUBE_SIZE)
    rows.push({
      tube: t + 1,
      cores: Array.from({ length: count }, (_, i) => first + i),
    })
  }
  return rows
}

export function coresRecordedLabel(
  recorded: number, cores: number | null | undefined,
): string {
  if (!cores) return recorded ? `${recorded} strand${recorded === 1 ? "" : "s"} recorded` : ""
  return `${recorded} of ${cores} cores recorded`
}

export function cableChipText(
  cores: number | null | undefined, coreNo: number | null | undefined,
): string | null {
  if (!cores) return null
  return coreNo ? `${cores}F·${coreNo}` : `${cores}F`
}
