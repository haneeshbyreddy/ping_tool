#!/usr/bin/env bash
#
# HANSA — local dev: run central + one edge probe together.
#
# The edge no longer has its own dashboard/DB (new-plan.md Phase C) — central IS
# the product now. This starts a local central server (foreground) and, unless
# --no-daemon, an edge probe in central-brain mode (background) pointed at it.
# Ctrl-C stops both.
#
#   ./run.sh                 # central on http://0.0.0.0:8080 + a local edge probe
#   ./run.sh --port 9000     # serve central on a different port
#   ./run.sh --no-daemon     # central only (don't start the probe)
#
# First run: create a superadmin, log into the dashboard, and add at least one
# device under tenant "default" (Nodes ▸ Add) before the probe has anything to
# report — see the printed instructions below.
#
# Real ICMP polling needs raw sockets — install deps in a venv (see README
# "Going live"). Env overrides: PYTHON (default python3), HOST (0.0.0.0),
# PORT (8080).
set -euo pipefail

cd "$(dirname "$0")"

# Prefer the project venv — the edge probe needs icmplib/httpx, and running it
# under a bare system python silently degrades to "every host 100% loss" (the
# prober's import fails and is swallowed as a probe failure). Honour an explicit
# $PYTHON, else use .venv if present, else fall back to system python3.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
NO_DAEMON=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-daemon)   NO_DAEMON=1 ;;
    --host)        HOST="$2"; shift ;;
    --port)        PORT="$2"; shift ;;
    -h|--help)     sed -n '3,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# The src/ layout is not installed; expose the package on the path for the CLIs.
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

# Point the edge probe at the central instance this script is about to start.
# No token needed for local dev (an unset WISP_CENTRAL_TOKEN leaves central's
# ingest open); set one yourself before exposing this beyond localhost.
export WISP_CENTRAL_BRAIN=1
export WISP_CENTRAL_URL="${WISP_CENTRAL_URL:-http://127.0.0.1:${PORT}}"
export WISP_TENANT_ID="${WISP_TENANT_ID:-default}"
export WISP_POLL_INTERVAL_S="${WISP_POLL_INTERVAL_S:-20}"
export WISP_RETRY_INTERVAL_S="${WISP_RETRY_INTERVAL_S:-1}"

command -v "$PY" >/dev/null 2>&1 || { echo "error: '$PY' not found" >&2; exit 1; }
echo "▸ using $("$PY" --version 2>&1)"

DAEMON_PID=""
cleanup() {
  [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ "$NO_DAEMON" -eq 0 ]; then
  echo "▸ starting edge probe in the background (central-brain mode, tenant=$WISP_TENANT_ID)…"
  "$PY" apps/daemon/main.py >/tmp/hansa_daemon.log 2>&1 &
  DAEMON_PID=$!
  echo "  probe pid=$DAEMON_PID  (log: /tmp/hansa_daemon.log — it'll idle/error until"
  echo "  central has at least one device for tenant \"$WISP_TENANT_ID\", see below)"
fi

echo "▸ starting central → http://${HOST}:${PORT}"
echo
echo "  First run: create a superadmin, then log in and add devices/team from"
echo "  the dashboard (tenant \"$WISP_TENANT_ID\" if you kept the default):"
echo "    PYTHONPATH=src $PY -m wisp.central.admin create-superadmin --username you --password ..."
echo
echo "  (Ctrl-C to stop everything)"
"$PY" apps/central/main.py --host "$HOST" --port "$PORT"
