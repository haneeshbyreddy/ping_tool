# PyInstaller spec — the frozen edge AGENT (the polling daemon), one binary per platform/arch.
#
# Build (on a native runner for each target; CI does win-amd64 / linux-amd64 / linux-arm64):
#     pip install -r requirements.txt pyinstaller
#     pyinstaller --clean -y deploy/wisp-edge.spec      # -> dist/wisp-edge
#
# The daemon lazy-imports icmplib / httpx / pysnmp, so PyInstaller's static analysis can't see
# them — they are force-included below, or the frozen binary would ImportError at runtime on the
# very probes/notifier/SNMP it exists to run. The supervisor is built from the same tree with
# entry apps/supervisor/main.py (a sibling spec or --name override); the agent is the hot path.
from PyInstaller.utils.hooks import collect_submodules

hidden = (["icmplib", "httpx"]
          + collect_submodules("pysnmp")
          + collect_submodules("pyasn1"))

a = Analysis(
    ["apps/daemon/main.py"],
    pathex=["src"],            # the src-layout package root (apps/* normally add this at runtime)
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="wisp-edge",
    console=True,
    strip=False,
    upx=False,                 # leave UPX off — it trips some AV/SmartScreen heuristics
)
