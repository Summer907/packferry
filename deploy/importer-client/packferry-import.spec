"""Minimal remote API client: no Nexus publishers or Exporter runtimes."""
from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]
a = Analysis([str(root / 'deploy/importer-client/launcher.py')], pathex=[str(root)],
             binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=['webview', 'uv', 'pip', 'twine', 'sqlalchemy'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='packferry-import', console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='packferry-import')
