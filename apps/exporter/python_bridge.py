"""Bounded static-metadata projection into uv's resolver.

The temporary Wheels are metadata carriers only. Every selected node maps back
to a verified original Registry distribution, which is the only file exported.
"""

from __future__ import annotations

import email
import hashlib
import json
import re
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from packaging import tags
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from packages.common.models import Environment
from packages.common.archives import bounded_tar_info
from packages.common.bundle import _preflight_zip
from .adapters import _marker_context, _pip_platforms, _run, check_cancelled
from .storage import ArtifactStore


class StaticMetadataUnavailable(ValueError):
    """Provider cannot prove a complete, safe static candidate catalog."""


@dataclass
class OriginalCandidate:
    name: str
    version: str
    filename: str
    distribution_type: str
    sha256: str
    path: Path
    requirements: list[Requirement]
    metadata_sha256: str
    source_url: str
    build_requirements: list[str] | None = None


def _compatible_tags(environment: Environment) -> list[tags.Tag]:
    major, minor, *_ = environment.version.split(".")
    version = int(major), int(minor)
    platforms = _pip_platforms(environment)
    result = list(tags.cpython_tags(python_version=version, abis=[environment.abi], platforms=platforms))
    result.extend(tags.compatible_tags(python_version=version, interpreter=f"cp{major}{minor}", platforms=platforms))
    return result


def _read_static_metadata(path: Path, kind: str, expected_name: str, expected_version: str) -> tuple[bytes, list[Requirement]]:
    if kind == "wheel":
        with path.open('rb') as source:
            _preflight_zip(source, 4096)
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 2 * 1024 * 1024:
                raise StaticMetadataUnavailable("Wheel metadata missing or ambiguous")
            data = archive.read(names[0])
    elif zipfile.is_zipfile(path):
        with path.open('rb') as source:
            _preflight_zip(source, 4096)
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith('/PKG-INFO') and name.count('/') == 1]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 2 * 1024 * 1024:
                raise StaticMetadataUnavailable('sdist ZIP metadata missing or ambiguous')
            data = archive.read(names[0])
    else:
        try:
            with tarfile.open(path, "r:gz", tarinfo=bounded_tar_info(2 * 1024 * 1024, 4096)) as archive:
                entries = []
                for count, item in enumerate(archive):
                    check_cancelled()
                    if count >= 4096:
                        raise StaticMetadataUnavailable("sdist has too many archive entries")
                    if item.name.endswith("/PKG-INFO") and item.name.count("/") == 1:
                        entries.append(item)
                if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 2 * 1024 * 1024:
                    raise StaticMetadataUnavailable("sdist PKG-INFO missing or ambiguous")
                stream = archive.extractfile(entries[0])
                if stream is None:
                    raise StaticMetadataUnavailable("sdist PKG-INFO missing")
                data = stream.read(2 * 1024 * 1024 + 1)
        except tarfile.TarError as exc:
            raise StaticMetadataUnavailable("Invalid sdist tar archive") from exc
    message = email.message_from_bytes(data)
    for field in ("Name", "Version", "Metadata-Version", "Requires-Python"):
        if len(message.get_all(field, [])) > 1:
            raise StaticMetadataUnavailable(f"Duplicate {field} in Core Metadata")
    if canonicalize_name(message.get("Name", "")) != expected_name or message.get("Version") != expected_version:
        raise StaticMetadataUnavailable("Static metadata identity mismatch")
    if kind == "sdist":
        try:
            metadata_version = tuple(int(part) for part in message.get("Metadata-Version", "0").split("."))
        except ValueError:
            metadata_version = (0,)
        if metadata_version < (2, 2):
            raise StaticMetadataUnavailable("Legacy sdist metadata cannot prove static dependencies")
        if any(value.lower() in ("requires-dist", "requires-python", "provides-extra") for value in message.get_all("Dynamic", [])):
            raise StaticMetadataUnavailable("Dynamic sdist dependency metadata")
    requirements = []
    for value in message.get_all("Requires-Dist", []):
        try:
            requirement = Requirement(value)
        except ValueError as exc:
            raise StaticMetadataUnavailable("Invalid Requires-Dist") from exc
        if requirement.url:
            raise StaticMetadataUnavailable("Direct URL dependency in metadata")
        requirements.append(requirement)
    return data, requirements


