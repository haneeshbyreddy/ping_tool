export type Plane = "optical" | "traffic" | "vitals" | "plant" | "fleet"

export const PLANES: readonly Plane[] = ["optical", "traffic", "vitals", "plant", "fleet"] as const

export const PLANE_LABEL: Record<Plane, string> = {
  optical: "Optical",
  traffic: "Traffic",
  vitals: "Vitals",
  plant: "Plant",
  fleet: "Fleet",
}

export const PLANE_VAR: Record<Plane, string> = {
  optical: "var(--plane-optical)",
  traffic: "var(--plane-traffic)",
  vitals: "var(--plane-vitals)",
  plant: "var(--plane-plant)",
  fleet: "var(--plane-fleet)",
}

export const KIND_PLANE: Record<string, Plane | null> = {
  device_down: null,
  probe_stale: "fleet",
  port_down: "traffic",
  camera_down: "plant",
  bandwidth: "traffic",
  onu_crit: "optical",
  onu_warn: "optical",
  onu_offline: "optical",
  dup_mac: "optical",
  pon_fiber: "optical",
  pon_power: "optical",
  pon_capacity: "optical",
}
