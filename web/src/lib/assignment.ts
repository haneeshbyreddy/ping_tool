import type { AssignableAccount, DeviceAssignment, OrgDevice } from "@/lib/types"

export interface Responsibility {
  own: number[]
  inherited: Array<{ user_id: number; from: OrgDevice }>
  effective: number[]
}

export function responsibilityFor(
  device: OrgDevice,
  byId: Map<number, OrgDevice>,
): Responsibility {
  const own = [...(device.assignee_ids ?? [])]
  const inherited: Array<{ user_id: number; from: OrgDevice }> = []
  const seen = new Set<number>([device.id])
  let parent = device.parent_device_id != null ? byId.get(device.parent_device_id) : undefined
  while (parent && !seen.has(parent.id)) {
    seen.add(parent.id)
    for (const uid of parent.assignee_ids ?? []) {
      if (!own.includes(uid) && !inherited.some((i) => i.user_id === uid)) {
        inherited.push({ user_id: uid, from: parent })
      }
    }
    parent = parent.parent_device_id != null ? byId.get(parent.parent_device_id) : undefined
  }
  return { own, inherited, effective: [...own, ...inherited.map((i) => i.user_id)] }
}

export function scopeOf(userId: number, devices: OrgDevice[]): Set<number> {
  const roots = devices.filter((d) => (d.assignee_ids ?? []).includes(userId))
  if (roots.length === 0) return new Set()
  const children = new Map<number, OrgDevice[]>()
  for (const d of devices) {
    if (d.parent_device_id == null) continue
    const list = children.get(d.parent_device_id)
    if (list) list.push(d)
    else children.set(d.parent_device_id, [d])
  }
  const out = new Set<number>(roots.map((d) => d.id))
  const stack = [...roots]
  while (stack.length) {
    const cur = stack.pop()!
    for (const kid of children.get(cur.id) ?? []) {
      if (!out.has(kid.id)) {
        out.add(kid.id)
        stack.push(kid)
      }
    }
  }
  return out
}

export const nameOf = (
  userId: number,
  accounts: AssignableAccount[] | undefined,
  assignments?: DeviceAssignment[],
): string =>
  accounts?.find((a) => a.user_id === userId)?.username ??
  assignments?.find((a) => a.user_id === userId)?.username ??
  `#${userId}`
