from __future__ import annotations

import hashlib
import io
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from apps.importer import Settings, create_app
from apps.importer.app import Runtime
from apps.importer.maintenance import backup
from apps.importer.nexus import NexusClient, RemoteResult
from apps.importer.db import Task
from apps.importer.storage_lock import storage_lock
from packages.common.bundle import build_bundle
from packages.common.hashes import blob_path
from packages.common.models import Artifact, Environment, Input, Manifest, Package, Resolution, Verification


def bundle(tmp_path: Path) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        content = b'{"name":"sample-pkg","version":"1.0.0"}'
        item = tarfile.TarInfo("package/package.json")
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    data = raw.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    source = tmp_path / "sample-pkg-1.0.0.tgz"
    source.write_bytes(data)
    manifest = Manifest(
        ecosystem="npm",
        input=Input(),
        environments=[Environment(environment_id="linux", runtime="node", version="22", platform="linux-x64", abi="none")],
        packages=[Package(node_id="sample", name="sample-pkg", version="1.0.0", source="https://registry.npmjs.org/sample-pkg")],
        artifacts=[Artifact(artifact_id="a1", filename=source.name, type="npm", archive_path=blob_path(sha), size=len(data), sha256=sha, source="https://registry.npmjs.org/sample-pkg/-/sample-pkg-1.0.0.tgz", package_node_ids=["sample"])],
        resolutions=[Resolution(environment_id="linux", nodes=["sample"], selected_artifacts=["a1"], status="COMPLETE")],
        verification=Verification(resolution_status="COMPLETE", collection_status="COMPLETE"),
    )
    return build_bundle(manifest, {sha: source}, tmp_path / "bundle.zip")


class FakeNexus:
    remote = {}
    publishes = 0

    def __init__(self, _base, username, password, **_kwargs):
        self.username = username
        self.password = password

    def authenticate(self):
        return self.password == "good"

    def close(self):
        pass

    def check(self, repository, ecosystem, artifact, package):
        key = (repository, package["name"], package["version"])
        found = self.remote.get(key)
        if found is None:
            return RemoteResult("ABSENT")
        return RemoteResult("SAME" if found == artifact["sha256"] else "CONFLICT")

    def publish(self, repository, ecosystem, artifact, package, file_path):
        type(self).publishes += 1
        self.remote[(repository, package["name"], package["version"])] = hashlib.sha256(file_path.read_bytes()).hexdigest()


def setup(tmp_path):
    FakeNexus.remote = {}
    FakeNexus.publishes = 0
    return create_app(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True), FakeNexus)


