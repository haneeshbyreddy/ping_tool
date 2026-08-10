// The fibre-strand standard, mirrored from `central/fiber.py`.
//
// Two copies on purpose, the same arrangement the theme allowlist and the
// map-detail defaults keep: the SPA has to render a swatch before any request
// resolves, and central has to refuse a bad strand without asking a browser.
// `unit/test_fiber.py` reads THIS FILE and fails if the two drift — a count
// central accepts and the form never offers is a knob nobody can reach, and a
// colour they disagree on is a crew sent to the wrong strand.
//
// THE ONE RULE THAT MATTERS HERE: a strand colour is a MARK colour, never a line
// colour and never text. The sequence contains red, orange, yellow and green —
// the exact hues this product reserves for alarms, on the one screen that exists
// to show them — so a cable painted red because it happens to be core 7 is a
// fabricated outage. It renders as a DOT inside a neutral chip and as a SWATCH
// in a panel: the identity-chip grammar from the two-colour-axes pass, where a
// status chip is coloured TEXT and an identity chip is neutral text beside a
// coloured mark. `--map-line-*` stays the only vocabulary a stroke may take.

/** Cable sizes an access network is built from. CLOSED — the count bounds the
 *  strand and drives the tube arithmetic, so a free-form "17F" would name a
 *  position that exists in no cable anyone can buy.
 *
 *  **1 is a single-fibre TAIL** (2026-08-09) — one strand out of a closure into a
 *  PON port. Without it that connection could not be recorded at all: no cable
 *  could be laid for it, and a trunk core cannot be terminated at a box its own
 *  sheath never reaches. */
export const FIBER_COUNTS = [1, 2, 4, 6, 8, 12, 24, 48, 96] as const
export type FiberCount = (typeof FIBER_COUNTS)[number]

/** TIA-598-D. ORDER IS THE STANDARD — index 0 is fibre 1 — so this array must
 *  never be sorted or deduped. Hexes are the conventional jacket renderings,
 *  picked to stay recognisable on both themes' card surfaces; white and black
 *  are the two that need a ring drawn round them (see `.wisp-strand` in
 *  index.css) rather than a fudged hex, or one vanishes per theme. */
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

/** how many fibres share one buffer tube — the reason the sequence repeats */
export const TUBE_SIZE = STRAND_COLORS.length

export const isFiberCount = (n: unknown): n is FiberCount =>
  typeof n === "number" && (FIBER_COUNTS as readonly number[]).includes(n)

const at = (position: number) => STRAND_COLORS[(position - 1) % TUBE_SIZE]

export const strandName = (position: number): string => at(position).name
export const strandHex = (position: number): string => at(position).hex

export interface StrandLocation {
  coreNo: number
  /** position WITHIN its tube, 1..12 */
  fiber: number
  fiberColor: string
  fiberHex: string
  /** null on a cable of 12 or fewer — there is no tube to choose between */
  tube: number | null
  tubeColor: string | null
  tubeHex: string | null
}

/** Where a strand physically is, as a crew would be told to find it.
 *
 *  Past twelve fibres a cable is buffer TUBES of twelve, and the tubes take the
 *  same colour sequence — so core 25 of a 48F is not "the 25th one", it is the
 *  BLUE fibre in the GREEN tube, and that is the only form of the answer that is
 *  any use to somebody holding an open closure. A cable of 12 or fewer reports
 *  no tube at all rather than "tube 1": naming a tube on a cable that has one is
 *  the same noise as the map printing a dash where there is no reading. */
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

/** One line a human can act on. */
export function strandLabel(coreNo: number, cores?: number | null): string {
  const s = strandAt(coreNo, cores)
  return s.tube == null
    ? `${s.fiberColor} fibre${cores ? ` (${coreNo} of ${cores})` : ""}`
    : `${s.fiberColor} fibre in the ${s.tubeColor} tube (core ${coreNo}${cores ? ` of ${cores}` : ""})`
}

/** How a cable is written on a drum tag and on the map: "12F". */
export const fiberLabel = (cores: number | null | undefined): string | null =>
  cores ? `${cores}F` : null

/** Why two fibres may not be joined — mirrored from `fiber.JOINT_REFUSAL_TEXT`.
 *
 *  Every one is PHYSICALLY IMPOSSIBLE rather than merely unusual, which is why
 *  they are stated as flat facts: "may be" belongs on a guess. A tray that
 *  refuses without saying which of these it is is indistinguishable from a
 *  broken button, so the server names the refusal and this turns it into the
 *  sentence.
 *
 *  There used to be a fourth family here — `split`/`fork`/`loop`, the faults a
 *  core could fall into when a cable was a bag of spans. A cable is a SEGMENT
 *  now, so core N of it has exactly two ends and none of those states can be
 *  written down. Deleting the vocabulary is the point; do not re-add it. */
export const JOINT_REFUSAL_TEXT: Record<string, string> = {
  absent: "Both fibres have to end at this point — a strand can only be joined where the cable is opened.",
  self: "A fibre cannot be joined to itself.",
  taken: "That fibre is already joined to another one here. One fibre joins exactly one fibre.",
}

/** The cable laid out as it is BUILT: rows of twelve, one row per buffer tube.
 *
 *  The layout is the explanation. Twelve is the tube, so a 48F draws four rows
 *  and picking core 25 means clicking the first swatch of the third row — which
 *  is the motion of finding it in the field, sheath open, counting into the
 *  green tube. A 12F or smaller has one row and no tube label, because naming
 *  "tube 1" on a cable with nothing to choose between is the same noise as
 *  printing a dash where there is no reading. */
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

/** How a cable's recorded coverage is SAID — and it is never said as spare.
 *
 *  Six strands written down on a 12F does not leave six free: nobody wrote the
 *  others down, and unknown is not spare. Exactly the splitter-legs rule, and
 *  for the same reason — the one capacity claim that survives an incomplete
 *  record is OVER-subscription, which is provable either way and is the only one
 *  made anywhere in this feature. */
export function coresRecordedLabel(
  recorded: number, cores: number | null | undefined,
): string {
  if (!cores) return recorded ? `${recorded} strand${recorded === 1 ? "" : "s"} recorded` : ""
  return `${recorded} of ${cores} cores recorded`
}

/** The whole cable, compressed to what fits on a map chip: `12F·7`.
 *
 *  The strand rides the same chip as the count because they are one fact — a
 *  core number with no cable to be a core OF is half a sentence, which is also
 *  why the server refuses to store one. Null when nothing is recorded, so the
 *  caller draws NOTHING: this map already learned (twice) that a badge announcing
 *  an absence is worse than no badge, because it spends the pixels a live
 *  reading would have used to say nothing at all. */
export function cableChipText(
  cores: number | null | undefined, coreNo: number | null | undefined,
): string | null {
  if (!cores) return null
  return coreNo ? `${cores}F·${coreNo}` : `${cores}F`
}
