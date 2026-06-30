#!/usr/bin/env bash
#
# WISP Edge — fleet installer (frozen binary). The "curl | sh" fleet path:
#
#   curl -fsSL https://central.example.net/install-edge.sh | sudo sh -s -- \
#        --central https://central.example.net --token <ENROLL> --tenant ispA --node edge-a1
#
# It detects the arch, downloads the matching signed binary, VERIFIES its sha256 (a published
# checksum — refuses to install on a mismatch), installs the agent + supervisor under /opt/wisp,
# writes config/identity to /etc/wisp (which an update never touches), enables unprivileged ICMP,
# and starts the systemd service. Supply-chain note: serve this over HTTPS and verify the sha256
# (and, in production, a minisign/GPG signature with the public key pinned here) — never ship an
# unverified pipe-to-shell.
set -euo pipefail

CENTRAL="" TOKEN="" TENANT="default" NODE="$(hostname)"
BASE_URL=""            # where the binaries + SHA256SUMS live (defaults to $CENTRAL/dl)
PREFIX=/opt/wisp
CONFIG_DIR=/etc/wisp

log() { echo "▸ $*"; }
err() { echo "✗ $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --central) CENTRAL="$2"; shift 2;;
    --token)   TOKEN="$2"; shift 2;;
    --tenant)  TENANT="$2"; shift 2;;
    --node)    NODE="$2"; shift 2;;
    --base-url) BASE_URL="$2"; shift 2;;
    *) err "unknown arg: $1";;
  esac
done
[ "$(id -u)" -eq 0 ] || err "run as root (use sudo)"
[ -n "$CENTRAL" ] || err "--central is required"
[ -n "$BASE_URL" ] || BASE_URL="$CENTRAL/dl"

# --- arch detection -> artifact name ---------------------------------------
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  PLAT=linux-amd64;;
  Linux-aarch64) PLAT=linux-arm64;;
  *) err "unsupported platform $(uname -s)-$(uname -m) (this installer is Linux amd64/arm64)";;
esac
log "platform: $PLAT"

# --- download agent + supervisor + verify sha256 ---------------------------
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
fetch() { curl -fsSL "$BASE_URL/$1" -o "$tmp/$1" || err "download failed: $BASE_URL/$1"; }

log "downloading binaries + checksums…"
fetch "wisp-edge-$PLAT"
fetch "wisp-supervisor-$PLAT"
fetch "SHA256SUMS"

log "verifying sha256…"
( cd "$tmp" && grep -E " wisp-(edge|supervisor)-$PLAT\$" SHA256SUMS | sha256sum -c - ) \
  || err "checksum verification FAILED — refusing to install"

# --- install (binary under /opt; config/identity under /etc, preserved on update) ----
install -d "$PREFIX/bin" "$CONFIG_DIR"
install -m 0755 "$tmp/wisp-edge-$PLAT"       "$PREFIX/bin/wisp-edge"
install -m 0755 "$tmp/wisp-supervisor-$PLAT" "$PREFIX/bin/wisp-supervisor"

if [ ! -f "$CONFIG_DIR/edge.env" ]; then
  log "writing $CONFIG_DIR/edge.env (identity + central)…"
  umask 077
  cat > "$CONFIG_DIR/edge.env" <<EOF
WISP_CENTRAL_URL=$CENTRAL
WISP_CENTRAL_TOKEN=$TOKEN
WISP_TENANT_ID=$TENANT
WISP_NODE_ID=$NODE
WISP_DB=$CONFIG_DIR/wisp.db
EOF
  chmod 0600 "$CONFIG_DIR/edge.env"
else
  log "$CONFIG_DIR/edge.env exists — leaving it (update never overwrites identity)"
fi

# --- unprivileged ICMP + systemd -------------------------------------------
log "enabling unprivileged ICMP (kernel ping group)…"
sysctl -w net.ipv4.ping_group_range="0 2147483647" >/dev/null
echo 'net.ipv4.ping_group_range=0 2147483647' > /etc/sysctl.d/99-wisp-ping.conf

log "installing systemd unit…"
curl -fsSL "$BASE_URL/wisp-edge.service" -o /etc/systemd/system/wisp-edge.service \
  2>/dev/null || cp "$(dirname "$0")/wisp-edge.service" /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now wisp-edge

log "done. Node $TENANT/$NODE reporting to $CENTRAL."
log "  logs:   journalctl -u wisp-edge -f"
log "  status: systemctl status wisp-edge"