def _carrier(directory: Path, candidate: OriginalCandidate, metadata: bytes) -> None:
    stem = candidate.name.replace("-", "_")
    wheel_name = f"{stem}-{candidate.version}-py3-none-any.whl"
    dist_info = f"{stem}-{candidate.version}.dist-info"
    with zipfile.ZipFile(directory / wheel_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nGenerator: packferry-metadata-carrier\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{dist_info}/RECORD", "")


def _read_build_requirements(path: Path) -> list[str] | None:
    if zipfile.is_zipfile(path):
        with path.open('rb') as source:
            _preflight_zip(source, 4096)
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith('/pyproject.toml') and name.count('/') == 1]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 1024 * 1024:
                return None
            payload = archive.read(names[0])
        return _parse_build_requirements(payload)
    with tarfile.open(path, "r:gz", tarinfo=bounded_tar_info(2 * 1024 * 1024, 4096)) as archive:
        entries = []
        for count, item in enumerate(archive):
            check_cancelled()
            if count >= 4096:
                return None
            if item.name.endswith("/pyproject.toml") and item.name.count("/") == 1:
                entries.append(item)
        if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 1024 * 1024:
            return None
        stream = archive.extractfile(entries[0])
        if stream is None:
            return None
        return _parse_build_requirements(stream.read(1024 * 1024 + 1))


def _parse_build_requirements(payload: bytes) -> list[str] | None:
    try:
        data = tomllib.loads(payload.decode('utf-8'))
        build_system = data.get('build-system')
        if not isinstance(build_system, dict) or 'requires' not in build_system:
            return None
        values = build_system['requires']
        if not isinstance(values, list):
            return None
        parsed = [Requirement(value) for value in values]
        if any(item.url for item in parsed):
            return None
        return [str(item) for item in parsed]
    except (UnicodeError, ValueError, TypeError):
        return None


