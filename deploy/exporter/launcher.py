"""PyInstaller entry: a double click opens the desktop shell; arguments use CLI."""

from __future__ import annotations

import sys

from apps.exporter.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["desktop"]))
