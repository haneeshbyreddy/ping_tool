"""Central server runtime — the aggregation plane (Phase 10 Part A skeleton).

A SEPARATE process/host from the edges. Edges ship events/rollups/heartbeats here over
HTTPS (edge-initiated, bearer-token auth); this process persists them into the central
store and serves a fleet-wide read view. The edge keeps detecting + paging locally — central
never runs an FSM and never pages; it owns the picture, the edge owns the page.

    WISP_CENTRAL_TOKEN=s3cret WISP_CENTRAL_PORT=8443 python apps/central/main.py

Run it behind a TLS terminator (nginx/Caddy) for HTTPS in production; the skeleton itself
speaks plain HTTP so it stays dependency-free. Zero-install: this puts <repo>/src on
sys.path. (Part D builds the frozen-binary fleet path; this is the server side.)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# --- bootstrap: make the `wisp` package importable without installing ---
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG, Config  # noqa: E402
from wisp.central.server import serve   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="WISP central ingest/aggregation server")
    ap.add_argument("--host", default=None, help="bind address (overrides WISP_CENTRAL_BIND)")
    ap.add_argument("--port", type=int, default=None, help="port (overrides WISP_CENTRAL_PORT)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # CLI overrides win over the env-var Config (same precedence as the daemon's --interval).
    overrides = {}
    if args.host is not None:
        overrides["central_bind"] = args.host
    if args.port is not None:
        overrides["central_port"] = args.port
    cfg = Config(**overrides) if overrides else CONFIG

    print(f"WISP central -> http://{cfg.central_bind}:{cfg.central_port}  "
          f"(db={cfg.central_db.name})")
    print("Ctrl-C to stop.")
    serve(cfg)


if __name__ == "__main__":
    main()
