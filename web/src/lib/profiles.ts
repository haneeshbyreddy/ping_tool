// Vendor profiles (SNMP health, GPON) are matched to a box by the LONGEST
// sysObjectID prefix that covers it, so a GLOBAL profile already claims every
// device under its prefix, in every org. The duplication this guards against is
// measured, not theoretical: the same C-Data recipe was entered four times, once
// per ISP, because nothing warned that the recipe already existed.
//
// Scope is set at CREATE time only: the update route rewrites name/prefix/spec
// and never org_id, so an editing form STATES the scope instead of offering it.
//
// Global is the default for every recipe table, not just these two. The helpers
// below are deliberately shaped around `org_id: string | null` alone, which is
// the one column all five recipe tables share, so the next table adopts the
// same words without a second vocabulary.

/** Who a recipe reaches. `org` means one organization, named by the caller. */
export type ProfileScope = "global" | "org"

export const scopeOf = (orgId: string | null): ProfileScope =>
  orgId === null ? "global" : "org"

/** The badge on a list row. Short, because it sits beside the name. */
export const scopeLabel = (orgId: string | null): string =>
  orgId === null ? "global" : `${orgId} only`

/** The sentence a form states about a saved recipe's reach. */
export const scopeSentence = (orgId: string | null): string =>
  orgId === null
    ? "Global. Every organization's probes receive it."
    : `${orgId} only. No other organization's probes receive it.`

export const scopeTitle = (orgId: string | null): string =>
  orgId === null
    ? "Every organization's probes receive this recipe"
    : "Only this organization's probes receive this recipe"

export interface ProfileClaim {
  id: number
  name: string
  org_id: string | null
  match_sysobjectid: string
  enabled: boolean
}

const trimOid = (oid: string) => (oid || "").trim().replace(/^\.+|\.+$/g, "")

/**
 * Mirrors the edge's own match (`ingress/health.match_profile`,
 * `ingress/gpon.match_profile`): exact, or the OID sits under the prefix on a
 * dot boundary. Plain startsWith would report 1.3.6.1.4.1.5651 as claiming
 * 1.3.6.1.4.1.56512, which is a different vendor.
 */
export function prefixCovers(prefix: string, oid: string): boolean {
  const p = trimOid(prefix)
  const o = trimOid(oid)
  if (!p || !o) return false
  return o === p || o.startsWith(`${p}.`)
}

/** The global profile that already claims this prefix, if there is one. */
export function globalClaim<T extends ProfileClaim>(
  profiles: T[], match: string, selfId?: number | null,
): T | null {
  const m = trimOid(match)
  if (!m) return null
  for (const p of profiles) {
    if (p.org_id !== null) continue
    if (selfId != null && p.id === selfId) continue
    if (prefixCovers(p.match_sysobjectid, m)) return p
  }
  return null
}

/**
 * A warning, never a refusal: a deliberate narrower override is legitimate.
 * Names the profile that already claims the prefix, because "somebody already
 * wrote this recipe" is the fact that stops the fourth copy.
 */
export function claimWarning(claim: ProfileClaim, match: string): string {
  const held = trimOid(claim.match_sysobjectid)
  const off = claim.enabled ? "" : ", currently off"
  if (held === trimOid(match)) {
    return `The global profile “${claim.name}”${off} already claims exactly this prefix. `
      + "A second profile for the same hardware is how one recipe ends up entered "
      + "once per organization. You can still save."
  }
  return `The global profile “${claim.name}”${off} claims ${held}, which covers this prefix. `
    + "Save only if this is a deliberate narrower override for this hardware. "
    + "You can still save."
}
