import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from apps.exporter.python_bridge import StaticMetadataUnavailable, StaticUvBridge
from apps.exporter.services import ExportRequest, ExportService
from apps.exporter.storage import ArtifactStore
from packages.common.models import Environment


def _wheel(name: str, version: str, dependencies: list[str]) -> bytes:
    buffer = io.BytesIO()
    stem = name.replace("-", "_")
    metadata = f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{stem}-{version}.dist-info/METADATA", metadata)
        archive.writestr(f"{stem}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
    return buffer.getvalue()


def _sdist(name: str, version: str, dependencies: list[str], dynamic: bool = False, build_requires: list[str] | None = None) -> bytes:
    buffer = io.BytesIO()
    metadata = f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    if dynamic:
        metadata += "Dynamic: Requires-Dist\n"
    payload = metadata.encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"{name}-{version}/PKG-INFO")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        if build_requires is not None:
            project = ("[build-system]\nrequires = [" + ", ".join(json.dumps(value) for value in build_requires) + "]\n").encode()
            info = tarfile.TarInfo(f"{name}-{version}/pyproject.toml")
            info.size = len(project)
            archive.addfile(info, io.BytesIO(project))
    return buffer.getvalue()


def _provider(monkeypatch, files: dict[str, tuple[str, str, bytes]]):
    import hashlib
    catalogs = {}
    blobs = {}
    for filename, (name, version, content) in files.items():
        url = f"https://registry.example/{filename}"
        blobs[url] = content
        kind = "sdist" if filename.endswith(".tar.gz") else "bdist_wheel"
        catalogs.setdefault(name, {}).setdefault(version, []).append({"filename": filename, "packagetype": kind, "url": url, "digests": {"sha256": hashlib.sha256(content).hexdigest()}, "yanked": False})

    def fake_urlopen(url, timeout=0):
        if url.endswith("/json"):
            name = url.rsplit("/", 2)[-2]
            return io.BytesIO(json.dumps({"releases": catalogs[name]}).encode())
        return io.BytesIO(blobs[url])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


@pytest.mark.skipif(__import__("shutil").which("uv") is None, reason="uv is required")
def test_uv_bridge_selects_newer_original_sdist_and_static_chain(tmp_path: Path, monkeypatch):
    files = {
        "root-1.0.0-py3-none-any.whl": ("root", "1.0.0", _wheel("root", "1.0.0", [])),
        "root-2.0.0.tar.gz": ("root", "2.0.0", _sdist("root", "2.0.0", ["helper==1.0.0"])),
        "helper-1.0.0-py3-none-any.whl": ("helper", "1.0.0", _wheel("helper", "1.0.0", ["leaf==1.0.0"])),
        "leaf-1.0.0-py3-none-any.whl": ("leaf", "1.0.0", _wheel("leaf", "1.0.0", [])),
    }
    _provider(monkeypatch, files)
    store = ArtifactStore(tmp_path / "artifacts")
    bridge = StaticUvBridge(store, index_url="https://registry.example/")
    environment = Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")
    selected, graph = bridge.resolve(["root"], environment)
    assert selected["root"].version == "2.0.0"
    assert selected["root"].distribution_type == "sdist"
    assert graph == {"root": ["helper"], "helper": ["leaf"], "leaf": []}
    assert all("bridge" not in candidate.filename for candidate in selected.values())
    assert selected["root"].path.read_bytes() == files["root-2.0.0.tar.gz"][2]


@pytest.mark.skipif(__import__("shutil").which("uv") is None, reason="uv is required")
def test_uv_bridge_rejects_dynamic_newer_sdist_without_downgrade(tmp_path: Path, monkeypatch):
    files = {
        "root-1.0.0-py3-none-any.whl": ("root", "1.0.0", _wheel("root", "1.0.0", [])),
        "root-2.0.0.tar.gz": ("root", "2.0.0", _sdist("root", "2.0.0", [], dynamic=True)),
    }
    _provider(monkeypatch, files)
    bridge = StaticUvBridge(ArtifactStore(tmp_path / "artifacts"), index_url="https://registry.example/")
    environment = Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")
    with pytest.raises(StaticMetadataUnavailable, match="unknown static metadata"):
        bridge.resolve(["root"], environment)


@pytest.mark.skipif(__import__("shutil").which("uv") is None, reason="uv is required")
def test_uv_bridge_accepts_exact_old_prerelease_beyond_candidate_window(tmp_path: Path, monkeypatch):
    files = {f"root-{major}.0.0-py3-none-any.whl": ("root", f"{major}.0.0", _wheel("root", f"{major}.0.0", [])) for major in range(2, 20)}
    files["root-1.0rc1-py3-none-any.whl"] = ("root", "1.0rc1", _wheel("root", "1.0rc1", []))
    _provider(monkeypatch, files)
    bridge = StaticUvBridge(ArtifactStore(tmp_path / "store"), index_url="https://registry.example/", max_versions=3)
    environment = Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")
    selected, _ = bridge.resolve(["root==1.0rc1"], environment)
    assert selected["root"].version == "1.0rc1"


