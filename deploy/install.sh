#!/usr/bin/env bash
#
# WISP — one-shot installer for a fresh Ubuntu/Debian edge box.
#
# Does everything from "code is on the box" to "the edge probe running under
# systemd": OS deps, venv, unprivileged-ICMP sysctl, the systemd unit. There is
# no local dashboard or database on the edge anymore — this box only probes and
# reports to a central server (see new-plan.md Phase C). Idempotent — safe to
# re-run to upgrade after a `git pull`.
#
# Usage (run from the repo, as root):
#     sudo deploy/install.sh
#
# It does NOT touch the firewall and it does NOT clone the private repo (that
# needs your credentials — clone/scp the code to its final home first, then run
# this from inside it). You'll still need to edit the installed systemd unit
# with your central server's URL/token/tenant — see the notes it prints.
set -euo pipefail

# --- resolve the repo root (this script lives in <repo>/deploy) -------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

err() { echo "✗ $*" >&2; exit 1; }
log() { echo "▸ $*"; }

[ "$(id -u)" -eq 0 ] || err "run as root:  sudo deploy/install.sh"

log "installing into: $REPO_ROOT"

# --- 1. OS prerequisites ----------------------------------------------------
log "installing OS packages (git, python venv/pip)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip >/dev/null

# --- 2. venv + the daemon deps -----------------------------------------------
if [ ! -d "$VENV" ]; then
  log "creating venv at $VENV…"
  python3 -m venv "$VENV"
fi
log "installing Python deps (icmplib, httpx)…"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$REPO_ROOT/requirements.txt"
# Fail loud HERE if the venv is broken — a probe that can't import icmplib/httpx
# must not reach "running" only to die on the first poll. Cheap insurance on a
# firewalled/offline LAN box where a partial wheel would otherwise pass silently.
"$VENV/bin/python" -c "import icmplib, httpx" \
  || err "venv deps failed to import (icmplib/httpx) — check the pip output above (offline box? proxy needed?)"

# --- 3. unprivileged ICMP (ping sockets, no root needed at runtime) ---------
# Persist the setting AND apply it now. Apply only our key with `-w` rather than
# `sysctl --system`, which re-applies every drop-in on the box — one unrelated
# broken key (common in containers) would otherwise abort the whole install.
log "enabling kernel ping group (unprivileged ICMP)…"
echo 'net.ipv4.ping_group_range=0 2147483647' > /etc/sysctl.d/99-wisp-ping.conf
sysctl -w net.ipv4.ping_group_range="0 2147483647" >/dev/null

# --- 4. systemd unit ---------------------------------------------------------
# The shipped unit assumes /opt/wisp. If the repo is elsewhere, rewrite the
# paths on the way in so this works from any install dir.
log "installing systemd unit (pointed at $REPO_ROOT)…"
sed -e "s#/opt/wisp/.venv/bin/python#$VENV/bin/python#g" \
    -e "s#WorkingDirectory=/opt/wisp#WorkingDirectory=$REPO_ROOT#g" \
    -e "s#/opt/wisp/apps#$REPO_ROOT/apps#g" \
    "$REPO_ROOT/deploy/wisp-monitor.service" > /etc/systemd/system/wisp-monitor.service

# Only drive systemctl when systemd is actually the init (it isn't in many
# containers / WSL). Otherwise install the unit and tell the operator how to
# start it by hand, rather than hard-failing.
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  SYSTEMD=1
else
  log "systemd is not running here — unit installed but not started."
  SYSTEMD=0
fi

# --- done -------------------------------------------------------------------
echo
echo "✓ WISP edge probe is installed."
echo
echo "  Before starting it, edit the central connection settings:"
echo "    sudo systemctl edit --full wisp-monitor"
echo "    # set WISP_CENTRAL_URL / WISP_CENTRAL_TOKEN / WISP_TENANT_ID (and"
echo "    # optionally WISP_NODE_ID) to the values your central operator gave you."
echo
if [ "${SYSTEMD:-0}" -eq 1 ]; then
  echo "  Then start it:"
  echo "    sudo systemctl enable --now wisp-monitor"
  echo "    journalctl -u wisp-monitor -f"
else
  echo "  systemd isn't active here — run it manually instead:"
  echo "    WISP_CENTRAL_URL=... WISP_CENTRAL_TOKEN=... WISP_TENANT_ID=... \\"
  echo "      $VENV/bin/python $REPO_ROOT/apps/daemon/main.py"
fi
echo
echo "  All device topology, team, alert routing, and outage history now live"
echo "  on your central server's dashboard — this box has no UI of its own."
echo
echo "  Re-run this script any time after a 'git pull' to upgrade."
