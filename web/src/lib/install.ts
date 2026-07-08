// Installers are served by central from its private-repo mirror, not GitHub —
// the source repo is private, so nobody but central holds a token. Same-origin
// path keeps it working behind Caddy/any hostname without hardcoding one.
export const releaseAsset = (name: string) =>
  `${window.location.origin}/download/latest/${name}`

export const WINDOWS_SETUP_EXE = "wisp-edge-setup-win-amd64.exe"

export interface ProbeIdentity {
  central: string
  org: string
  nodeId: string
  token: string
}

export const probeIdentity = (org: string, nodeId: string, token: string): ProbeIdentity => ({
  central: window.location.origin,
  org,
  nodeId,
  token,
})

export function linuxInstallCmd(id: ProbeIdentity, arch: "amd64" | "arm64" = "amd64"): string {
  return [
    `curl -fsSLo /tmp/wisp-edge.deb \\`,
    `  ${releaseAsset(`wisp-edge-linux-${arch}.deb`)}`,
    `sudo dpkg -i /tmp/wisp-edge.deb`,
    `sudo tee /etc/wisp/edge.env >/dev/null <<'EOF'`,
    `WISP_CENTRAL_URL=${id.central}`,
    `WISP_CENTRAL_TOKEN=${id.token}`,
    `WISP_ORG_ID=${id.org}`,
    `WISP_NODE_ID=${id.nodeId}`,
    `WISP_DB=/etc/wisp/wisp.db`,
    `EOF`,
    `sudo chmod 600 /etc/wisp/edge.env`,
    `sudo systemctl enable --now wisp-edge`,
  ].join("\n")
}

export function windowsSilentCmd(id: ProbeIdentity): string {
  return `.\\${WINDOWS_SETUP_EXE} /VERYSILENT /Central=${id.central}`
    + ` /Token=${id.token} /Org=${id.org} /Node=${id.nodeId}`
}
