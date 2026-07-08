import os
from PyInstaller.utils.hooks import collect_submodules

_ROOT = os.path.dirname(SPECPATH)

hidden = (["icmplib", "httpx"]
          + collect_submodules("pysnmp")
          + collect_submodules("pyasn1"))

a = Analysis(
    [os.path.join(_ROOT, "apps", "daemon", "main.py")],
    pathex=[os.path.join(_ROOT, "src")],
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
    upx=False,
)
