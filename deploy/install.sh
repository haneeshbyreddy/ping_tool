#!/usr/bin/env bash
#
# WISP — one-shot installer for a fresh Ubuntu/Debian box.
#
# Does everything from "code is on the box" to "both services running under
# systemd": OS deps, venv, unprivileged-ICMP sysctl, DB migrate, systemd units.
# Idempotent — safe to re-run to upgrade after a `git pull`.
#
# Usage (run from the repo, as root):
#     sudo deploy/install.sh
#
# It does NOT touch the firewall (that needs your LAN subnet — see the notes it
# prints) and it does NOT clone the private repo (that needs your credentials —
# clone/scp the code to its final home first, then run this from inside it).
set -euo pipefail

# --- resolve the repo root (this script lives in <repo>/deploy) -------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

err() { echo "✗ $*" >&2; exit 1; }
log() { echo "▸ $*"; }

[ "$(id -u)" -eq 0 ] || err "run as root:  sudo deploy/install.sh"

log "installing into: $REPO_ROOT"

# --- 1. OS prerequisites ----------------------------------------------------
log "installing OS packages (git, python venv/pip, sqlite3)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip sqlite3 >/dev/null

# --- 2. venv + the two daemon deps ------------------------------------------
if [ ! -d "$VENV" ]; then
  log "creating venv at $VENV…"
  python3 -m venv "$VENV"
fi
log "installing Python deps (icmplib, httpx)…"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$REPO_ROOT/requirements.txt"

# --- 3. unprivileged ICMP (ping sockets, no root needed at runtime) ---------
# Persist the setting AND apply it now. Apply only our key with `-w` rather than
# `sysctl --system`, which re-applies every drop-in on the box — one unrelated
# broken key (common in containers) would otherwise abort the whole install.
log "enabling kernel ping group (unprivileged ICMP)…"
echo 'net.ipv4.ping_group_range=0 2147483647' > /etc/sysctl.d/99-wisp-ping.conf
sysctl -w net.ipv4.ping_group_range="0 2147483647" >/dev/null

# --- 4. database (idempotent migrations) ------------------------------------
log "creating / migrating database…"
PYTHONPATH="$REPO_ROOT/src" "$VENV/bin/python" -m wisp.database.client >/dev/null

# --- 5. systemd units -------------------------------------------------------
# The shipped units assume /opt/wisp. If the repo is elsewhere, rewrite the
# paths on the way in so this works from any install dir.
log "installing systemd units (pointed at $REPO_ROOT)…"
for unit in wisp-monitor wisp-dashboard; do
  sed -e "s#/opt/wisp/.venv/bin/python#$VENV/bin/python#g" \
      -e "s#WorkingDirectory=/opt/wisp#WorkingDirectory=$REPO_ROOT#g" \
      -e "s#/opt/wisp/apps#$REPO_ROOT/apps#g" \
      "$REPO_ROOT/deploy/$unit.service" > "/etc/systemd/system/$unit.service"
done

# Only drive systemctl when systemd is actually the init (it isn't in many
# containers / WSL). Otherwise install the units and tell the operator how to
# start the two processes by hand, rather than hard-failing.
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now wisp-monitor wisp-dashboard
  SYSTEMD=1
else
  log "systemd is not running here — units installed but not started."
  SYSTEMD=0
fi

# --- done -------------------------------------------------------------------
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="$(awk -F'--port ' '/--port/{print $2; exit}' "$REPO_ROOT/deploy/wisp-dashboard.service" | awk '{print $1}')"
PORT="${PORT:-8000}"

echo
if [ "${SYSTEMD:-0}" -eq 1 ]; then
  echo "✓ WISP is installed and running."
  echo
  systemctl --no-pager --lines=0 status wisp-monitor wisp-dashboard | sed -n '1,3p;/Active:/p' || true
  echo
  echo "  Dashboard:  http://${LAN_IP:-<box-lan-ip>}:${PORT}   (set the PIN on first visit)"
  echo "  Logs:       journalctl -u wisp-monitor -f"
else
  echo "✓ WISP is installed (systemd not active here, so not started)."
  echo
  echo "  Start the two processes manually from $REPO_ROOT:"
  echo "    .venv/bin/python apps/daemon/main.py &"
  echo "    .venv/bin/python apps/dashboard/main.py --host 0.0.0.0 --port ${PORT}"
  echo "  Or just: ./run.sh"
  echo
  echo "  Dashboard:  http://${LAN_IP:-<box-lan-ip>}:${PORT}   (set the PIN on first visit)"
fi
echo
echo "  Next:"
echo "   1. Lock the dashboard to your LAN (replace the subnet):"
echo "        sudo ufw allow OpenSSH"
echo "        sudo ufw allow from 192.168.1.0/24 to any port ${PORT} proto tcp"
echo "        sudo ufw enable"
echo "      Keep it OFF the public internet — use a VPN for remote access."
echo "   2. Open the dashboard, set the PIN, add devices (Nodes) + team (Team)."
echo "   3. Settings ▸ Send test alert — confirm a push lands before you trust it."
echo
echo "  Re-run this script any time after a 'git pull' to upgrade."
