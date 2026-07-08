#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

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

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

export WISP_CENTRAL_URL="${WISP_CENTRAL_URL:-http://127.0.0.1:${PORT}}"
export WISP_ORG_ID="${WISP_ORG_ID:-default}"
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
  echo "▸ starting edge probe in the background (central-brain mode, org=$WISP_ORG_ID)…"
  "$PY" apps/daemon/main.py >/tmp/hansa_daemon.log 2>&1 &
  DAEMON_PID=$!
  echo "  probe pid=$DAEMON_PID  (log: /tmp/hansa_daemon.log — it'll idle/error until"
  echo "  central has at least one device for org \"$WISP_ORG_ID\", see below)"
fi

echo "▸ starting central → http://${HOST}:${PORT}"
echo
echo "  First run: create a superadmin, then log in and add devices/team from"
echo "  the dashboard (org \"$WISP_ORG_ID\" if you kept the default):"
echo "    PYTHONPATH=src $PY -m wisp.central.admin create-superadmin --username you --password ..."
echo
echo "  (Ctrl-C to stop everything)"
"$PY" apps/central/main.py --host "$HOST" --port "$PORT"