def login(client, username):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": "good"})
    assert response.status_code == 200
    assert response.json()["token"].startswith("pf_")
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_import_upload_confirm_publish_and_repeat_skip(tmp_path):
    path = bundle(tmp_path)
    with TestClient(setup(tmp_path)) as client:
        headers = login(client, "alice")
        with path.open("rb") as stream:
            response = client.post("/api/v1/imports", headers={**headers, "Idempotency-Key": "first"}, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")})
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        assert response.json()["status"] == "READY"
        assert client.get(f"/api/v1/imports/{task_id}/preview", headers=headers).json()["artifact_count"] == 1
        assert client.get("/api/v1/operations/first", headers=headers).json()["task"]["id"] == task_id
        confirm = client.post(f"/api/v1/imports/{task_id}/confirm", headers={**headers, "Idempotency-Key": "confirm-first"})
        assert confirm.status_code == 200
        for _ in range(100):
            status = client.get(f"/api/v1/imports/{task_id}", headers=headers).json()["status"]
            if status == "COMPLETED":
                break
            time.sleep(0.02)
        assert status == "COMPLETED"
        assert client.get(f"/api/v1/imports/{task_id}/report", headers=headers).json()["files"][0]["status"] == "UPLOADED"
        assert FakeNexus.publishes == 1
        with path.open("rb") as stream:
            repeat = client.post("/api/v1/imports", headers={**headers, "Idempotency-Key": "first"}, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")})
        assert repeat.json()["id"] == task_id
        assert client.post(f"/api/v1/imports/{task_id}/confirm", headers={**headers, "Idempotency-Key": "confirm-first"}).status_code == 200


def test_owner_and_bad_auth(tmp_path):
    path = bundle(tmp_path)
    with TestClient(setup(tmp_path)) as client:
        assert client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401
        alice = login(client, "alice")
        bob = login(client, "bob")
        with path.open("rb") as stream:
            task = client.post("/api/v1/imports", headers=alice, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()
        assert client.get(f"/api/v1/imports/{task['id']}", headers=bob).status_code == 404
        assert client.get("/api/v1/imports", headers=bob).json()["total"] == 0
        assert client.post(f"/api/v1/imports/{task['id']}/confirm", headers=bob).status_code == 404
        assert client.get("/api/v1/audit/events", headers=alice).status_code == 403


def test_restart_requires_original_owner_authorization(tmp_path):
    path = bundle(tmp_path)
    settings = Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False)
    with TestClient(create_app(settings, FakeNexus)) as client:
        alice = login(client, "alice")
        with path.open("rb") as stream:
            task_id = client.post("/api/v1/imports", headers=alice, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()["id"]
        client.post(f"/api/v1/imports/{task_id}/confirm", headers=alice)
    with TestClient(create_app(settings, FakeNexus)) as client:
        alice = login(client, "alice")
        bob = login(client, "bob")
        assert client.get(f"/api/v1/imports/{task_id}", headers=alice).json()["status"] == "NEEDS_AUTH"
        assert client.post(f"/api/v1/imports/{task_id}/authorize", headers=bob, json={"password": "good"}).status_code == 404
        assert client.post(f"/api/v1/imports/{task_id}/authorize", headers=alice, json={"password": "good"}).status_code == 200


def test_cookie_csrf_and_idempotency_conflict(tmp_path):
    path = bundle(tmp_path)
    with TestClient(setup(tmp_path)) as client:
        login_response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "good"})
        csrf = login_response.json()["csrf_token"]
        with path.open("rb") as stream:
            denied = client.post("/api/v1/imports", data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")})
        assert denied.status_code == 403
        with path.open("rb") as stream:
            allowed = client.post("/api/v1/imports", headers={"Origin": "http://testserver", "X-CSRF-Token": csrf, "Idempotency-Key": "same-key"}, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")})
        assert allowed.status_code == 201
        token = login_response.json()["token"]
        conflicting = client.post(f"/api/v1/imports/{allowed.json()['id']}/confirm", headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "same-key"})
        assert conflicting.status_code == 409


def test_npm_publisher_preserves_original_tarball(tmp_path):
    path = bundle(tmp_path)
    from packages.common.bundle import validate_bundle

    manifest = validate_bundle(path)
    artifact = manifest.artifacts[0].model_dump()
    package = manifest.packages[0].model_dump()
    raw = tmp_path / artifact["filename"]
    captured = {}

    def serve(request):
        captured["body"] = request.read()
        return httpx.Response(201, json={"ok": True})

    client = NexusClient("https://nexus.test", "alice", "good")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(serve), auth=("alice", "good"))
    client.publish("npm-hosted", "npm", artifact, package, raw)
    import base64
    import json
    payload = json.loads(captured["body"])
    assert base64.b64decode(payload["_attachments"][artifact["filename"]]["data"]) == raw.read_bytes()
    assert payload["versions"]["1.0.0"]["dist"]["integrity"].startswith("sha512-")
    client.close()


def test_npm_check_malformed_metadata_is_unknown(tmp_path):
    path = bundle(tmp_path)
    from packages.common.bundle import validate_bundle

    manifest = validate_bundle(path)
    artifact = manifest.artifacts[0].model_dump()
    package = manifest.packages[0].model_dump()

    def serve(_request):
        return httpx.Response(200, json={"versions": "broken"})

    client = NexusClient("https://nexus.test", "alice", "good")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(serve))
    assert client.check("npm-hosted", "npm", artifact, package).state == "UNKNOWN"
    assert not client._trusted_artifact_url("https://nexus.test/repository/npm-hosted/../other/file.tgz", "npm-hosted")
    assert not client._trusted_artifact_url("https://nexus.test/repository/npm-hosted/%252e%252e/other", "npm-hosted")
    client.close()


def test_upload_size_limit_applies_before_parsing(tmp_path):
    path = bundle(tmp_path)
    app = create_app(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True, max_bundle_bytes=8), FakeNexus)
    with TestClient(app) as client:
        headers = login(client, "alice")
        with path.open("rb") as stream:
            response = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")})
        assert response.status_code == 413
        response = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": ("huge.zip", b"x" * (2 * 1024 * 1024), "application/zip")})
        assert response.status_code == 413


def test_backup_contains_consistent_database_and_retained_bundle(tmp_path):
    path = bundle(tmp_path)
    app = setup(tmp_path)
    with TestClient(app) as client:
        headers = login(client, "alice")
        with path.open("rb") as stream:
            task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()["id"]
        import zipfile
        snapshot = backup(tmp_path / "data", tmp_path / "backup.zip")
        with zipfile.ZipFile(snapshot) as archive:
            assert "importer.sqlite3" in archive.namelist()
            assert f"bundles/{task_id}.zip" in archive.namelist()
        import pytest
        with pytest.raises(ValueError, match="Bundle directory"):
            backup(tmp_path / "data", tmp_path / "data" / "bundles" / "recursive.zip")


def test_unknown_is_reconciled_without_blind_republish(tmp_path):
    path = bundle(tmp_path)
    FakeNexus.remote = {}
    FakeNexus.publishes = 0
    app = create_app(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False), FakeNexus)
    with TestClient(app) as client:
        headers = login(client, "alice")
        with path.open("rb") as stream:
            task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()["id"]
        runtime = app.state.runtime
        with runtime.sessions.begin() as db:
            task = db.get(Task, task_id)
            task.status = "QUEUED"
            task.files[0].status = "UNKNOWN"
        runtime._process(task_id)
        assert FakeNexus.publishes == 0
        assert client.get(f"/api/v1/imports/{task_id}/report", headers=headers).json()["files"][0]["status"] == "UNKNOWN"


