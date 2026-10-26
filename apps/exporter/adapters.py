"""Safe wrappers around uv, pip and npm. No third-party build scripts run here."""

from __future__ import annotations

import json
import contextvars
import re
import shutil
import subprocess
import os
import signal
import tempfile
import threading
import time
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass, field
from email.parser import BytesParser
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from packages.common.models import Artifact, Environment, Package, Resolution, Warning
from .runtime import python_executable
from .storage import ArtifactStore


_process_lock = threading.Lock()
_active_processes: set[subprocess.Popen] = set()
_cancel_context: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("export_cancel", default=None)


class ExportCancelled(RuntimeError):
    pass


def bind_cancel_event(event: threading.Event):
    return _cancel_context.set(event)


def unbind_cancel_event(token) -> None:
    _cancel_context.reset(token)


def check_cancelled() -> None:
    event = _cancel_context.get()
    if event is not None and event.is_set():
        raise ExportCancelled("Export cancelled")


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def cancel_active_subprocesses() -> None:
    """Stop active resolver/download processes during desktop shutdown."""
    with _process_lock:
        processes = list(_active_processes)
    for process in processes:
        _terminate_process(process)


@dataclass
class AdapterResult:
    packages: list[Package] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    sources: dict[str, Path] = field(default_factory=dict)


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 600) -> str:
    check_cancelled()
    executable = shutil.which(command[0]) or command[0]
    command = [executable, *command[1:]]
    try:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags, start_new_session=os.name != "nt")
        with _process_lock:
            _active_processes.add(process)
        try:
            deadline = time.monotonic() + timeout
            while True:
                check_cancelled()
                try:
                    stdout, _stderr = process.communicate(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
        finally:
            with _process_lock:
                _active_processes.discard(process)
    except ExportCancelled:
        if "process" in locals() and process.poll() is None:
            _terminate_process(process)
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if "process" in locals() and process.poll() is None:
            _terminate_process(process)
        raise RuntimeError(f"Tool execution failed: {command[0]}: {type(exc).__name__}") from exc
    if process.returncode:
        # Tool output can contain private URLs; do not copy it to reports.
        raise RuntimeError(f"{Path(command[0]).name} failed with exit code {process.returncode}")
    return stdout


def load_python_input(packages: list[str], project_path: Path | None, prefer_lock: bool = True) -> tuple[list[str], list[Warning]]:
    if project_path is None:
        parsed = [Requirement(value) for value in packages]
        if any(item.url for item in parsed):
            raise ValueError('Direct URL roots are unsupported; provide a Registry package constraint')
        return [str(item) for item in parsed], []
    path = Path(project_path)
    if path.is_dir():
        for name in (("pylock.toml", "requirements.txt", "pyproject.toml") if prefer_lock else ("requirements.txt", "pyproject.toml")):
            candidate = path / name
            if candidate.is_file():
                path = candidate
                break
    if path.name == "requirements.txt":
        values = []
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            if clean.startswith("-") or " @ " in clean or "://" in clean or clean.startswith((".", "/", "\\")):
                raise ValueError("Requirements directives, direct URLs and local paths are unsupported")
            values.append(str(Requirement(clean)))
        return values, []
    if path.name == "pyproject.toml":
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        if "dependencies" in project.get("dynamic", []):
            raise ValueError("Dynamic project dependencies cannot be resolved without executing build code")
        parsed = [Requirement(value) for value in project.get('dependencies', [])]
        if any(item.url for item in parsed):
            raise ValueError('Direct URL project dependencies are unsupported')
        values = [str(item) for item in parsed]
        return values, []
    if path.name == "pylock.toml":
        if not prefer_lock:
            for name in ['requirements.txt', 'pyproject.toml']:
                original = path.with_name(name)
                if original.is_file():
                    return load_python_input([], original, prefer_lock=False)
        raise ValueError('Re-resolution of a lock requires original requirements.txt or pyproject.toml')
    raise ValueError("Unsupported Python project input")


def _platform_for_uv(environment: Environment) -> str:
    if environment.runtime != 'cpython':
        raise ValueError('This release supports CPython targets only; other implementations are not verified')
    if environment.libc not in {None, 'glibc', 'glibc2.31', 'glibc-2.31'}:
        raise ValueError('This release supports glibc Linux targets only')
    value = environment.platform.lower()
    if value in ("win_amd64", "windows", "x86_64-pc-windows-msvc"):
        return "x86_64-pc-windows-msvc"
    if value in ("linux_x86_64", "manylinux_2_31_x86_64", "x86_64-unknown-linux-gnu", "linux"):
        return "x86_64-unknown-linux-gnu"
    raise ValueError(f"Unsupported Python platform: {environment.platform}")


def _pip_platforms(environment: Environment) -> list[str]:
    if _platform_for_uv(environment).endswith("windows-msvc"):
        return ["win_amd64"]
    # pip needs the full compatible baseline set for older manylinux wheels.
    return [f"manylinux_2_{minor}_x86_64" for minor in range(31, 16, -1)] + ["manylinux2014_x86_64", "linux_x86_64"]


def _marker_context(environment: Environment, extra: str = "") -> dict[str, str]:
    context = default_environment()
    major, minor, *_ = environment.version.split(".")
    windows = _platform_for_uv(environment).endswith("windows-msvc")
    context.update({
        "python_version": f"{major}.{minor}",
        "python_full_version": environment.version if environment.version.count(".") == 2 else f"{major}.{minor}.0",
        "implementation_version": environment.version if environment.version.count('.') == 2 else f'{major}.{minor}.0',
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "sys_platform": "win32" if windows else "linux",
        "os_name": "nt" if windows else "posix",
        "platform_system": "Windows" if windows else "Linux",
        "platform_machine": "AMD64" if windows else "x86_64",
        "extra": extra,
    })
    return context


def _wheel_metadata(path: Path) -> tuple[str, str, list[str]]:
    with zipfile.ZipFile(path) as archive:
        entries = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(entries) != 1 or archive.getinfo(entries[0]).file_size > 2 * 1024 * 1024:
            raise ValueError("Wheel has missing, ambiguous or oversized metadata")
        data = archive.read(entries[0])
    metadata = BytesParser().parsebytes(data)
    name, version = metadata.get("Name"), metadata.get("Version")
    if not name or not version:
        raise ValueError("Wheel identity missing")
    return canonicalize_name(name), version, metadata.get_all("Requires-Dist", [])


def _pypi_latest_sdist(requirement: Requirement, environment: Environment) -> dict | None:
    """Check direct roots for the uv --no-build downgrade hazard."""
    name = canonicalize_name(requirement.name)
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        check_cancelled()
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None
    candidates = []
    for version_text, files in payload.get("releases", {}).items():
        try:
            version = Version(version_text)
        except ValueError:
            continue
        if version not in requirement.specifier or version.is_prerelease and not requirement.specifier.prereleases:
            continue
        for item in files:
            if item.get("packagetype") != "sdist" or item.get("yanked"):
                continue
            requires_python = item.get("requires_python")
            if requires_python:
                from packaging.specifiers import SpecifierSet
                if Version(environment.version) not in SpecifierSet(requires_python):
                    continue
            candidates.append((version, item))
    if not candidates:
        return None
    version, item = max(candidates, key=lambda pair: pair[0])
    return {**item, "_candidate_version": str(version)}


class PythonAdapter:
    def __init__(self, store: ArtifactStore, uv: str = "uv", python: str | None = None):
        self.store, self.uv, self.python = store, uv, python or python_executable()

    def resolve(self, requirements: list[str], environments: list[Environment]) -> AdapterResult:
        result = AdapterResult()
        packages_by_id: dict[str, Package] = {}
        artifacts_by_digest: dict[str, Artifact] = {}
        for environment in environments:
            from .observation import observe
            observe("resolving", f"解析与收集目标环境：{environment.environment_id}")
            check_cancelled()
            environment_id = environment.environment_id
            resolution = Resolution(environment_id=environment_id, status="COMPLETE")
            result.resolutions.append(resolution)
            try:
                from .python_bridge import StaticUvBridge
                selected, graph = StaticUvBridge(self.store, self.uv).resolve(requirements, environment)
                build_pending = []
                for name, candidate in selected.items():
                    node_id = f"pypi:{name}=={candidate.version}"
                    dependency_ids = [f'pypi:{child}=={selected[child].version}' for child in graph[name]]
                    if node_id not in packages_by_id:
                        packages_by_id[node_id] = Package(node_id=node_id, name=name, version=candidate.version, source=f"https://pypi.org/simple/{name}/", dependencies=dependency_ids, dependencies_known=True)
                    else:
                        packages_by_id[node_id].dependencies = sorted(set(packages_by_id[node_id].dependencies + dependency_ids))
                    resolution.dependencies[node_id] = dependency_ids
                    resolution.nodes.append(node_id)
                    digest = candidate.sha256
                    if digest not in artifacts_by_digest:
                        artifacts_by_digest[digest] = Artifact(artifact_id=digest, filename=candidate.filename, type="wheel" if candidate.distribution_type == "wheel" else "sdist", archive_path=f"artifacts/sha256/{digest[:2]}/{digest}", size=candidate.path.stat().st_size, sha256=digest, source=candidate.source_url, package_node_ids=[node_id])
                        result.sources[digest] = candidate.path
                    elif node_id not in artifacts_by_digest[digest].package_node_ids:
                        artifacts_by_digest[digest].package_node_ids.append(node_id)
                    resolution.selected_artifacts.append(digest)
                    if candidate.distribution_type == "sdist":
                        build_pending.append((node_id, candidate.build_requirements, 0))
                        result.warnings.append(Warning(code="PY_SDIST_BUILD_UNVERIFIED", severity="warning", affected_environment_ids=[environment_id], affected_node_ids=[node_id], message=f"{name}=={candidate.version}: original source collected; build execution and offline install not verified"))
                self._collect_build_requirements(result, packages_by_id, artifacts_by_digest, resolution, environment, build_pending)
                continue
            except (OSError, RuntimeError, ValueError) as bridge_exc:
                resolution.status = "PARTIAL"
                result.warnings.append(Warning(code="PY_STATIC_BRIDGE_UNAVAILABLE", severity="warning", affected_environment_ids=[environment_id], message=str(bridge_exc)))
            try:
                pinned = self._compile(requirements, environment)
            except (RuntimeError, ValueError) as exc:
                resolution.status = "FAILED"
                result.warnings.append(Warning(code="PY_RESOLUTION_FAILED", severity="error", affected_environment_ids=[environment_id], message=str(exc)))
                continue
            # uv's no-build filtering is not a safe proof that a newer sdist-only root does not exist.
            for value in requirements:
                requirement = Requirement(value)
                selected = pinned.get(canonicalize_name(requirement.name))
                latest = _pypi_latest_sdist(requirement, environment)
                if selected and latest:
                    try:
                        newer = Version(latest["_candidate_version"]) > Version(selected)
                    except ValueError:
                        newer = False
                    if newer:
                        resolution.status = "PARTIAL"
                        result.warnings.append(Warning(code="PY_NEWER_SDIST_UNRESOLVED", severity="warning", affected_environment_ids=[environment_id], message=f"{requirement.name}: newer source distribution requires static metadata bridge; binary-only result is incomplete"))
            for name, version in pinned.items():
                check_cancelled()
                node_id = f"pypi:{name}=={version}"
                if node_id not in packages_by_id:
                    packages_by_id[node_id] = Package(node_id=node_id, name=name, version=version, source=f"https://pypi.org/simple/{name}/", dependencies_known=False)
                resolution.nodes.append(node_id)
                try:
                    wheel = self._download_wheel(name, version, environment)
                    metadata_name, metadata_version, dependencies = _wheel_metadata(wheel)
                    if metadata_name != name or metadata_version != version:
                        raise ValueError("Downloaded Wheel identity mismatch")
                    child_ids = []
                    for dependency in dependencies:
                        parsed = Requirement(dependency)
                        if parsed.marker and not parsed.marker.evaluate(_marker_context(environment)):
                            continue
                        child_name = canonicalize_name(parsed.name)
                        if child_name in pinned:
                            child_ids.append(f"pypi:{child_name}=={pinned[child_name]}")
                    package = packages_by_id[node_id]
                    package.dependencies = sorted(set(package.dependencies + child_ids))
                    package.dependencies_known = True
                    digest, source = self.store.put_file(wheel)
                    if digest not in artifacts_by_digest:
                        artifact = Artifact(artifact_id=digest, filename=wheel.name, type="wheel", archive_path=f"artifacts/sha256/{digest[:2]}/{digest}", size=source.stat().st_size, sha256=digest, source=f"https://pypi.org/simple/{name}/", package_node_ids=[node_id])
                        artifacts_by_digest[digest] = artifact
                        result.sources[digest] = source
                    else:
                        if node_id not in artifacts_by_digest[digest].package_node_ids:
                            artifacts_by_digest[digest].package_node_ids.append(node_id)
                    resolution.selected_artifacts.append(digest)
                except (RuntimeError, ValueError, OSError) as exc:
                    resolution.status = "PARTIAL"
                    result.warnings.append(Warning(code="PY_ARTIFACT_MISSING", severity="warning", affected_environment_ids=[environment_id], affected_node_ids=[node_id], message=f"{name}=={version}: {exc}"))
        result.packages = list(packages_by_id.values())
        result.artifacts = list(artifacts_by_digest.values())
        return result

    def _collect_build_requirements(self, result: AdapterResult, packages_by_id: dict[str, Package], artifacts_by_digest: dict[str, Artifact], resolution: Resolution, environment: Environment, pending: list[tuple[str, list[str] | None, int]]) -> None:
        from .python_bridge import StaticUvBridge

        seen: set[tuple[str, ...]] = set()
        while pending:
            check_cancelled()
            source_node, requirements, depth = pending.pop()
            if requirements is None:
                resolution.status = "PARTIAL"
                result.warnings.append(Warning(code="PY_BUILD_REQUIREMENTS_UNKNOWN", severity="warning", affected_environment_ids=[environment.environment_id], affected_node_ids=[source_node], message="Source build requirements cannot be statically identified"))
                continue
            if not requirements:
                continue
            signature = tuple(sorted(requirements))
            if signature in seen:
                continue
            seen.add(signature)
            if depth >= 3 or len(seen) > 20:
                resolution.status = "PARTIAL"
                result.warnings.append(Warning(code="PY_BUILD_REQUIREMENTS_LIMIT", severity="warning", affected_environment_ids=[environment.environment_id], affected_node_ids=[source_node], message="Source build dependency recursion limit reached"))
                continue
            try:
                selected, graph = StaticUvBridge(self.store, self.uv).resolve(requirements, environment)
            except (OSError, RuntimeError, ValueError) as exc:
                resolution.status = "PARTIAL"
                result.warnings.append(Warning(code="PY_BUILD_REQUIREMENTS_UNRESOLVED", severity="warning", affected_environment_ids=[environment.environment_id], affected_node_ids=[source_node], message=f"Static source build dependencies could not be collected: {exc}"))
                continue
            build_nodes = []
            for name, candidate in selected.items():
                node_id = f"pypi:{name}=={candidate.version}"
                build_nodes.append(node_id)
                dependencies = [f"pypi:{child}=={selected[child].version}" for child in graph[name]]
                if node_id not in packages_by_id:
                    packages_by_id[node_id] = Package(node_id=node_id, name=name, version=candidate.version, source=f"https://pypi.org/simple/{name}/", dependencies=dependencies, dependencies_known=True)
                else:
                    packages_by_id[node_id].dependencies = sorted(set(packages_by_id[node_id].dependencies + dependencies))
                if node_id not in resolution.nodes:
                    resolution.nodes.append(node_id)
                digest = candidate.sha256
                if digest not in artifacts_by_digest:
                    artifacts_by_digest[digest] = Artifact(artifact_id=digest, filename=candidate.filename, type="wheel" if candidate.distribution_type == "wheel" else "sdist", archive_path=f"artifacts/sha256/{digest[:2]}/{digest}", size=candidate.path.stat().st_size, sha256=digest, source=candidate.source_url, package_node_ids=[node_id])
                    result.sources[digest] = candidate.path
                elif node_id not in artifacts_by_digest[digest].package_node_ids:
                    artifacts_by_digest[digest].package_node_ids.append(node_id)
                if digest not in resolution.selected_artifacts:
                    resolution.selected_artifacts.append(digest)
                if candidate.distribution_type == "sdist":
                    pending.append((node_id, candidate.build_requirements, depth + 1))
                    result.warnings.append(Warning(code="PY_SDIST_BUILD_UNVERIFIED", severity="warning", affected_environment_ids=[environment.environment_id], affected_node_ids=[node_id], message=f"{name}=={candidate.version}: source build execution and offline install not verified"))
            result.warnings.append(Warning(code="PY_BUILD_REQUIREMENTS_COLLECTED", severity="info", affected_environment_ids=[environment.environment_id], affected_node_ids=[source_node, *build_nodes], message=f"Collected {len(build_nodes)} packages from statically known source build requirements"))
            resolution.build_dependencies[source_node] = build_nodes
            for name in selected:
                build_id = f'pypi:{name}=={selected[name].version}'
                resolution.build_dependencies[build_id] = [f'pypi:{child}=={selected[child].version}' for child in graph[name]]

    def _compile(self, requirements: list[str], environment: Environment) -> dict[str, str]:
        with tempfile.TemporaryDirectory(prefix="packferry-uv-") as temporary:
            path = Path(temporary) / "requirements.in"
            path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
            output = _run([self.uv, "pip", "compile", str(path), "--no-build", "--no-header", "--no-annotate", "--python-version", environment.version, "--python-platform", _platform_for_uv(environment), "--format", "requirements.txt"])
        pinned = {}
        for line in output.splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$", line.strip())
            if match:
                pinned[canonicalize_name(match[1])] = match[2]
        if not pinned:
            raise RuntimeError("uv returned no pinned packages")
        return pinned

    def _download_wheel(self, name: str, version: str, environment: Environment) -> Path:
        from .observation import observe
        observe("resolving", f"收集 wheel：{name} {version} · {environment.environment_id}")
        directory = Path(tempfile.mkdtemp(prefix="packferry-wheel-"))
        command = [self.python, "-m", "pip", "download", f"{name}=={version}", "--no-deps", "--only-binary=:all:", "--implementation", "cp", "--python-version", environment.version, "--abi", environment.abi, "--dest", str(directory)]
        for platform in _pip_platforms(environment):
            command.extend(["--platform", platform])
        try:
            _run(command)
            wheels = list(directory.glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("pip did not select one compatible Wheel")
            return wheels[0]
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