class StaticUvBridge:
    def __init__(self, store: ArtifactStore, uv: str = "uv", index_url: str = "https://pypi.org/pypi/", max_names: int = 100, max_versions: int = 6, max_bytes: int = 100 * 1024 * 1024):
        self.store = store
        self.uv = uv
        self.index_url = index_url.rstrip("/") + "/"
        self.max_names = max_names
        self.max_versions = max_versions
        self.max_bytes = max_bytes
        self._unknown: dict[str, list[Version]] = {}

    def resolve(self, roots: list[str], environment: Environment) -> tuple[dict[str, OriginalCandidate], dict[str, list[str]]]:
        requested = [Requirement(value) for value in roots]
        if any(item.url for item in requested):
            raise StaticMetadataUnavailable("Direct URL roots are unsupported")
        candidates: dict[tuple[str, str], OriginalCandidate] = {}
        constraints: dict[str, set[str]] = {}
        for item in requested:
            constraints.setdefault(canonicalize_name(item.name), set()).add(str(item.specifier))
        pending = set(constraints)
        visited: set[str] = set()
        processed_signatures: dict[str, tuple[str, ...]] = {}
        with tempfile.TemporaryDirectory(prefix="packferry-bridge-") as directory:
            carriers = Path(directory) / "carriers"
            carriers.mkdir()
            while pending:
                check_cancelled()
                name = pending.pop()
                signature = tuple(sorted(constraints.get(name, set())))
                if processed_signatures.get(name) == signature:
                    continue
                visited.add(name)
                processed_signatures[name] = signature
                if len(visited) > self.max_names:
                    raise StaticMetadataUnavailable("Static provider package limit reached")
                for candidate, data in self._candidates(name, environment, signature):
                    key = name, candidate.version
                    candidates[key] = candidate
                    _carrier(carriers, candidate, data)
                    for requirement in candidate.requirements:
                        # Extra-specific branches are included; uv evaluates the marker.
                        child = canonicalize_name(requirement.name)
                        values = constraints.setdefault(child, set())
                        previous = len(values)
                        values.add(str(requirement.specifier))
                        if child not in processed_signatures or len(values) != previous:
                            pending.add(child)
            with tempfile.TemporaryDirectory(prefix="packferry-bridge-input-") as input_dir:
                requirements_path = Path(input_dir) / "requirements.in"
                requirements_path.write_text("\n".join(roots) + "\n", encoding="utf-8")
                output = _run([self.uv, "pip", "compile", str(requirements_path), "--no-index", "--find-links", str(carriers), "--no-build", "--no-header", "--no-annotate", "--python-version", environment.version, "--python-platform", "x86_64-pc-windows-msvc" if "win" in environment.platform.lower() else "x86_64-unknown-linux-gnu", "--format", "requirements.txt"])
        selected: dict[str, OriginalCandidate] = {}
        for line in output.splitlines():
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line.strip())
            if match:
                key = canonicalize_name(match[1]), match[2]
                if key not in candidates:
                    raise StaticMetadataUnavailable("uv selected a package outside the provider catalog")
                selected[key[0]] = candidates[key]
        if not selected:
            raise StaticMetadataUnavailable("uv returned no selected nodes")
        for name, candidate in selected.items():
            check_cancelled()
            if any(version >= Version(candidate.version) for version in self._unknown.get(name, [])):
                raise StaticMetadataUnavailable(f"{name}: newer candidate has unknown static metadata")
        # Use selected Core Metadata to produce direct edges. Propagate requested extras.
        graph: dict[str, list[str]] = {name: [] for name in selected}
        queue = [(canonicalize_name(item.name), set(item.extras)) for item in requested]
        seen: set[tuple[str, tuple[str, ...]]] = set()
        while queue:
            name, extras = queue.pop()
            marker_key = name, tuple(sorted(extras))
            if marker_key in seen:
                continue
            seen.add(marker_key)
            candidate = selected.get(name)
            if candidate is None:
                raise StaticMetadataUnavailable("Selected graph misses a requested dependency")
            for requirement in candidate.requirements:
                contexts = extras or {""}
                if requirement.marker and not any(requirement.marker.evaluate(_marker_context(environment, extra)) for extra in contexts):
                    continue
                child = canonicalize_name(requirement.name)
                if child not in selected or Version(selected[child].version) not in requirement.specifier:
                    raise StaticMetadataUnavailable("Selected graph does not satisfy static metadata")
                graph[name].append(child)
                queue.append((child, set(requirement.extras)))
        return selected, {name: sorted(set(children)) for name, children in graph.items()}

    def _candidates(self, name: str, environment: Environment, constraints: tuple[str, ...]) -> list[tuple[OriginalCandidate, bytes]]:
        url = urllib.parse.urljoin(self.index_url, urllib.parse.quote(name) + "/json")
        check_cancelled()
        with urllib.request.urlopen(url, timeout=5) as response:
            catalog = json.load(_LimitReader(response, 10 * 1024 * 1024))
        versions = []
        for value, files in catalog.get("releases", {}).items():
            try:
                parsed = Version(value)
            except ValueError:
                continue
            specifiers = [SpecifierSet(value) for value in constraints if value]
            if parsed.is_prerelease and not any(spec.prereleases for spec in specifiers):
                continue
            if specifiers and not any(parsed in spec for spec in specifiers):
                continue
            versions.append((parsed, value, files))
        versions.sort(reverse=True)
        versions = versions[: self.max_versions]
        target_tags = _compatible_tags(environment)
        tag_rank = {tag: index for index, tag in enumerate(target_tags)}
        result = []
        for _, version, files in versions:
            check_cancelled()
            ranked = []
            for file in files:
                if file.get("yanked"):
                    continue
                requires_python = file.get("requires_python")
                if requires_python and Version(environment.version) not in SpecifierSet(requires_python):
                    continue
                filename = file.get("filename", "")
                kind = file.get("packagetype")
                if kind == "bdist_wheel":
                    try:
                        wheel_name, wheel_version, _, wheel_tags = parse_wheel_filename(filename)
                    except ValueError:
                        continue
                    if wheel_name != name or str(wheel_version) != version:
                        continue
                    compatible = [tag_rank[tag] for tag in wheel_tags if tag in tag_rank]
                    if compatible:
                        ranked.append((min(compatible), "wheel", file))
                elif kind == "sdist" and filename.endswith((".tar.gz", '.zip')):
                    ranked.append((len(tag_rank) + 1, "sdist", file))
            if not ranked:
                continue
            _, kind, file = min(ranked, key=lambda item: item[0])
            digest = file.get("digests", {}).get("sha256")
            source_url = file.get("url", "")
            parsed_url = urllib.parse.urlsplit(source_url)
            if not digest or parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise StaticMetadataUnavailable(f"{name}=={version}: unverifiable source")
            source = self.store.verify(digest)
            if source is None:
                with urllib.request.urlopen(source_url, timeout=5) as response:
                    limited = _LimitReader(response, self.max_bytes)
                    _, source = self.store.put_stream(limited, digest)
            try:
                data, requirements = _read_static_metadata(source, kind, name, version)
            except (ValueError, OSError, zipfile.BadZipFile):
                self._unknown.setdefault(name, []).append(Version(version))
                continue
            build_requirements = _read_build_requirements(source) if kind == "sdist" else None
            result.append((OriginalCandidate(name, version, file["filename"], kind, digest, source, requirements, hashlib.sha256(data).hexdigest(), source_url, build_requirements), data))
        if not result:
            raise StaticMetadataUnavailable(f"{name}: no safe compatible Registry candidate")
        return result


class _LimitReader:
    def __init__(self, source, limit: int):
        self.source, self.remaining = source, limit

    def read(self, size: int = -1) -> bytes:
        check_cancelled()
        if self.remaining <= 0:
            if self.source.read(1):
                raise StaticMetadataUnavailable("Original distribution exceeds provider size limit")
            return b""
        data = self.source.read(min(size if size > 0 else self.remaining, self.remaining))
        self.remaining -= len(data)
        return data
