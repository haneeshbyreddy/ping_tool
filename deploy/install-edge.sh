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
# and starts the systemd service. Supply-chain note: serve this over HTTPS and verify the sha256.
# If a minisign public key is available (deploy/minisign.pub, shipped alongside this script, or
# fetched from $BASE_URL) AND a release actually published a SHA256SUMS.minisig
# (.github/workflows/release.yml's "Sign checksums (minisign)" step — no-op until the operator
# sets the MINISIGN_KEY secret), the checksums manifest's signature is verified too, hard-failing
# on a mismatch. Until then this degrades to sha256-only, same as before — never ship an
# unverified pipe-to-shell once signing IS configured.
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

# --- minisign (optional, self-activating once the operator publishes a signed release) -----
# Best-effort fetch of the public key + the checksums signature — neither exists until the
# operator generates a keypair (deploy/minisign.pub, committed to the repo) and sets the
# MINISIGN_KEY Actions secret, so a 404 here is normal on an unsigned release, not an error.
PUBKEY=""
if curl -fsSL "$BASE_URL/minisign.pub" -o "$tmp/minisign.pub" 2>/dev/null; then
  PUBKEY="$tmp/minisign.pub"
elif [ -f "$(dirname "$0")/minisign.pub" ]; then
  PUBKEY="$(dirname "$0")/minisign.pub"
fi

if [ -n "$PUBKEY" ] && curl -fsSL "$BASE_URL/SHA256SUMS.minisig" -o "$tmp/SHA256SUMS.minisig" 2>/dev/null; then
  command -v minisign >/dev/null 2>&1 || {
    log "minisign not installed — installing (apt)…"
    apt-get update -qq && apt-get install -y -qq minisign >/dev/null
  }
  log "verifying minisign signature over SHA256SUMS…"
  minisign -V -p "$PUBKEY" -m "$tmp/SHA256SUMS" -x "$tmp/SHA256SUMS.minisig" -q \
    || err "minisign signature verification FAILED — refusing to install (checksums manifest may be tampered)"
  log "minisign signature OK."
else
  log "no minisign public key / signature published yet — sha256-only verification (see deploy/minisign.pub)."
fi

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
