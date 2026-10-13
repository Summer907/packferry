#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
test -x "$repo/dist/packferry/packferry"
mkdir -p "$repo/dist/installers"
tar -C "$repo/dist" -czf "$repo/dist/installers/packferry-0.1.0-linux-x64.tar.gz" packferry
sha256sum "$repo/dist/installers/packferry-0.1.0-linux-x64.tar.gz" > "$repo/dist/installers/packferry-0.1.0-linux-x64.tar.gz.sha256"
