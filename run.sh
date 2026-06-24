#!/usr/bin/env bash
#
# HANSA — one-shot setup + run.
#
# Migrates the DB, then runs the two decoupled runtimes together: the polling
# daemon (background) and the dashboard web server (foreground). Ctrl-C stops
# both. Add your real devices + team from the dashboard once it's up.
#
#   ./run.sh                 # setup (if needed) + run on http://0.0.0.0:8080
#   ./run.sh --port 9000     # serve on a different port
#   ./run.sh --no-daemon     # dashboard only (don't start the worker)
#   ./run.sh --setup-only    # migrate, then exit
#
# Real ICMP polling needs raw sockets — install deps in a venv and grant
# cap_net_raw (see README "Going live"). Env overrides: PYTHON (default
# python3), HOST (127.0.0.1), PORT (8000).
set -euo pipefail

cd "$(dirname "$0")"

# Prefer the project venv — the daemon needs icmplib/httpx, and running it under a
# bare system python silently degrades to "every host 100% loss / uplink down"
# (the prober's import fails and is swallowed as a probe failure). Honour an explicit
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
NO_DAEMON=0; SETUP_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-daemon)   NO_DAEMON=1 ;;
    --setup-only)  SETUP_ONLY=1 ;;
    --host)        HOST="$2"; shift ;;
    --port)        PORT="$2"; shift ;;
    -h|--help)     sed -n '3,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# The src/ layout is not installed; expose the package on the path for the CLIs.
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

# Role alert channels (ntfy topics). Unguessable so they can't be read on the
# public ntfy.sh; each person subscribes to the topic for their role. `:=` keeps
# any value already set in the environment, so you can override per-host.
export WISP_NTFY_TOPIC_OWNER="${WISP_NTFY_TOPIC_OWNER:=hansa-owner-35f027e3a8}"
export WISP_NTFY_TOPIC_OPERATOR="${WISP_NTFY_TOPIC_OPERATOR:=hansa-ops-428fe896b8}"
export WISP_NTFY_TOPIC_TECH="${WISP_NTFY_TOPIC_TECH:=hansa-tech-87e2965d5e}"

# Detection speed: poll every 20s, but the between-cycle watch + fast-confirm probe
# changed hosts every WISP_RETRY_INTERVAL_S and confirm in seconds (DOWN ~5s, UP ~4s)
# without weakening the 3-strike flap suppression. The retry probes the whole fleet each
# tick, so 1s is great for a small fleet; raise it (e.g. 2–3s) once you run hundreds of
# nodes so the box isn't pinging everything every second.
export WISP_POLL_INTERVAL_S="${WISP_POLL_INTERVAL_S:=20}"
export WISP_RETRY_INTERVAL_S="${WISP_RETRY_INTERVAL_S:=1}"

command -v "$PY" >/dev/null 2>&1 || { echo "error: '$PY' not found" >&2; exit 1; }
echo "▸ using $("$PY" --version 2>&1)"

# 1. Schema (idempotent) -----------------------------------------------------
echo "▸ migrating database…"
"$PY" -m wisp.database.client >/dev/null
echo "  done (data/wisp.db)"

if [ "$SETUP_ONLY" -eq 1 ]; then
  echo "✓ setup complete."
  exit 0
fi

# 2. Run the two runtimes ----------------------------------------------------
DAEMON_PID=""
cleanup() {
  [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ "$NO_DAEMON" -eq 0 ]; then
  echo "▸ starting polling daemon (background)…"
  "$PY" apps/daemon/main.py >/tmp/hansa_daemon.log 2>&1 &
  DAEMON_PID=$!
  echo "  daemon pid=$DAEMON_PID  (log: /tmp/hansa_daemon.log)"
fi

echo "▸ starting dashboard → http://${HOST}:${PORT}"
echo "  (Ctrl-C to stop everything)"
"$PY" apps/dashboard/main.py --host "$HOST" --port "$PORT"
