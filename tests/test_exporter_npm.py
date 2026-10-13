import json
from pathlib import Path

import pytest

from apps.exporter.npm_adapter import NpmAdapter
from apps.exporter.storage import ArtifactStore
from packages.common.models import Environment


def test_project_git_source_rejected_before_npm_runs(tmp_path: Path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "dependencies": {"bad": "git+https://example.com/bad.git"}}), encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("npm was invoked before special source rejection")
    monkeypatch.setattr("apps.exporter.npm_adapter._run", forbidden)
    adapter = NpmAdapter(ArtifactStore(tmp_path / "store"))
    with pytest.raises(ValueError, match="non-Registry"):
        adapter.resolve([], tmp_path, [Environment(environment_id="node-win", runtime="node", version="26", platform="win32-x64", abi="none")])


def test_supplied_lock_platform_omission_is_reported(tmp_path: Path, monkeypatch):
    (tmp_path / "package.json").write_text('{"name":"fixture","version":"1.0.0"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{}', encoding="utf-8")
    baseline = {"": {"name": "fixture", "version": "1.0.0"}, "node_modules/root": {"version": "1.0.0"}}
    lock = {"lockfileVersion": 3, "packages": baseline}

    def fake_run(command, cwd=None, timeout=600):
        expanded = dict(baseline)
        if "--os=linux" in command:
            expanded["node_modules/linux-only"] = {"version": "1.0.0", "os": ["linux"]}
        (cwd / "package-lock.json").write_text(json.dumps({"packages": expanded}), encoding="utf-8")
        return ""

    monkeypatch.setattr("apps.exporter.npm_adapter._run", fake_run)
    adapter = NpmAdapter(ArtifactStore(tmp_path / "store"))
    environments = [
        Environment(environment_id="win", runtime="node", version="26", platform="win32-x64", abi="none"),
        Environment(environment_id="linux", runtime="node", version="26", platform="linux-x64", abi="none", libc="glibc"),
    ]
    gaps = adapter._platform_audit(tmp_path, lock, environments, locked_project=True)
    assert "win" not in gaps
    assert "linux" in gaps
    assert "node_modules/linux-only" not in baseline


def test_fresh_platform_lock_merges_compatible_optional_instances(tmp_path: Path, monkeypatch):
    (tmp_path / "package.json").write_text('{"name":"fixture","version":"1.0.0"}', encoding="utf-8")
    baseline = {"": {"name": "fixture", "version": "1.0.0"}}
    lock = {"lockfileVersion": 3, "packages": baseline}

    def fake_run(command, cwd=None, timeout=600):
        expanded = dict(baseline)
        if "--os=linux" in command:
            expanded["node_modules/linux-only"] = {"version": "1.0.0", "os": ["linux"], "integrity": "sha512-test"}
        (cwd / "package-lock.json").write_text(json.dumps({"packages": expanded}), encoding="utf-8")
        return ""

    monkeypatch.setattr("apps.exporter.npm_adapter._run", fake_run)
    adapter = NpmAdapter(ArtifactStore(tmp_path / "store"))
    environment = Environment(environment_id="linux", runtime="node", version="26", platform="linux-x64", abi="none", libc="glibc")
    assert adapter._platform_audit(tmp_path, lock, [environment], locked_project=False) == {}
    assert "node_modules/linux-only" in baseline


def test_independent_target_resolve_catches_lock_hidden_optional(tmp_path: Path, monkeypatch):
    (tmp_path / "package.json").write_text('{"name":"fixture","version":"1.0.0"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{}', encoding="utf-8")
    baseline = {"": {"name": "fixture", "version": "1.0.0"}}
    lock = {"lockfileVersion": 3, "packages": baseline}

    def fake_run(command, cwd=None, timeout=600):
        expanded = dict(baseline)
        if not (cwd / "package-lock.json").exists():
            expanded["node_modules/linux-extra"] = {"version": "1.0.0", "optional": True, "os": ["linux"]}
        (cwd / "package-lock.json").write_text(json.dumps({"packages": expanded}), encoding="utf-8")
        return ""

    monkeypatch.setattr("apps.exporter.npm_adapter._run", fake_run)
    adapter = NpmAdapter(ArtifactStore(tmp_path / "store"))
    environment = Environment(environment_id="linux", runtime="node", version="26", platform="linux-x64", abi="none", libc="glibc")
    gaps = adapter._platform_audit(tmp_path, lock, [environment], locked_project=True)
    assert "linux" in gaps
