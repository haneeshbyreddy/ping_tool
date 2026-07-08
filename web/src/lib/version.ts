// Mirrors src/wisp/version.py's version_tuple/is_newer — semver major.minor.patch,
// tolerant of a leading "v" and git-describe suffixes ("0.13.0-3-gabc123").
export function versionTuple(v: string | null | undefined): [number, number, number] {
  if (!v) return [0, 0, 0]
  const out = v.trim().replace(/^[vV]/, "").split(/[.\-+]/).slice(0, 3)
    .map((p) => { const m = p.match(/^\d+/); return m ? parseInt(m[0], 10) : 0 })
  while (out.length < 3) out.push(0)
  return out as [number, number, number]
}

export function isNewerVersion(candidate: string | null | undefined,
                               current: string | null | undefined): boolean {
  const a = versionTuple(candidate), b = versionTuple(current)
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i]
  }
  return false
}
