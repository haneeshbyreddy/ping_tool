#!/usr/bin/env bash
#
# HANSA — one-shot setup + run.
#
# Pure stdlib, zero install. Migrates the DB, seeds the demo network, then runs
# the two decoupled runtimes together: the polling daemon (background) and the
# dashboard web server (foreground). Ctrl-C stops both.
#
#   ./run.sh                 # setup (if needed) + run on http://127.0.0.1:8000
#   ./run.sh --reset         # wipe + reseed the demo network first
#   ./run.sh --demo          # prepopulate fast demo outages so the UI isn't empty
#   ./run.sh --port 9000     # serve on a different port
#   ./run.sh --no-daemon     # dashboard only (don't start the worker)
#   ./run.sh --setup-only    # migrate + seed, then exit
#
# Env overrides: PYTHON (default python3), HOST (127.0.0.1), PORT (8000).
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
RESET=0; DEMO=0; NO_DAEMON=0; SETUP_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --reset)       RESET=1 ;;
    --demo)        DEMO=1 ;;
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

command -v "$PY" >/dev/null 2>&1 || { echo "error: '$PY' not found" >&2; exit 1; }
echo "▸ using $("$PY" --version 2>&1)"

# 1. Schema (idempotent) -----------------------------------------------------
echo "▸ migrating database…"
"$PY" -m wisp.database.client >/dev/null
echo "  done (data/wisp.db)"

# 2. Demo network ------------------------------------------------------------
if [ "$RESET" -eq 1 ]; then
  echo "▸ reseeding demo network (--reset)…"
  "$PY" -m wisp.database.seed --reset >/dev/null
else
  echo "▸ seeding demo network (only if empty)…"
  "$PY" -m wisp.database.seed >/dev/null || true
fi

# 3. Optional: fast burst so the dashboard has live outages immediately ------
if [ "$DEMO" -eq 1 ]; then
  echo "▸ generating demo outages (fast burst)…"
  "$PY" apps/daemon/main.py --interval 1 --cycles 13 >/dev/null
fi

if [ "$SETUP_ONLY" -eq 1 ]; then
  echo "✓ setup complete."
  exit 0
fi

# 4. Run the two runtimes ----------------------------------------------------
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
