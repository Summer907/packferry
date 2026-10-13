"""Build from the repository root after both frontend production builds.

Set PACKFERRY_NODE_HOME to pinned Node/npm and PACKFERRY_PYTHON_DIR to a
self-contained Python 3.12 runtime with pip before invoking PyInstaller.
"""

from pathlib import Path
import os
import shutil

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve().parents[1]
node_home = Path(os.environ.get("PACKFERRY_NODE_HOME", ""))
if not node_home.is_dir():
    raise SystemExit("PACKFERRY_NODE_HOME must point to a pinned Node/npm runtime directory")
python_home = Path(os.environ.get("PACKFERRY_PYTHON_DIR", ""))
python_executable = python_home / ("python.exe" if os.name == "nt" else "bin/python3")
if not python_executable.is_file():
    raise SystemExit("PACKFERRY_PYTHON_DIR must contain a standalone Python 3.12 runtime")
uv_executable = shutil.which("uv")
if not uv_executable:
    raise SystemExit("uv executable is required in the build environment")
for ui in ("exporter", "importer"):
    if not (root / "frontend" / ui / "dist" / "index.html").is_file():
        raise SystemExit(f"Build frontend/{ui} before PyInstaller")

uv_datas, uv_binaries, uv_hiddenimports = collect_all("uv")
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")
datas = [
    (str(root / "frontend" / "exporter" / "dist"), "frontend/exporter/dist"),
    (str(root / "frontend" / "importer" / "dist"), "frontend/importer/dist"),
    (str(node_home), "runtime/node"),
    (str(python_home), "runtime/python"),
    (str(root / "apps" / "exporter" / "npm_helper.cjs"), "apps/exporter"),
    *uv_datas,
    *webview_datas,
]
binaries = [(uv_executable, "runtime/uv"), *uv_binaries, *webview_binaries]

a = Analysis(
    [str(root / "deploy" / "exporter" / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["apps.exporter.api", "apps.exporter.desktop", "pip", *uv_hiddenimports, *webview_hiddenimports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="packferry", console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="packferry")
