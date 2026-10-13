"""PEP 751 pylock input: select locked originals without re-resolving versions."""

from __future__ import annotations

import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from packages.common.models import Artifact, Environment, Package, Resolution, Warning
from .adapters import AdapterResult, _marker_context, check_cancelled
from .python_bridge import _compatible_tags, _read_static_metadata
from .storage import ArtifactStore


class PylockAdapter:
    def __init__(self, store: ArtifactStore):
        self.store = store

    def resolve(self, path: Path, environments: list[Environment]) -> AdapterResult:
        path = Path(path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if data.get("lock-version") != "1.0" or not data.get("created-by") or not isinstance(data.get("packages"), list):
            raise ValueError("Only PEP 751 pylock version 1.0 is supported")
        result = AdapterResult()
        packages: dict[str, Package] = {}
        artifacts: dict[str, Artifact] = {}
        for environment in environments:
            check_cancelled()
            resolution = Resolution(environment_id=environment.environment_id, status="COMPLETE")
            result.resolutions.append(resolution)
            context = _marker_context(environment)
            context["extras"] = set()
            context["dependency_groups"] = set(data.get("default-groups", []))
            allowed_environments = data.get("environments", [])
            if allowed_environments and not any(Marker(marker).evaluate(context) for marker in allowed_environments):
                resolution.status = "FAILED"
                result.warnings.append(Warning(code="PYLOCK_UNSUPPORTED_ENVIRONMENT", severity="error", affected_environment_ids=[environment.environment_id], message="Target is outside pylock environments"))
                continue
            requires_python = data.get("requires-python")
            if requires_python and Version(environment.version) not in SpecifierSet(requires_python):
                resolution.status = "FAILED"
                result.warnings.append(Warning(code="PYLOCK_UNSUPPORTED_ENVIRONMENT", severity="error", affected_environment_ids=[environment.environment_id], message="Target Python is outside pylock requires-python"))
                continue
            selected: dict[str, tuple[str, list]] = {}
            for item in data["packages"]:
                name = canonicalize_name(item.get("name", ""))
                version = item.get("version")
                if not name or not version:
                    raise ValueError("pylock package has missing identity")
                marker = item.get("marker")
                if marker:
                    try:
                        if not Marker(marker).evaluate(context):
                            continue
                    except (ValueError, KeyError) as exc:
                        raise ValueError("Unsupported pylock package marker") from exc
                package_requires_python = item.get("requires-python")
                if package_requires_python and Version(environment.version) not in SpecifierSet(package_requires_python):
                    raise ValueError(f"pylock package {name} is incompatible with the target Python")
                if name in selected:
                    raise ValueError(f"Ambiguous pylock entries for {name}")
                selected[name] = (version, item)
            pending_edges: dict[str, list[str]] = {}
            for name, (version, item) in selected.items():
                check_cancelled()
                node_id = f"pypi:{name}=={version}"
                try:
                    file_item, kind = self._choose_file(item, environment)
                    filename = file_item.get("name") or Path(urllib.parse.urlsplit(file_item.get("url", file_item.get("path", ""))).path).name
                    sha256 = file_item.get("hashes", {}).get("sha256")
                    if not sha256 or len(sha256) != 64:
                        raise ValueError("Locked file needs SHA-256")
                    source = self._obtain(path.parent, file_item, sha256)
                    metadata, requirements = _read_static_metadata(source, kind, name, version)
                    edges = []
                    if "dependencies" in item:
                        for dependency in item["dependencies"]:
                            child = canonicalize_name(dependency.get("name", ""))
                            if child not in selected or dependency.get("version", selected[child][0]) != selected[child][0]:
                                raise ValueError(f"Locked dependency missing or incompatible: {child}")
                            edges.append(f"pypi:{child}=={selected[child][0]}")
                    else:
                        for dependency in requirements:
                            if dependency.marker and not dependency.marker.evaluate(_marker_context(environment)):
                                continue
                            child = canonicalize_name(dependency.name)
                            if child not in selected or Version(selected[child][0]) not in dependency.specifier:
                                raise ValueError(f"Locked dependency missing or incompatible: {child}")
                            edges.append(f"pypi:{child}=={selected[child][0]}")
                    pending_edges[node_id] = sorted(set(edges))
                    if node_id not in packages:
                        packages[node_id] = Package(node_id=node_id, name=name, version=version, source="https://pypi.org/simple/" + name + "/", dependencies_known=True)
                    digest = sha256
                    if digest not in artifacts:
                        artifact_source = file_item.get("url") or "https://pypi.org/simple/" + name + "/"
                        artifacts[digest] = Artifact(artifact_id=digest, filename=filename, type=kind, archive_path=f"artifacts/sha256/{digest[:2]}/{digest}", size=source.stat().st_size, sha256=digest, source=artifact_source, package_node_ids=[node_id])
                        result.sources[digest] = source
                    elif node_id not in artifacts[digest].package_node_ids:
                        artifacts[digest].package_node_ids.append(node_id)
                    resolution.nodes.append(node_id)
                    resolution.selected_artifacts.append(digest)
                    if kind == "sdist":
                        result.warnings.append(Warning(code="PY_SDIST_BUILD_UNVERIFIED", affected_environment_ids=[environment.environment_id], affected_node_ids=[node_id], message=f"{name}=={version}: locked source collected; build and offline install not verified"))
                except (ValueError, OSError) as exc:
                    resolution.status = "PARTIAL"
                    result.warnings.append(Warning(code="PYLOCK_FILE_UNAVAILABLE", severity="warning", affected_environment_ids=[environment.environment_id], affected_node_ids=[node_id], message=f"{name}=={version}: {exc}"))
                    if node_id not in packages:
                        packages[node_id] = Package(node_id=node_id, name=name, version=version, source="https://pypi.org/simple/" + name + "/", dependencies_known=False)
                    resolution.nodes.append(node_id)
            for node_id, edges in pending_edges.items():
                packages[node_id].dependencies = sorted(set(packages[node_id].dependencies + edges))
                resolution.dependencies[node_id] = edges
        result.packages = list(packages.values())
        result.artifacts = list(artifacts.values())
        return result

    def _choose_file(self, item: dict, environment: Environment) -> tuple[dict, str]:
        ranking = {tag: index for index, tag in enumerate(_compatible_tags(environment))}
        wheels = []
        for wheel in item.get("wheels", []):
            filename = wheel.get("name") or Path(urllib.parse.urlsplit(wheel.get("url", wheel.get("path", ""))).path).name
            try:
                _, _, _, file_tags = parse_wheel_filename(filename)
            except ValueError:
                continue
            scores = [ranking[tag] for tag in file_tags if tag in ranking]
            if scores:
                wheels.append((min(scores), wheel))
        if wheels:
            return min(wheels, key=lambda pair: pair[0])[1], "wheel"
        if isinstance(item.get("sdist"), dict):
            return item["sdist"], "sdist"
        raise ValueError("No compatible locked Wheel or sdist")

    def _obtain(self, base: Path, item: dict, digest: str) -> Path:
        cached = self.store.verify(digest)
        if cached:
            return cached
        if item.get("url"):
            url = urllib.parse.urlsplit(item["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Unsafe pylock distribution URL")
            check_cancelled()
            with urllib.request.urlopen(item["url"], timeout=5) as response:
                _, source = self.store.put_stream(response, digest)
            return source
        if item.get("path"):
            source = (base / item["path"]).resolve()
            if not source.is_relative_to(base.resolve()) or not source.is_file():
                raise ValueError("Unsafe pylock local distribution path")
            _, stored = self.store.put_file(source, digest)
            return stored
        raise ValueError("pylock distribution has no URL or path")
