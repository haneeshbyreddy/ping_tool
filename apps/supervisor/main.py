from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.config import CONFIG
from wisp.runtime.supervisor import Supervisor, consume_restart

log = logging.getLogger("wisp.supervisor")

AGENT_BIN = os.environ.get("WISP_AGENT_BIN", "")
_DEV_AGENT = [sys.executable, str(_REPO_ROOT / "apps" / "daemon" / "main.py")]

def _agent_cmd() -> list[str]:
    return [AGENT_BIN] if AGENT_BIN else _DEV_AGENT

def _current_version() -> str:
    try:
        out = subprocess.check_output(_agent_cmd() + ["--version"], text=True, timeout=30)
        return out.strip()
    except Exception:
        return ""

def _download(url: str) -> Path:
    import httpx
    # Central now mirrors release binaries and hands back a central-relative URL
    # (/download/<ver>/<name>) so the private GitHub repo stays out of the fleet's
    # reach. Resolve it against the same central this edge already reports to.
    # Absolute URLs (legacy directives, or a CDN) still pass through untouched.
    if url.startswith("/"):
        url = CONFIG.central_url.rstrip("/") + url
    dest = Path(CONFIG.db_path).parent / "agent.download"
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest

class AgentRunner:

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.stop()
        log.info("starting agent: %s", " ".join(_agent_cmd()))
        self.proc = subprocess.Popen(_agent_cmd())

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if AGENT_BIN and not Path(AGENT_BIN).exists():
        log.error("WISP_AGENT_BIN=%s does not exist", AGENT_BIN); raise SystemExit(2)

    runner = AgentRunner()
    runner.start()
    agent_path = Path(AGENT_BIN) if AGENT_BIN else Path(_DEV_AGENT[1])
    request_path = Path(CONFIG.db_path).parent / "update_request.json"
    restart_path = Path(CONFIG.db_path).parent / "restart_request.json"
    deadline = CONFIG.agent_health_deadline_s

    def restart():
        runner.start()

    def health_ok():
        return runner.alive()

    sup = Supervisor(agent_path=agent_path, backup_path=agent_path.with_suffix(".lkg"),
                     download=_download, restart=restart, stop=runner.stop,
                     health_ok=health_ok, current_version=_current_version,
                     deadline_s=deadline)

    log.info("supervisor up; managing %s (update requests: %s)", agent_path, request_path)
    try:
        while True:
            if not runner.alive():
                log.warning("agent exited — restarting"); runner.start()
            try:
                consume_restart(restart_path, stop=runner.stop, restart=runner.start)
            except Exception:
                log.exception("central-requested restart failed")
            if request_path.is_file():
                try:
                    outcome = sup.consume_request(request_path)
                    log.info("update outcome: %s", outcome)
                except Exception:
                    log.exception("update attempt failed; discarding request")
                    request_path.unlink(missing_ok=True)
            time.sleep(5)
    except KeyboardInterrupt:
        runner.stop()

if __name__ == "__main__":
    main()
