"""npm CLI resolution and original Registry tarball collection."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from packages.common.models import Artifact, Environment, Package, Resolution, Warning
from packages.common.archives import bounded_tar_info
from .adapters import AdapterResult, _run, check_cancelled
from .storage import ArtifactStore


def _package_name(path: str) -> str:
    return path.split("node_modules/")[-1]


def _resolve_instance(owner_path: str, name: str, packages: dict) -> str | None:
    current = owner_path
    while True:
        candidate = f"{current}/node_modules/{name}" if current else f"node_modules/{name}"
        if candidate in packages:
            return candidate
        if not current:
            return None
        current = current.rsplit("/node_modules/", 1)[0] if "/node_modules/" in current else ""


def _read_tarball_package_json(path: Path) -> dict:
    with tarfile.open(path, "r:gz", tarinfo=bounded_tar_info(1024 * 1024, 100_000)) as archive:
        entries = [member for member in archive if member.name == "package/package.json"]
        if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 1024 * 1024:
            raise ValueError("npm tarball has missing, ambiguous or oversized package.json")
        stream = archive.extractfile(entries[0])
        if stream is None:
            raise ValueError("npm package metadata missing")
        return json.loads(stream.read(1024 * 1024 + 1))


class NpmAdapter:
    def __init__(self, store: ArtifactStore, npm: str = "npm", registry: str = "https://registry.npmjs.org/"):
        self.store, self.npm, self.registry = store, npm, registry

    def resolve(self, packages: list[str], project_path: Path | None, environments: list[Environment], production_only: bool = False, prefer_lock: bool = True) -> AdapterResult:
        result = AdapterResult()
        with tempfile.TemporaryDirectory(prefix="packferry-npm-") as temporary:
            workspace = Path(temporary)
            if project_path:
                project = Path(project_path)
                if project.is_dir():
                    manifest_path = project / "package.json"
                    lock_path = project / "package-lock.json"
                elif project.name == "package-lock.json":
                    manifest_path = project.with_name("package.json")
                    lock_path = project
                else:
                    manifest_path = project
                    lock_path = project.with_name("package-lock.json")
                if not manifest_path.is_file():
                    raise ValueError("package.json is required")
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                    for name, value in data.get(key, {}).items():
                        if _special_spec(value):
                            raise ValueError(f"{name}: non-Registry dependency cannot be safely resolved without running untrusted source preparation")
                (workspace / "package.json").write_text(json.dumps(data), encoding="utf-8")
                if prefer_lock and lock_path.is_file():
                    shutil.copyfile(lock_path, workspace / "package-lock.json")
            else:
                dependencies = {}
                for specification in packages:
                    name, constraint = _parse_package_spec(specification)
                    if _special_spec(constraint):
                        raise ValueError("Direct URL, Git, file and workspace npm specifications are unsupported")
                    dependencies[name] = constraint
                data = {"name": "packferry-input", "version": "0.0.0", "private": True, "dependencies": dependencies}
                (workspace / "package.json").write_text(json.dumps(data), encoding="utf-8")
            if not (workspace / "package-lock.json").exists():
                _run([self.npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund", "--registry", self.registry], cwd=workspace)
            npm_root = _run([self.npm, "root", "-g"]).strip()
            lock = json.loads((workspace / "package-lock.json").read_text(encoding="utf-8"))
            if lock.get("lockfileVersion") not in (2, 3) or not isinstance(lock.get("packages"), dict):
                raise ValueError("Only npm package-lock v2/v3 with packages map is supported")
            lock_packages = lock["packages"]
            unsafe_lock_nodes = [path for path, item in lock_packages.items() if path and item.get("resolved") and not _registry_url(item.get("resolved", ""), self.registry)]
            locked_project = bool(project_path and prefer_lock and lock_path.is_file())
            platform_gaps = ({environment.environment_id: "Special-source lock nodes prevent safe platform verification" for environment in environments} if unsafe_lock_nodes else self._platform_audit(workspace, lock, environments, locked_project))
            if unsafe_lock_nodes:
                result.warnings.append(Warning(code="NPM_SPECIAL_SOURCE", severity="warning", message=f"Lock contains {len(unsafe_lock_nodes)} non-Registry sources; no source preparation was executed"))
            if not locked_project:
                (workspace / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            tree = json.loads(_run(["node", str(_helper_path()), "tree", npm_root, str(workspace), "true" if production_only else "false"]))
            selected_paths = set(tree["paths"])
            selected_paths &= set(lock_packages)
            edge_map = tree["edges"]
            for environment in environments:
                result.resolutions.append(Resolution(environment_id=environment.environment_id, status="COMPLETE"))
            if tree.get("missingEdges"):
                result.warnings.append(Warning(code="NPM_INCOMPLETE_TREE", severity="warning", affected_environment_ids=[environment.environment_id for environment in environments], message=f"Arborist found {len(tree['missingEdges'])} missing or invalid dependency edges"))
                for resolution in result.resolutions:
                    resolution.status = "PARTIAL"
            for environment_id, message in platform_gaps.items():
                result.warnings.append(Warning(code="NPM_CROSS_PLATFORM_LOCK_GAP", severity="warning", affected_environment_ids=[environment_id], message=message))
                for resolution in result.resolutions:
                    if resolution.environment_id == environment_id:
                        resolution.status = "PARTIAL"
            artifact_by_digest: dict[str, Artifact] = {}
            for path in sorted(selected_paths):
                check_cancelled()
                node = lock_packages[path]
                name = node.get("name") or _package_name(path)
                version = node.get("version")
                if not version:
                    result.warnings.append(Warning(code="NPM_UNVERSIONED_NODE", severity="warning", message=f"{path}: version missing"))
                    continue
                node_id = f"npm:{path}"
                dependency_ids = [f"npm:{edge['to']}" for edge in edge_map.get(path, []) if edge["to"] in selected_paths]
                package = Package(node_id=node_id, name=name, version=version, source=self.registry, dependencies=sorted(set(dependency_ids)), dependencies_known=True)
                result.packages.append(package)
                applicable = [resolution for resolution, environment in zip(result.resolutions, environments) if _applies(node, environment)]
                for resolution in applicable:
                    resolution.nodes.append(node_id)
                if not applicable:
                    continue
                resolved = node.get("resolved", "")
                integrity = node.get("integrity")
                if not _registry_url(resolved, self.registry) or not integrity:
                    result.warnings.append(Warning(code="NPM_NON_REGISTRY_OR_UNVERIFIED", severity="warning", affected_node_ids=[node_id], affected_environment_ids=[r.environment_id for r in applicable], message=f"{name}@{version}: no verified Registry tarball available"))
                    for resolution in applicable:
                        resolution.status = "PARTIAL"
                    continue
                try:
                    digest, source = self._fetch(resolved, integrity, npm_root)
                    tar_info = _read_tarball_package_json(source)
                    if tar_info.get("name") != name or tar_info.get("version") != version:
                        raise ValueError("npm tarball identity mismatch")
                    if any(script in tar_info.get("scripts", {}) for script in ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly")):
                        result.warnings.append(Warning(code="NPM_INSTALL_SCRIPTS", severity="warning", affected_node_ids=[node_id], affected_environment_ids=[r.environment_id for r in applicable], message=f"{name}@{version}: install lifecycle scripts may require offline resources or execute code"))
                    if digest not in artifact_by_digest:
                        artifact_by_digest[digest] = Artifact(artifact_id=digest, filename=f"{name.replace('/', '-')}-{version}.tgz", type="npm", archive_path=f"artifacts/sha256/{digest[:2]}/{digest}", size=source.stat().st_size, sha256=digest, source=resolved, integrity=integrity, package_node_ids=[node_id])
                        result.sources[digest] = source
                    else:
                        artifact_by_digest[digest].package_node_ids.append(node_id)
                    for resolution in applicable:
                        resolution.selected_artifacts.append(digest)
                except (OSError, ValueError, RuntimeError) as exc:
                    result.warnings.append(Warning(code="NPM_ARTIFACT_MISSING", severity="warning", affected_node_ids=[node_id], affected_environment_ids=[r.environment_id for r in applicable], message=f"{name}@{version}: {exc}"))
                    for resolution in applicable:
                        resolution.status = "PARTIAL"
            result.artifacts = list(artifact_by_digest.values())
            packages_by_id = {package.node_id: package for package in result.packages}
            for resolution in result.resolutions:
                resolution.dependencies = {
                    node_id: [child for child in packages_by_id[node_id].dependencies if child in resolution.nodes]
                    for node_id in resolution.nodes
                }
            return result

    def _platform_audit(self, workspace: Path, lock: dict, environments: list[Environment], locked_project: bool) -> dict[str, str]:
        """Re-resolve per target without scripts to find missing platform optionals.

        A supplied lock stays authoritative. A freshly generated lock may merge
        platform instances when their identity and integrity agree.
        """
        gaps = {}
        baseline = lock["packages"]
        for environment in environments:
            check_cancelled()
            platform = environment.platform.lower()
            os_name = "win32" if "win" in platform else "linux"
            with tempfile.TemporaryDirectory(prefix="packferry-npm-platform-") as temporary:
                probe = Path(temporary)
                shutil.copyfile(workspace / "package.json", probe / "package.json")
                if locked_project:
                    shutil.copyfile(workspace / "package-lock.json", probe / "package-lock.json")
                command = [self.npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund", f"--os={os_name}", "--cpu=x64", "--registry", self.registry]
                if os_name == "linux":
                    command.append("--libc=glibc")
                try:
                    _run(command, cwd=probe)
                    probed = json.loads((probe / "package-lock.json").read_text(encoding="utf-8"))["packages"]
                    fresh_optional = []
                    if locked_project:
                        (probe / "package-lock.json").unlink()
                        _run(command, cwd=probe)
                        independent = json.loads((probe / "package-lock.json").read_text(encoding="utf-8"))["packages"]
                        fresh_optional = sorted(path for path, item in independent.items() if path not in baseline and (item.get("optional") or item.get("os") or item.get("cpu") or item.get("libc")))
                except (OSError, RuntimeError, ValueError, KeyError):
                    gaps[environment.environment_id] = "Target platform lock verification failed"
                    continue
                missing = sorted(set(probed) - set(baseline))
                conflicting = sorted(path for path in set(probed) & set(baseline) if (probed[path].get("version"), probed[path].get("integrity")) != (baseline[path].get("version"), baseline[path].get("integrity")))
                if conflicting:
                    gaps[environment.environment_id] = f"Platform resolution disagrees with lock at {len(conflicting)} package paths"
                elif locked_project and (missing or fresh_optional):
                    gaps[environment.environment_id] = f"Lock omits {len(set(missing) | set(fresh_optional))} target platform package paths"
                elif missing:
                    for path in missing:
                        baseline[path] = probed[path]
        return gaps

    def _fetch(self, url: str, integrity: str, npm_root: str) -> tuple[str, Path]:
        cached = self.store.lookup(f"npm:{url}:{integrity}")
        if cached:
            return cached
        algorithm, _, encoded = integrity.partition("-")
        if algorithm not in ("sha512", "sha256") or not encoded:
            raise ValueError("Unsupported npm integrity")
        expected = base64.b64decode(encoded, validate=True)
        with tempfile.NamedTemporaryFile(prefix="packferry-npm-download-", delete=False) as stage:
            path = Path(stage.name)
        try:
            _run(["node", str(_helper_path()), "fetch", npm_root, url, integrity, str(path), str(self.store.root / "npm-cache"), self.registry])
            hasher = hashlib.new(algorithm)
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.digest() != expected:
                raise ValueError("npm integrity mismatch")
            digest, stored = self.store.put_file(path)
            self.store.record(f"npm:{url}:{integrity}", digest)
            return digest, stored
        finally:
            path.unlink(missing_ok=True)


def _helper_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "apps" / "exporter" / "npm_helper.cjs"
    return Path(__file__).with_name("npm_helper.cjs")


def _parse_package_spec(value: str) -> tuple[str, str]:
    if value.startswith("@"):
        match = re.fullmatch(r"(@[^/]+/[^@]+)(?:@(.+))?", value)
    else:
        match = re.fullmatch(r"([^@]+)(?:@(.+))?", value)
    if not match:
        raise ValueError("Invalid npm package specification")
    return match.group(1), match.group(2) or "*"


def _special_spec(value: str) -> bool:
    return value.startswith(("file:", "link:", "workspace:", "git:", "git+", "http:", "https:", "ssh:"))


def _registry_url(url: str, registry: str) -> bool:
    candidate, expected = urllib.parse.urlsplit(url), urllib.parse.urlsplit(registry)
    return candidate.scheme == expected.scheme == "https" and candidate.hostname == expected.hostname and candidate.port == expected.port and not candidate.username and not candidate.password and not candidate.query and not candidate.fragment


def _applies(node: dict, environment: Environment) -> bool:
    platform = environment.platform.lower()
    os_name = "win32" if "win" in platform else "linux"
    cpu = "x64"
    for field, value in (("os", os_name), ("cpu", cpu), ("libc", "glibc")):
        rules = node.get(field, [])
        if isinstance(rules, list) and rules:
            if f"!{value}" in rules or value not in rules and any(not item.startswith("!") for item in rules):
                return False
    return True
