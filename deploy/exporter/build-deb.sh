#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
portable="$repo/dist/packferry"
test -x "$portable/packferry"
command -v dpkg-deb >/dev/null
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/DEBIAN" "$work/opt/packferry" "$work/usr/bin" "$repo/dist/installers"
cp -a "$portable/." "$work/opt/packferry/"
cat > "$work/DEBIAN/control" <<'EOF'
Package: packferry
Version: 0.1.0
Section: utils
Priority: optional
Architecture: amd64
Maintainer: PackFerry maintainers
Description: Offline PyPI and npm package bundle exporter
EOF
cat > "$work/usr/bin/packferry" <<'EOF'
#!/bin/sh
exec /opt/packferry/packferry "$@"
EOF
chmod 755 "$work/usr/bin/packferry"
dpkg-deb --build --root-owner-group "$work" "$repo/dist/installers/packferry_0.1.0_amd64.deb"
sha256sum "$repo/dist/installers/packferry_0.1.0_amd64.deb" > "$repo/dist/installers/packferry_0.1.0_amd64.deb.sha256"
