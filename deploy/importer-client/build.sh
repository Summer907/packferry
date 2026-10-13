#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
uv run --extra build pyinstaller --noconfirm --clean deploy/importer-client/packferry-import.spec
