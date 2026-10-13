#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${PACKFERRY_NODE_HOME:?Set PACKFERRY_NODE_HOME to a pinned Node/npm directory}"
: "${PACKFERRY_PYTHON_DIR:?Set PACKFERRY_PYTHON_DIR to a standalone Python 3.12 runtime with pip}"
test -x "$PACKFERRY_NODE_HOME/bin/node"
test -x "$PACKFERRY_NODE_HOME/bin/npm"
test -x "$PACKFERRY_PYTHON_DIR/bin/python3"
test "$("$PACKFERRY_NODE_HOME/bin/node" --version)" = "v26.4.0"
test "$("$PACKFERRY_NODE_HOME/bin/npm" --version)" = "11.17.0"
[[ "$("$PACKFERRY_PYTHON_DIR/bin/python3" -c 'import platform; print(platform.python_version())')" == 3.12.* ]]
"$PACKFERRY_PYTHON_DIR/bin/python3" -m pip --version
for ui in exporter importer; do
  (cd "$repo/frontend/$ui" && npm ci && npm test && npm run build)
done
stage="$repo/.build/node-release"
mkdir -p "$stage/bin" "$stage/lib/node_modules"
cp -a "$PACKFERRY_NODE_HOME/bin/node" "$stage/bin/node"
cp -a "$PACKFERRY_NODE_HOME/bin/npm" "$stage/bin/npm"
cp -a "$PACKFERRY_NODE_HOME/lib/node_modules/npm" "$stage/lib/node_modules/npm"
export PACKFERRY_NODE_HOME="$stage"
cd "$repo"
uv run --extra desktop --extra build pyinstaller --noconfirm --clean deploy/exporter/packferry.spec
sha256sum \
  "$stage/bin/node" "$stage/lib/node_modules/npm/package.json" \
  "$PACKFERRY_PYTHON_DIR/bin/python3" "$(command -v uv)" \
  "$repo/apps/exporter/npm_helper.cjs" \
  "$repo/frontend/exporter/dist/index.html" \
  "$repo/frontend/importer/dist/index.html" \
  > "$repo/dist/packferry/runtime-sources.sha256"
