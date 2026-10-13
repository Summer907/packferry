// This helper uses npm's own Arborist tree and pacote cache implementation.
const fs = require('node:fs');
const path = require('node:path');
const process = require('node:process');

async function main() {
  const [mode, npmRoot, ...args] = process.argv.slice(2);
  const npmModules = path.join(npmRoot, 'npm', 'node_modules');
  if (mode === 'tree') {
    const Arborist = require(path.join(npmModules, '@npmcli', 'arborist'));
    const [workspace, productionOnly] = args;
    const tree = await new Arborist({path: workspace}).loadVirtual();
    const queue = [tree];
    const visited = new Set();
    const edges = {};
    const missingEdges = [];
    while (queue.length) {
      const node = queue.pop();
      if (!node || visited.has(node.location)) continue;
      visited.add(node.location);
      edges[node.location] = [];
      for (const edge of node.edgesOut.values()) {
        if (productionOnly === 'true' && edge.type.startsWith('dev')) continue;
        if (!edge.to || edge.error) {
          missingEdges.push({from: node.location, name: edge.name, type: edge.type, error: String(edge.error || 'MISSING')});
          continue;
        }
        edges[node.location].push({to: edge.to.location, type: edge.type, name: edge.name});
        queue.push(edge.to);
      }
    }
    process.stdout.write(JSON.stringify({paths: [...visited].filter(Boolean), edges, missingEdges}));
  } else if (mode === 'fetch') {
    const pacote = require(path.join(npmModules, 'pacote'));
    const [url, integrity, output, cache, registry] = args;
    await pacote.tarball.file(url, output, {integrity, cache, registry});
    process.stdout.write(JSON.stringify({size: fs.statSync(output).size}));
  } else {
    throw new Error('Unknown helper mode');
  }
}

main().catch((error) => {
  process.stderr.write(String(error && error.code || error && error.name || 'NPM_HELPER_ERROR'));
  process.exitCode = 1;
});