def test_artifact_store_detects_corruption_and_rewrites(tmp_path: Path):
    store = ArtifactStore(tmp_path / "store")
    digest, path = store.put_stream(io.BytesIO(b"original"))
    store.record("origin", digest)
    assert store.lookup("origin") == (digest, path)
    path.write_bytes(b"corrupt")
    assert store.verify(digest) is None
    assert store.lookup("origin") is None
    second_digest, second_path = store.put_stream(io.BytesIO(b"original"))
    assert second_digest == digest
    assert second_path.read_bytes() == b"original"


@pytest.mark.skipif(__import__("shutil").which("uv") is None, reason="uv is required")
def test_export_service_bundle_contains_original_sdist_not_carrier(tmp_path: Path, monkeypatch):
    files = {
        "root-2.0.0.tar.gz": ("root", "2.0.0", _sdist("root", "2.0.0", [], build_requires=[])),
    }
    _provider(monkeypatch, files)
    output = tmp_path / "bundle.zip"
    request = ExportRequest(ecosystem="pypi", constraints=["root"], environments=[Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")])
    manifest = ExportService(tmp_path / "data").export(request, output)
    assert manifest.verification.resolution_status == "COMPLETE"
    assert len(manifest.artifacts) == 1
    assert manifest.artifacts[0].type == "sdist"
    with zipfile.ZipFile(output) as archive:
        assert archive.read(manifest.artifacts[0].archive_path) == files["root-2.0.0.tar.gz"][2]
        assert all("bridge" not in name for name in archive.namelist())


@pytest.mark.skipif(__import__("shutil").which("uv") is None, reason="uv is required")
def test_static_sdist_build_requirements_are_collected_without_build(tmp_path: Path, monkeypatch):
    files = {
        "root-2.0.0.tar.gz": ("root", "2.0.0", _sdist("root", "2.0.0", [], build_requires=["builder==1.0.0"])),
        "builder-1.0.0-py3-none-any.whl": ("builder", "1.0.0", _wheel("builder", "1.0.0", [])),
    }
    _provider(monkeypatch, files)
    output = tmp_path / "bundle.zip"
    request = ExportRequest(ecosystem="pypi", constraints=["root"], environments=[Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")])
    manifest = ExportService(tmp_path / "data").export(request, output)
    assert manifest.verification.resolution_status == "COMPLETE"
    assert {package.name for package in manifest.packages} == {"root", "builder"}
    assert len(manifest.artifacts) == 2
    assert any(warning.code == "PY_BUILD_REQUIREMENTS_COLLECTED" for warning in manifest.warnings)


@pytest.mark.skipif(__import__("shutil").which("uv") is None, reason="uv is required")
def test_unknown_sdist_build_requirements_mark_partial(tmp_path: Path, monkeypatch):
    files = {"root-2.0.0.tar.gz": ("root", "2.0.0", _sdist("root", "2.0.0", []))}
    _provider(monkeypatch, files)
    request = ExportRequest(ecosystem="pypi", constraints=["root"], environments=[Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")])
    manifest = ExportService(tmp_path / "data").export(request, tmp_path / "partial.zip")
    assert manifest.verification.resolution_status == "PARTIAL"
    assert any(warning.code == "PY_BUILD_REQUIREMENTS_UNKNOWN" for warning in manifest.warnings)


def test_pylock_project_uses_locked_original_wheel_and_hash(tmp_path: Path):
    import hashlib

    wheel = _wheel("locked", "1.2.3", [])
    wheel_path = tmp_path / "locked-1.2.3-py3-none-any.whl"
    wheel_path.write_bytes(wheel)
    digest = hashlib.sha256(wheel).hexdigest()
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\ncreated-by = "fixture"\nrequires-python = ">=3.9"\n'
        '[[packages]]\nname = "locked"\nversion = "1.2.3"\n'
        '[[packages.wheels]]\nname = "locked-1.2.3-py3-none-any.whl"\n'
        'path = "locked-1.2.3-py3-none-any.whl"\n'
        f'[packages.wheels.hashes]\nsha256 = "{digest}"\n',
        encoding="utf-8",
    )
    request = ExportRequest(ecosystem="pypi", project_path=tmp_path, environments=[Environment(environment_id="linux", version="3.12", platform="linux_x86_64", abi="cp312")])
    output = tmp_path / "locked.zip"
    manifest = ExportService(tmp_path / "data").export(request, output)
    assert manifest.verification.resolution_status == "COMPLETE"
    assert manifest.input.original_lock_fingerprint
    with zipfile.ZipFile(output) as archive:
        assert archive.read(manifest.artifacts[0].archive_path) == wheel
    reused = ExportService(tmp_path / "data").export(request.model_copy(update={"resolution_policy": "reuse"}), tmp_path / "reused.zip")
    assert reused.input.resolution_policy == "reuse"
    assert reused.bundle_id != manifest.bundle_id
    assert reused.artifacts[0].sha256 == manifest.artifacts[0].sha256
