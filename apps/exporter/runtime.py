"""Frozen tools override host PATH; development uses the locked environment."""
import os
from pathlib import Path
import sys


def prepare_runtime():
    if not getattr(sys, 'frozen', False):
        return
    base = Path(sys._MEIPASS) / 'runtime'
    paths = [base / 'node', base / 'node' / 'bin', base / 'uv']
    os.environ['PATH'] = os.pathsep.join(str(p) for p in paths) + os.pathsep + os.environ.get('PATH', '')
    # npm config supplied by users must not accidentally execute hooks or select a mirror.
    os.environ['npm_config_ignore_scripts'] = 'true'
    os.environ['npm_config_prefix'] = str(base / 'node')


prepare_runtime()


def python_executable() -> str:
    if not getattr(sys, 'frozen', False):
        return sys.executable
    root = Path(sys._MEIPASS) / 'runtime' / 'python'
    candidate = root / 'python.exe' if sys.platform == 'win32' else root / 'bin' / 'python3'
    if not candidate.is_file():
        raise RuntimeError('Bundled Python/pip runtime is missing; rebuild the distribution')
    return str(candidate)