def test_finished_bundle_retention_preserves_report(tmp_path):
    path = bundle(tmp_path)
    app = create_app(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False), FakeNexus)
    with TestClient(app) as client:
        headers = login(client, "alice")
        with path.open("rb") as stream:
            task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()["id"]
        runtime = app.state.runtime
        with runtime.sessions.begin() as db:
            task = db.get(Task, task_id)
            original_path = Path(task.zip_path)
            task.status = "COMPLETED"
            task.completed_at = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        runtime.cleanup_finished_bundles()
        assert not original_path.exists()
        assert client.get(f"/api/v1/imports/{task_id}/report", headers=headers).json()["id"] == task_id


def test_invalid_login_is_redacted_and_plaintext_api_refused(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data", nexus_url="https://nexus.test", repositories={"npm-hosted": "npm"}, run_worker=False), FakeNexus)
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/login", json={"username": "alice", "password": {"secret": "never-repeat-this"}})
        assert response.status_code == 403
        assert "never-repeat-this" not in response.text
    with TestClient(setup(tmp_path / "insecure")) as client:
        response = client.post("/api/v1/auth/login", json={"username": "alice", "password": {"secret": "never-repeat-this"}})
        assert response.status_code == 422
        assert "never-repeat-this" not in response.text


def test_simultaneous_confirm_same_key_is_idempotent(tmp_path):
    path = bundle(tmp_path)
    app = create_app(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False), FakeNexus)
    with TestClient(app) as client:
        headers = login(client, "alice")
        for index in range(10):
            with path.open("rb") as stream:
                task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()["id"]

            def submit(_):
                return client.post(f"/api/v1/imports/{task_id}/confirm", headers={**headers, "Idempotency-Key": f"same-confirm-{index}"})

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(submit, range(2)))
            assert [response.status_code for response in responses] == [200, 200]
            assert {response.json()["id"] for response in responses} == {task_id}


def test_logout_revokes_all_user_tokens_and_background_credential(tmp_path):
    app = setup(tmp_path)
    with TestClient(app) as client:
        first = login(client, "alice")
        second = login(client, "alice")
        assert app.state.runtime.credential("alice") == "good"
        assert client.post("/api/v1/auth/logout", headers=first).status_code == 200
        assert client.get("/api/v1/auth/me", headers=second).status_code == 401
        assert app.state.runtime.credential("alice") is None


def test_repository_and_nexus_url_configuration_rejects_unsafe_values(tmp_path):
    import pytest

    defaults = dict(data_dir=tmp_path / "data", allow_insecure_http=True)
    with pytest.raises(ValueError, match="whitelist"):
        create_app(Settings(**defaults, nexus_url="http://nexus.test", repositories={"../other": "npm"}), FakeNexus)
    for url in ("http://alice:password@nexus.test", "http://nexus.test/?token=x", "http://nexus.test/#fragment", "http://nexus.test/repository/../other"):
        with pytest.raises(ValueError, match="Nexus URL"):
            create_app(Settings(**defaults, nexus_url=url, repositories={"npm-hosted": "npm"}), FakeNexus)


def test_backup_config_excludes_secrets_and_storage_lock_serializes(tmp_path):
    path = bundle(tmp_path)
    app = create_app(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False), FakeNexus)
    with TestClient(app) as client:
        headers = login(client, "alice")
        with path.open("rb") as stream:
            task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (path.name, stream, "application/zip")}).json()["id"]
        config = tmp_path / "config.yaml"
        config.write_text('nexus_url: http://user:url-secret@nexus.test\nrepositories:\n  npm-hosted: npm\npassword: do-not-back-up\n', encoding="utf-8")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with storage_lock(tmp_path / "data"):
                future = pool.submit(backup, tmp_path / "data", tmp_path / "backup.zip", config)
                time.sleep(0.05)
                assert not future.done()
            future.result(timeout=5)
        import zipfile
        with zipfile.ZipFile(tmp_path / "backup.zip") as archive:
            assert f"bundles/{task_id}.zip" in archive.namelist()
            assert "do-not-back-up" not in archive.read("config.yaml").decode()
            assert "url-secret" not in archive.read("config.yaml").decode()


def test_shutdown_preserves_singleton_lock_while_worker_is_alive(tmp_path):
    class SlowWorker:
        def join(self, timeout):
            assert timeout == 10

        def is_alive(self):
            return True

    runtime = Runtime(Settings(data_dir=tmp_path / "data", nexus_url="http://nexus.test", repositories={"npm-hosted": "npm"}, allow_insecure_http=True), FakeNexus)
    handle = (tmp_path / "data" / "worker.lock").open("w+b")
    runtime._worker_lock_file = handle
    runtime.worker = SlowWorker()
    runtime.shutdown()
    assert not handle.closed
    runtime._release_worker_lock()
