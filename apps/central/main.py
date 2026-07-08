from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG, Config
from wisp.central.server import serve

def main() -> None:
    ap = argparse.ArgumentParser(description="WISP central ingest/aggregation server")
    ap.add_argument("--host", default=None, help="bind address (overrides WISP_CENTRAL_BIND)")
    ap.add_argument("--port", type=int, default=None, help="port (overrides WISP_CENTRAL_PORT)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for _noisy in ("httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    overrides = {}
    if args.host is not None:
        overrides["central_bind"] = args.host
    if args.port is not None:
        overrides["central_port"] = args.port
    cfg = Config(**overrides) if overrides else CONFIG

    scheme = "https" if (cfg.central_tls_cert and cfg.central_tls_key) else "http"
    print(f"WISP central -> {scheme}://{cfg.central_bind}:{cfg.central_port}  "
          f"(ingest + dashboard; db={cfg.central_db.name})")
    print("Bootstrap an account: PYTHONPATH=src python -m wisp.central.admin "
          "create-superadmin --username <you>")
    print("Ctrl-C to stop.")
    serve(cfg)

if __name__ == "__main__":
    main()
