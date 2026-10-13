#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for ui in exporter importer; do
  (cd "$repo/frontend/$ui" && npm ci && npm test && npm run build)
done
cd "$repo"
docker build -f deploy/importer/Dockerfile -t packferry-importer:0.1.0 .
docker save packferry-importer:0.1.0 | gzip > deploy/importer/packferry-importer-0.1.0.tar.gz
sha256sum deploy/importer/packferry-importer-0.1.0.tar.gz > deploy/importer/packferry-importer-0.1.0.tar.gz.sha256
