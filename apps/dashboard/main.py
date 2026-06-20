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
import sys
from pathlib import Path

# --- bootstrap: make the `wisp` package importable without installing ---
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG          # noqa: E402
from wisp.server.routes import make_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="HANSA dashboard server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    server = make_server(args.host, args.port)
    print(f"HANSA dashboard → http://{args.host}:{args.port}  (db={CONFIG.db_path.name})")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
