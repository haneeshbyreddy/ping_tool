#!/usr/bin/env bash
set -euo pipefail

err() { echo "build-deb: $*" >&2; exit 1; }

[ $# -eq 5 ] || err "usage: build-deb.sh <version> <plat> <agent-bin> <supervisor-bin> <outdir>"
VERSION="$1" PLAT="$2" AGENT="$3" SUPERVISOR="$4" OUTDIR="$5"
HERE="$(cd "$(dirname "$0")" && pwd)"

case "$PLAT" in
  linux-amd64) ARCH=amd64;;
  linux-arm64) ARCH=arm64;;
  *) err "unsupported platform '$PLAT' (linux-amd64 / linux-arm64)";;
esac
[ -f "$AGENT" ] || err "agent binary not found: $AGENT"
[ -f "$SUPERVISOR" ] || err "supervisor binary not found: $SUPERVISOR"

case "$VERSION" in
  [0-9]*) DEBVER="$VERSION";;
  *)      DEBVER="0.0.0+git.$VERSION";;
esac
DEBVER="${DEBVER//-/\~}"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 0755 "$stage"

install -d "$stage/DEBIAN" "$stage/opt/wisp/bin" "$stage/etc/wisp" \
  "$stage/lib/systemd/system" "$stage/etc/sysctl.d"
install -m 0755 "$AGENT"      "$stage/opt/wisp/bin/wisp-edge"
install -m 0755 "$SUPERVISOR" "$stage/opt/wisp/bin/wisp-supervisor"
install -m 0644 "$HERE/wisp-edge.service" "$stage/lib/systemd/system/wisp-edge.service"

echo 'net.ipv4.ping_group_range=0 2147483647' > "$stage/etc/sysctl.d/99-wisp-ping.conf"
chmod 0644 "$stage/etc/sysctl.d/99-wisp-ping.conf"

cat > "$stage/etc/wisp/edge.env" <<'EOF'
# WISP edge — identity + central connection. Fill in, then:
#   sudo systemctl enable --now wisp-edge
WISP_CENTRAL_URL=
WISP_CENTRAL_TOKEN=
WISP_ORG_ID=default
# WISP_NODE_ID defaults to this host's hostname; uncomment to override.
#WISP_NODE_ID=
WISP_DB=/etc/wisp/wisp.db
EOF
chmod 0600 "$stage/etc/wisp/edge.env"

cat > "$stage/DEBIAN/control" <<EOF
Package: wisp-edge
Version: $DEBVER
Architecture: $ARCH
Maintainer: WISP <ops@localhost>
Section: net
Priority: optional
Description: WISP edge probe (agent + self-updating supervisor)
 Thin ICMP/SNMP probe that reports raw samples to WISP central, which owns
 all detection and alerting. The systemd unit runs the supervisor, which
 launches the agent and applies staged, health-gated self-updates that
 central hands out over the heartbeat channel.
EOF

cat > "$stage/DEBIAN/conffiles" <<'EOF'
/etc/wisp/edge.env
/etc/sysctl.d/99-wisp-ping.conf
EOF

cat > "$stage/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
# Unprivileged ICMP (kernel ping group) — same knob install-edge.sh used to set.
sysctl -p /etc/sysctl.d/99-wisp-ping.conf >/dev/null 2>&1 || true
chmod 0600 /etc/wisp/edge.env 2>/dev/null || true
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
    # Only (re)start when the box is actually configured — a fresh install with an
    # empty edge.env would just crash-loop against a blank central URL.
    if grep -q '^WISP_CENTRAL_URL=..*' /etc/wisp/edge.env 2>/dev/null; then
        systemctl enable wisp-edge >/dev/null 2>&1 || true
        systemctl restart wisp-edge || true
        echo "wisp-edge: check probe health any time with: /opt/wisp/bin/wisp-edge status"
    else
        echo "wisp-edge: edit /etc/wisp/edge.env (central URL + enrollment token from"
        echo "wisp-edge: the dashboard's Probes section), then: systemctl enable --now wisp-edge"
    fi
fi
exit 0
EOF

cat > "$stage/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl stop wisp-edge >/dev/null 2>&1 || true
    systemctl disable wisp-edge >/dev/null 2>&1 || true
fi
exit 0
EOF

chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"

mkdir -p "$OUTDIR"
deb="$OUTDIR/wisp-edge-${PLAT}.deb"
dpkg-deb --build --root-owner-group "$stage" "$deb" >/dev/null
echo "built $deb ($DEBVER)"
