#!/bin/sh
# preremove for the wisp-edge .deb/.rpm — stop + disable the service before the binaries go.
# Leaves /etc/wisp (the DB + identity) in place so a reinstall keeps the node's history.
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop wisp-edge.service 2>/dev/null || true
  systemctl disable wisp-edge.service 2>/dev/null || true
fi
