"""Exporter orchestration shared by CLI, local HTTP and desktop entry points."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.common.models import Environment, Input, Manifest, Verification
from .adapters import PythonAdapter, load_python_input
from .npm_adapter import NpmAdapter
from .storage import ArtifactStore


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ecosystem: Literal["pypi", "npm"]
    constraints: list[str] = Field(default_factory=list)
    project_path: Path | None = None
    environments: list[Environment] = Field(default_factory=list)
    production_only: bool = False
    resolution_policy: Literal["fresh", "reuse"] = "fresh"
    lock_policy: Literal["prefer", "ignore"] = "prefer"

    @property
    def packages(self) -> list[str]:
        return self.constraints

    @property
    def fresh(self) -> bool:
        return self.resolution_policy == "fresh"

    @property
    def prefer_lock(self) -> bool:
        return self.lock_policy == "prefer"

    @model_validator(mode="after")
    def input_present(self):
        if bool(self.constraints) == bool(self.project_path):
            raise ValueError("Provide packages or project_path")
        return self


class ExportService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ArtifactStore(self.data_dir / "artifacts")
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        from .adapters import cancel_active_subprocesses
        self._cancel_event.set()
        cancel_active_subprocesses()

    def export(self, request: ExportRequest, output: Path) -> Manifest:
        from .adapters import bind_cancel_event, unbind_cancel_event
        if self._cancel_event.is_set():
            raise RuntimeError('Export was cancelled before it started')
        token = bind_cancel_event(self._cancel_event)
        try:
            return self._export_impl(request, output)
        finally:
            unbind_cancel_event(token)

    def _export_impl(self, request: ExportRequest, output: Path) -> Manifest:
        from .adapters import check_cancelled
        from packages.common.bundle import build_bundle

        environments = request.environments or self._defaults(request.ecosystem)
        versions = self._tool_versions(request.ecosystem)
        cache_key = self._resolution_key(request, environments, versions)
        snapshot = self.data_dir / "resolutions" / f"{cache_key}.json"
        if request.resolution_policy == "reuse":
            if not snapshot.is_file():
                raise ValueError("No matching historical resolution; run fresh first")
            manifest = Manifest.model_validate_json(snapshot.read_bytes())
            sources = {}
            for artifact in manifest.artifacts:
                source = self.store.verify(artifact.sha256)
                if source is None or source.stat().st_size != artifact.size:
                    raise ValueError(f"Historical artifact missing or corrupt: {artifact.filename}")
                sources[artifact.sha256] = source
            manifest = manifest.model_copy(update={
                "bundle_id": str(uuid4()),
                "created_at": datetime.now(timezone.utc),
                "input": manifest.input.model_copy(update={"resolution_policy": "reuse"}),
            })
            check_cancelled()
            build_bundle(manifest, sources, Path(output))
            return manifest
        if request.ecosystem == "pypi":
            pylock = self._pylock_path(request.project_path, request.prefer_lock)
            if pylock:
                from .pylock_adapter import PylockAdapter
                resolved = PylockAdapter(self.store).resolve(pylock, environments)
                constraints = sorted(f"{package.name}=={package.version}" for package in resolved.packages)
            else:
                constraints, input_warnings = load_python_input(request.constraints, request.project_path, request.prefer_lock)
                resolved = PythonAdapter(self.store).resolve(constraints, environments)
                resolved.warnings.extend(input_warnings)
        else:
            constraints = request.constraints if request.constraints else self._npm_project_constraints(request.project_path)
            resolved = NpmAdapter(self.store).resolve(request.constraints, request.project_path, environments, request.production_only, request.prefer_lock)
        check_cancelled()
        statuses = [resolution.status for resolution in resolved.resolutions]
        resolution_status = "FAILED" if statuses and all(status == "FAILED" for status in statuses) else "PARTIAL" if any(status != "COMPLETE" for status in statuses) else "COMPLETE"
        covered_nodes = {node_id for artifact in resolved.artifacts for node_id in artifact.package_node_ids}
        collection_status = "PARTIAL" if any(package.node_id not in covered_nodes for package in resolved.packages) else "COMPLETE"
        if resolution_status == "FAILED":
            collection_status = "FAILED"
        fingerprint = None
        if request.project_path:
            path = Path(request.project_path)
            if path.is_dir():
                lock_name = "pylock.toml" if request.ecosystem == "pypi" else "package-lock.json"
                path = path / lock_name
            if path.name in ("pylock.toml", "package-lock.json") and path.is_file():
                fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = Manifest(
            ecosystem=request.ecosystem,
            tool_versions=versions,
            input=Input(mode="project" if request.project_path else "packages", normalized_constraints=constraints, resolution_policy="fresh", lock_policy="prefer" if request.prefer_lock else "ignore", original_lock_fingerprint=fingerprint),
            environments=environments,
            packages=resolved.packages,
            artifacts=resolved.artifacts,
            resolutions=resolved.resolutions,
            warnings=resolved.warnings,
            verification=Verification(resolution_status=resolution_status, collection_status=collection_status, offline_install_status="NOT_VERIFIED"),
        )
        check_cancelled()
        build_bundle(manifest, resolved.sources, Path(output))
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="resolution-", suffix=".json", dir=snapshot.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(manifest.model_dump_json().encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, snapshot)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return manifest

    @staticmethod
    def _tool_versions(ecosystem: str) -> dict[str, str]:
        from .adapters import _run
        from .runtime import python_executable

        if ecosystem == "pypi":
            return {"uv": _run(["uv", "--version"]).strip(), "pip": _run([python_executable(), "-c", "import pip; print(pip.__version__)"]).strip()}
        return {"node": _run(["node", "--version"]).strip(), "npm": _run(["npm", "--version"]).strip()}

    def _resolution_key(self, request: ExportRequest, environments: list[Environment], versions: dict[str, str]) -> str:

        project_files = []
        if request.project_path:
            path = Path(request.project_path)
            if path.is_dir():
                names = ("pylock.toml", "requirements.txt", "pyproject.toml") if request.ecosystem == "pypi" else ("package.json", "package-lock.json")
                files = [path / name for name in names]
            else:
                files = [path]
                if request.ecosystem == 'pypi' and request.prefer_lock and path.name != 'pylock.toml':
                    files.append(path.with_name('pylock.toml'))
                if path.name == "package-lock.json":
                    files.append(path.with_name("package.json"))
            project_files = sorted((file.name, hashlib.sha256(file.read_bytes()).hexdigest()) for file in files if file.is_file())
        key = {
            "ecosystem": request.ecosystem,
            "constraints": request.constraints,
            "project_files": project_files,
            "environments": [environment.model_dump(mode="json") for environment in environments],
            "production_only": request.production_only,
            "lock_policy": request.lock_policy,
            "tool_versions": versions,
        }
        return hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _defaults(ecosystem: str) -> list[Environment]:
        if ecosystem == "pypi":
            return [Environment(environment_id="cp312-win", runtime="cpython", version="3.12", platform="win_amd64", abi="cp312")]
        return [
            Environment(environment_id="node-win", runtime="node", version="26", platform="win32-x64", abi="none"),
            Environment(environment_id="node-linux", runtime="node", version="26", platform="linux-x64", abi="none", libc="glibc"),
        ]

    @staticmethod
    def _npm_project_constraints(project_path: Path | None) -> list[str]:
        import json
        if project_path is None:
            return []
        path = Path(project_path)
        if path.is_dir() or path.name == "package-lock.json":
            path = path / "package.json" if path.is_dir() else path.with_name("package.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        values = []
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            values.extend(f"{name}@{'<special-source>' if str(constraint).startswith(('file:', 'link:', 'workspace:', 'git:', 'git+', 'http:', 'https:', 'ssh:')) else constraint}" for name, constraint in data.get(field, {}).items())
        return sorted(set(values))

    @staticmethod
    def _pylock_path(project_path: Path | None, prefer_lock: bool) -> Path | None:
        if not project_path or not prefer_lock:
            return None
        path = Path(project_path)
        candidate = path / 'pylock.toml' if path.is_dir() else path if path.name == 'pylock.toml' else path.with_name('pylock.toml')
        return candidate if candidate.name == "pylock.toml" and candidate.is_file() else None
