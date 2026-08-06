/* AXIS B — the five MEASUREMENT PLANES.
 *
 * The product has always had two colour questions and one channel to answer
 * them with. Axis A (status) says "is this broken?" and owns red/amber/green.
 * This is the other one: "what KIND of fact is this?" — which is constant, is
 * true whether or not anything is wrong, and therefore has to be carried by
 * something that can never read as an alarm.
 *
 * THE PLANES ARE NOT INVENTED FOR THE PALETTE. The schema already models them:
 * one `OrgDevice` row carries four separate freshness stamps
 * (`optics_updated_at`, `ports_updated_at`, `health_updated_at`,
 * `state_updated_at`), the SNMP layer runs three separate walk clocks with
 * three separate timeout caps, and the device panel's tabs are literally
 * Optical | Health | Ports. All of it rendered in the same grey until now.
 *
 * WHY EXACTLY FIVE, and why REACHABILITY IS NOT ONE OF THEM: a plane earns a
 * hue only if it produces facts that survive the thing being healthy. Optical
 * has an Rx and a PON id; traffic has a port name and a rate; vitals has a CPU
 * figure; plant has a split ratio and a coordinate; fleet has a version and a
 * disk. Reachability has latency, loss and up/down — every one of them a STATE.
 * There is no constant reachability fact, so it takes no identity hue: it IS
 * Axis A. That is what takes the count from six to five, which matters, because
 * five hues fit the permitted 200-330deg band at 26deg apart and six do not.
 *
 * The hues themselves, the budget they are solved under and the reason identity
 * may never colour TEXT all live in index.css beside the --plane-* tokens.
 */

export type Plane = "optical" | "traffic" | "vitals" | "plant" | "fleet"

export const PLANES: readonly Plane[] = ["optical", "traffic", "vitals", "plant", "fleet"] as const

export const PLANE_LABEL: Record<Plane, string> = {
  optical: "Optical",
  traffic: "Traffic",
  vitals: "Vitals",
  plant: "Plant",
  fleet: "Fleet",
}

/** The CSS custom property carrying each plane's hue. Values live in index.css
 *  and are deliberately not operator-settable — they are an encoding solved
 *  against a chroma/contrast budget taken from the status tones, and a
 *  hand-typed value could breach the ceiling that keeps identity from ever
 *  reading as an alarm. */
export const PLANE_VAR: Record<Plane, string> = {
  optical: "var(--plane-optical)",
  traffic: "var(--plane-traffic)",
  vitals: "var(--plane-vitals)",
  plant: "var(--plane-plant)",
  fleet: "var(--plane-fleet)",
}

/** What an issue kind is a fact ABOUT. `null` means reachability, which has no
 *  identity hue by construction (see above) — a device being down is the alarm,
 *  not a category of alarm.
 *
 *  Seven of the eleven kinds are optical, which is the measured reason the
 *  Issues page reads as a wall of identical red: it is overwhelmingly ONE
 *  subsystem talking, and the page had no way to say so. Mirrors
 *  `central/issues.py:KINDS` — a kind added there needs a plane here. */
export const KIND_PLANE: Record<string, Plane | null> = {
  device_down: null,
  probe_stale: "fleet",
  port_down: "traffic",
  bandwidth: "traffic",
  onu_crit: "optical",
  onu_warn: "optical",
  onu_offline: "optical",
  dup_mac: "optical",
  pon_fiber: "optical",
  pon_power: "optical",
  pon_capacity: "optical",
}
