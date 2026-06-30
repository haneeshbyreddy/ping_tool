#!/bin/sh
# postinstall for the wisp-edge .deb/.rpm. Mirrors deploy/install-edge.sh's OS setup: a config
# dir that survives upgrades, unprivileged ICMP, and the systemd unit enabled. It deliberately
# does NOT start the service — the node needs its identity (/etc/wisp/edge.env) first, which the
# package can't know (the enrollment token is per-fleet).
set -e

# Config + identity + DB dir — survives package upgrades; the package never ships edge.env.
install -d -m 0755 /etc/wisp

# Unprivileged ICMP via the kernel ping group (no root/cap_net_raw needed at runtime).
if [ -d /etc/sysctl.d ]; then
  echo 'net.ipv4.ping_group_range=0 2147483647' > /etc/sysctl.d/99-wisp-ping.conf
  sysctl -w net.ipv4.ping_group_range="0 2147483647" >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl enable wisp-edge.service || true
fi

cat <<'MSG'
─────────────────────────────────────────────────────────────────────
 WISP edge installed to /opt/wisp/bin (agent + supervisor).

 Before starting, write /etc/wisp/edge.env with this node's identity:

   WISP_CENTRAL_URL=https://central.example.net
   WISP_CENTRAL_TOKEN=<the fleet ingest token>
   WISP_TENANT_ID=<your org id>
   WISP_NODE_ID=<this node's id>
   WISP_DB=/etc/wisp/wisp.db

 Then start it:
   sudo systemctl start wisp-edge
   journalctl -u wisp-edge -f

 (Leave WISP_CENTRAL_URL empty to run as a standalone monitor — no central.)
─────────────────────────────────────────────────────────────────────
MSG
