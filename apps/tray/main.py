from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import traceback
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from wisp.runtime import edge_status
from wisp.runtime.win32tray import TrayApp, make_circle_icon

TASK_NAME = "WISP-Edge"
CREATE_NO_WINDOW = 0x08000000
MB_YESNO, MB_ICONWARNING, IDYES = 0x4, 0x30, 6

_COLORS = {
    edge_status.STATE_OK: (52, 168, 83),
    edge_status.STATE_STARTING: (251, 188, 4),
    edge_status.STATE_DEGRADED: (251, 188, 4),
    edge_status.STATE_STALE: (217, 48, 37),
    edge_status.STATE_ERROR: (217, 48, 37),
    edge_status.STATE_UNKNOWN: (128, 128, 134),
}

def _config_dir() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "WISP"

def _load_config() -> dict[str, str]:
    env_file = _config_dir() / "edge.env.ps1"
    try:
        return edge_status.parse_env_ps1(env_file.read_text())
    except OSError:
        return {}

def _status_file(cfg: dict[str, str]) -> Path:
    db = cfg.get("WISP_DB") or str(_config_dir() / "wisp.db")
    return edge_status.status_path(db)

def _task_installed() -> bool:
    try:
        return subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
        ).returncode == 0
    except OSError:
        return False

def _run_elevated(command: str) -> None:
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        f"-NoProfile -WindowStyle Hidden -Command {command}", None, 0)

def _confirm(text: str, caption: str) -> bool:
    return ctypes.windll.user32.MessageBoxW(
        None, text, caption, MB_YESNO | MB_ICONWARNING) == IDYES

def _open(path_or_url: str) -> None:
    try:
        os.startfile(path_or_url)
    except OSError:
        pass

class TrayController:
    def __init__(self) -> None:
        self.cfg = _load_config()
        self._icons = {state: make_circle_icon(rgb) for state, rgb in _COLORS.items()}
        self.view = edge_status.StatusView(edge_status.STATE_UNKNOWN, "reading status…")
        self.installed = True
        self.app = TrayApp(refresh=self.refresh, build_menu=self.menu)

    def refresh(self) -> tuple[int, str]:
        self.cfg = self.cfg or _load_config()
        self.installed = _task_installed()
        if not self.installed:
            self.view = edge_status.StatusView(
                edge_status.STATE_UNKNOWN, "probe task not installed")
        else:
            self.view = edge_status.read_status(_status_file(self.cfg))
        node = self.cfg.get("WISP_NODE_ID", "")
        tip = f"WISP Edge{f' [{node}]' if node else ''}: {self.view.detail}"
        return self._icons[self.view.state], tip

    def menu(self):
        from wisp.version import VERSION
        running_ish = self.view.state in (
            edge_status.STATE_OK, edge_status.STATE_STARTING, edge_status.STATE_DEGRADED)
        start_label = "Restart probe" if running_ish else "Start probe"
        dashboard = (self.cfg.get("WISP_CENTRAL_URL") or "").strip()
        return [
            (f"WISP Edge v{VERSION}", False, None),
            (self.view.detail, False, None),
            None,
            ("Open dashboard", bool(dashboard), lambda: _open(dashboard)),
            ("Open log folder", True, lambda: _open(str(_config_dir() / "logs"))),
            None,
            (start_label, self.installed, self.start_probe),
            None,
            ("Uninstall WISP Edge…", True, self.uninstall),
            ("Exit and stop monitoring", True, self.exit_and_stop),
        ]

    def start_probe(self) -> None:
        _run_elevated(
            f'"schtasks /End /TN {TASK_NAME}; Start-Sleep 1; schtasks /Run /TN {TASK_NAME}"')

    def exit_and_stop(self) -> None:
        if not _confirm(
            "Stop monitoring and close?\n\nThis machine will stop probing and stop "
            "reporting to central until the probe is started again (from this menu, "
            "or at the next reboot).", "WISP Edge",
        ):
            return
        if self.installed:
            _run_elevated(f'"schtasks /End /TN {TASK_NAME}"')
        self.app.quit()

    def uninstall(self) -> None:
        exe_dir = Path(sys.executable).resolve().parent
        uninst = exe_dir.parent / "unins000.exe"
        if uninst.is_file():
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(uninst), None, None, 1)
        else:
            _open("ms-settings:appsfeatures")

def main() -> None:
    if not sys.platform.startswith("win"):
        print("wisp-tray is Windows-only (the probe itself is cross-platform).")
        raise SystemExit(2)
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\WispEdgeTray")
    if ctypes.windll.kernel32.GetLastError() == 183:
        raise SystemExit(0)
    TrayController().app.run()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash = Path(os.environ.get("LOCALAPPDATA", ".")) / "WISP" / "tray-crash.log"
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc())
        except OSError:
            pass
        raise
