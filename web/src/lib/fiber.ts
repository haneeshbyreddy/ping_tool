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

export function portLabel(kind: string | null | undefined,
                          no: number | null | undefined): string | null {
  if (!kind || !(PORT_KINDS as readonly string[]).includes(kind)) return null
  if (kind === "pon") return no == null ? "PON" : `PON ${no}`
  if (kind === "leg") return no == null ? "leg" : `leg ${no}`
  if (kind === "port") return no == null ? "port" : `port ${no}`
  return no && no > 1 ? `input ${no}` : "input"
}

export const ENCLOSURE_TYPES = ["coupler", "closure", "fdb"] as const

export function portKindFor(deviceType: string | null | undefined): PortKind | null {
  if (!deviceType) return null
  if ((ENCLOSURE_TYPES as readonly string[]).includes(deviceType)) return null
  if (deviceType === "splitter") return "leg"
  if (deviceType === "OLT") return "pon"
  return "port"
}

export function isPlumbing(c: { name?: string | null; cores?: number | null
                                path?: unknown }): boolean {
  const traced = Array.isArray(c.path) ? c.path.length > 0 : !!c.path
  return !(c.name || "").trim() && (c.cores ?? 1) <= 1 && !traced
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
