"""Local HTTP integration: actual NexusClient and CLI HTTP traffic, no external Nexus."""
from __future__ import annotations

import base64
import io
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn

from apps.importer import Settings, create_app
from clients.importer_cli.__main__ import main as cli_main
from tests.test_importer import bundle


@contextmanager
def nexus_emulator():
    state = {"metadata": None, "tarball": None, "publishes": 0, "enabled": True, "drop_publish_response": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, code, body=b"", content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            expected = "Basic " + base64.b64encode(b"alice:good").decode()
            return state["enabled"] and self.headers.get("Authorization") == expected

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path != "/service/extdirect":
                return self.reply(404)
            if not self.authorized():
                return self.reply(403)
            self.reply(200, json.dumps({"result": {"data": {"id": "alice"}}}).encode())

        def do_GET(self):
            if not self.authorized():
                return self.reply(403)
            if self.path == "/repository/npm-hosted/sample-pkg":
                if not state["metadata"]:
                    return self.reply(404)
                return self.reply(200, json.dumps(state["metadata"]).encode())
            if self.path.endswith("/-/sample-pkg-1.0.0.tgz"):
                if not state["tarball"]:
                    return self.reply(404)
                return self.reply(200, state["tarball"], "application/octet-stream")
            self.reply(404)

        def do_PUT(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            if not self.authorized():
                return self.reply(403)
            if self.path != "/repository/npm-hosted/sample-pkg":
                return self.reply(404)
            payload = json.loads(body)
            if state["metadata"]:
                return self.reply(409)
            filename, attachment = next(iter(payload["_attachments"].items()))
            state["tarball"] = base64.b64decode(attachment["data"])
            state["metadata"] = {"versions": payload["versions"]}
            state["publishes"] += 1
            if state["drop_publish_response"]:
                self.close_connection = True
                return
            self.reply(201, b'{"ok":true}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def importer_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_actual_http_nexus_and_cli_flow(tmp_path, monkeypatch, capsys):
    archive = bundle(tmp_path)
    with nexus_emulator() as (nexus_url, nexus_state):
        app = create_app(Settings(data_dir=tmp_path / "data", nexus_url=nexus_url, repositories={"npm-hosted": "npm"}, allow_insecure_http=True))
        with importer_server(app) as url:
            monkeypatch.setattr("sys.stdin", io.StringIO("good\n"))
            assert cli_main(["--url", url, "--allow-http", "--json", "login", "alice", "--password-stdin"]) == 0
            token = json.loads(capsys.readouterr().out)["token"]
            common = ["--url", url, "--allow-http", "--token", token, "--json"]
            assert cli_main(common + ["submit", str(archive), "--repository", "npm-hosted"]) == 0
            task_id = json.loads(capsys.readouterr().out)["id"]
            assert cli_main(common + ["preview", task_id]) == 0
            assert json.loads(capsys.readouterr().out)["artifact_count"] == 1
            assert cli_main(common + ["confirm", task_id]) == 0
            capsys.readouterr()
            assert cli_main(common + ["wait", task_id, "--timeout", "10"]) == 0
            assert json.loads(capsys.readouterr().out)["status"] == "COMPLETED"
            assert cli_main(common + ["report", task_id]) == 0
            assert json.loads(capsys.readouterr().out)["files"][0]["status"] == "UPLOADED"
            assert nexus_state["publishes"] == 1


def test_nexus_identity_revocation_pauses_task(tmp_path):
    archive = bundle(tmp_path)
    with nexus_emulator() as (nexus_url, state):
        app = create_app(Settings(data_dir=tmp_path / "data", nexus_url=nexus_url, repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False))
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "good"})
            token = login.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            with archive.open("rb") as source:
                task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (archive.name, source, "application/zip")}).json()["id"]
            from apps.importer.db import Task
            with app.state.runtime.sessions.begin() as db:
                db.get(Task, task_id).status = "QUEUED"
            state["enabled"] = False
            app.state.runtime._process(task_id)
            assert client.get(f"/api/v1/imports/{task_id}", headers=headers).json()["status"] == "NEEDS_AUTH"
            assert state["publishes"] == 0


def test_publish_response_loss_reconciles_remote_bytes(tmp_path):
    archive = bundle(tmp_path)
    with nexus_emulator() as (nexus_url, state):
        state["drop_publish_response"] = True
        app = create_app(Settings(data_dir=tmp_path / "data", nexus_url=nexus_url, repositories={"npm-hosted": "npm"}, allow_insecure_http=True, run_worker=False))
        from fastapi.testclient import TestClient
        from apps.importer.db import Task
        with TestClient(app) as client:
            login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "good"})
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            with archive.open("rb") as source:
                task_id = client.post("/api/v1/imports", headers=headers, data={"repository": "npm-hosted"}, files={"bundle": (archive.name, source, "application/zip")}).json()["id"]
            with app.state.runtime.sessions.begin() as db:
                db.get(Task, task_id).status = "QUEUED"
            app.state.runtime._process(task_id)
            report = client.get(f"/api/v1/imports/{task_id}/report", headers=headers).json()
            assert report["status"] == "COMPLETED"
            assert report["files"][0]["status"] == "UPLOADED"
            assert state["publishes"] == 1
