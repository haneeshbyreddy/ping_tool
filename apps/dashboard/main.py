"""Dashboard runtime — the web UI process.

One of the two decoupled runtimes (the other is apps/daemon). It only reads the
WAL database (plus the ack/post-mortem/device-CRUD writes), so it runs happily
alongside the polling daemon.

    python apps/dashboard/main.py                    # http://127.0.0.1:8000
    python apps/dashboard/main.py --host 0.0.0.0 --port 9000

Zero-install: this entry point puts <repo>/src on sys.path, so no packaging or
PYTHONPATH is needed.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# --- bootstrap: make the `wisp` package importable without installing ---
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG          # noqa: E402
from wisp.server.routes import make_server  # noqa: E402
from wisp.server.watchdog import start_watchdog_thread  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="HANSA dashboard server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Windows consoles / redirected pipes default to a legacy code page (cp1252/cp437)
    # that can't encode the arrows & dashes in our log lines — that crashed the
    # dashboard on startup. Force UTF-8 (replace any holdout glyph) so a stray
    # character can never take the process down.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every ntfy POST
    # The dashboard process watches the polling daemon: if the daemon dies, this
    # pages the owner instead of silently showing an all-green network.
    start_watchdog_thread(CONFIG)

    server = make_server(args.host, args.port)
    print(f"HANSA dashboard -> http://{args.host}:{args.port}  (db={CONFIG.db_path.name})")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
