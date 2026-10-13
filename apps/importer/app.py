from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from packages.common.bundle import BundleLimits, validate_bundle

from .db import Audit, Idempotency, Task, TaskFile, make_session_factory, utcnow
from .nexus import NexusClient, NexusFailure
from .storage_lock import storage_lock


@dataclass
class Settings:
    data_dir: Path = Path(".packferry-importer")
    nexus_url: str = ""
    repositories: dict[str, str] = field(default_factory=dict)  # name -> pypi/npm
    maintainers: set[str] = field(default_factory=set)
    max_bundle_bytes: int = 5 * 1024**3
    bundle_retention_days: int = 30
    credential_ttl_seconds: int = 3600
    session_ttl_seconds: int = 3600
    allow_insecure_http: bool = False
    verify_nexus_tls: bool = True
    run_worker: bool = True


class LoginBody(BaseModel):
    username: str
    password: str


class AuthorizeBody(BaseModel):
    password: str


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


class _BodyLimitExceeded(Exception):
    pass


class UploadBodyLimit:
    """Limit request bytes before Starlette's multipart parser spools them."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes + 1024 * 1024  # multipart framing allowance

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/v1/imports":
            return await self.app(scope, receive, send)
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        response = JSONResponse(status_code=413, content={"error": {"code": "BUNDLE_TOO_LARGE", "message": "Bundle 超过大小限制", "retryable": False}})
                        return await response(scope, receive, send)
                except ValueError:
                    pass
        count = 0

        async def limited_receive():
            nonlocal count
            message = await receive()
            if message["type"] == "http.request":
                count += len(message.get("body", b""))
                if count > self.max_bytes:
                    raise _BodyLimitExceeded()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyLimitExceeded:
            response = JSONResponse(status_code=413, content={"error": {"code": "BUNDLE_TOO_LARGE", "message": "Bundle 超过大小限制", "retryable": False}})
            await response(scope, receive, send)


class Runtime:
    def __init__(self, settings: Settings, nexus_factory: Callable[..., NexusClient]):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "bundles").mkdir(exist_ok=True)
        self.sessions = make_session_factory(str(settings.data_dir / "importer.sqlite3"))
        self.nexus_factory = nexus_factory
        self.tokens: dict[str, tuple[str, float, str]] = {}
        self.credentials: dict[str, tuple[str, float]] = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.worker: threading.Thread | None = None
        self._worker_lock_file = None

    def client(self, username: str, password: str):
        return self.nexus_factory(self.settings.nexus_url, username, password, verify_tls=self.settings.verify_nexus_tls)

    def authorize(self, username: str, password: str):
        client = self.client(username, password)
        try:
            if not client.authenticate():
                raise APIError(401, "INVALID_CREDENTIALS", "Nexus 账号或密码无效")
        finally:
            client.close()
        with self.lock:
            self.credentials[username] = (password, time.monotonic() + self.settings.credential_ttl_seconds)

    def credential(self, username: str) -> str | None:
        with self.lock:
            value = self.credentials.get(username)
            if not value:
                return None
            if time.monotonic() >= value[1]:
                self.credentials.pop(username, None)
                return None
            return value[0]

    def issue_token(self, username: str):
        token, csrf = "pf_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = (username, time.monotonic() + self.settings.session_ttl_seconds, csrf)
        return token, csrf

    def principal(self, request: Request, *, write: bool = False) -> str:
        auth = request.headers.get("authorization", "")
        bearer = auth.startswith("Bearer ")
        token = auth[7:] if bearer else request.cookies.get("packferry_session", "")
        with self.lock:
            entry = self.tokens.get(token)
            if entry and time.monotonic() >= entry[1]:
                self.tokens.pop(token, None)
                entry = None
        if not entry:
            raise APIError(401, "SESSION_EXPIRED", "请重新登录")
        if write and not bearer:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin != expected or request.headers.get("x-csrf-token") != entry[2]:
                raise APIError(403, "CSRF_REJECTED", "请求来源或 CSRF 校验失败")
        return entry[0]

    def start(self):
        if self.settings.run_worker:
            lock_path = self.settings.data_dir / "worker.lock"
            lock_path.touch(exist_ok=True)
            lock_file = lock_path.open("r+b")
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            lock_file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                lock_file.close()
                raise RuntimeError("Importer requires a single API/Worker process") from exc
            self._worker_lock_file = lock_file
        with self.sessions() as db:
            tasks = db.scalars(select(Task).where(Task.status.in_(["QUEUED", "RUNNING", "INTERRUPTED"]))).all()
            for task in tasks:
                task.status = "NEEDS_AUTH"
                task.updated_at = utcnow()
                db.add(Audit(task_id=task.id, actor="system", event="RECOVERY_NEEDS_AUTH"))
            db.commit()
        if self.settings.run_worker:
            self.worker = threading.Thread(target=self._work_loop, name="packferry-import-worker", daemon=True)
            self.worker.start()

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=10)
        if not self.worker or not self.worker.is_alive():
            self._release_worker_lock()

    def _release_worker_lock(self):
        with self.lock:
            if self._worker_lock_file:
                self._worker_lock_file.close()
                self._worker_lock_file = None

    def _work_loop(self):
        last_cleanup = 0.0
        while not self.stop.is_set():
            try:
                if time.monotonic() - last_cleanup > 3600:
                    self.cleanup_finished_bundles()
                    last_cleanup = time.monotonic()
                with self.sessions() as db:
                    task_id = db.scalar(select(Task.id).where(Task.status == "QUEUED").order_by(Task.created_at).limit(1))
                if task_id:
                    try:
                        self._process(task_id)
                    except Exception:
                        self._set_task(task_id, "INTERRUPTED", "Worker 意外中断，需重新核实")
                    continue
            except Exception:
                # The task remains in SQLite and is reconciled on next startup.
                pass
            self.wake.wait(1)
            self.wake.clear()
        self._release_worker_lock()

    def _set_file(self, task_id: str, file_id: int, status: str, error: str | None = None):
        with self.sessions.begin() as db:
            row = db.get(TaskFile, file_id)
            row.status, row.error = status, error
            task = db.get(Task, task_id)
            task.updated_at = utcnow()
            db.add(Audit(task_id=task_id, actor=task.owner, event=f"FILE_{status}", detail=json.dumps({"artifact_id": row.artifact_id, "error": error}, ensure_ascii=False)))

    def _set_task(self, task_id: str, status: str, error: str | None = None):
        with self.sessions.begin() as db:
            task = db.get(Task, task_id)
            task.status, task.error, task.updated_at = status, error, utcnow()
            if status in {"COMPLETED", "PARTIAL", "FAILED", "EXPIRED"}:
                task.completed_at = utcnow()
            db.add(Audit(task_id=task_id, actor=task.owner, event=f"TASK_{status}"))

    def _process(self, task_id: str):
        with self.sessions() as db:
            task = db.get(Task, task_id)
            if not task or task.status != "QUEUED":
                return
            owner, repository, ecosystem = task.owner, task.repository, task.ecosystem
            manifest, bundle_path = json.loads(task.manifest_json), Path(task.zip_path)
            rows = [(row.id, row.artifact_id, row.status) for row in task.files]
        password = self.credential(owner)
        if not password:
            self._set_task(task_id, "NEEDS_AUTH", "需要原任务所有者重新授权")
            return
        self._set_task(task_id, "RUNNING")
        client = self.client(owner, password)
        try:
            packages = {p["node_id"]: p for p in manifest["packages"]}
            artifacts = {a["artifact_id"]: a for a in manifest["artifacts"]}
            for file_id, artifact_id, old_status in rows:
                if self.stop.is_set():
                    self._set_task(task_id, "INTERRUPTED")
                    return
                if self.credential(owner) != password:
                    self._set_task(task_id, "NEEDS_AUTH", "后台授权已撤销或过期")
                    return
                if not client.authenticate():
                    with self.lock:
                        self.credentials.pop(owner, None)
                    self._set_task(task_id, "NEEDS_AUTH", "Nexus 账号授权失效")
                    return
                if old_status in {"UPLOADED", "SKIPPED", "CONFLICT", "FAILED"}:
                    continue
                artifact = artifacts[artifact_id]
                node_ids = artifact.get("package_node_ids", [])
                package = packages[node_ids[0]] if node_ids else None
                if package is None:
                    self._set_file(task_id, file_id, "FAILED", "制品缺少包身份")
                    continue
                try:
                    self._set_file(task_id, file_id, "CHECKING")
                    remote = self._check_with_retry(client, repository, ecosystem, artifact, package)
                    if remote.state == "SAME":
                        self._set_file(task_id, file_id, "SKIPPED")
                        continue
                    if remote.state == "CONFLICT":
                        self._set_file(task_id, file_id, "CONFLICT", "远端同身份内容不同")
                        continue
                    if remote.state != "ABSENT":
                        self._set_file(task_id, file_id, "UNKNOWN", remote.reason)
                        continue
                    if old_status in {"UPLOADING", "VERIFYING", "UNKNOWN"}:
                        self._set_file(task_id, file_id, "UNKNOWN", "先前发布结果不明，远端未找到；需新任务重试")
                        continue
                    with zipfile.ZipFile(bundle_path) as zf, tempfile.TemporaryDirectory() as tmp:
                        file_path = Path(tmp) / artifact["filename"]
                        digest = hashlib.sha256()
                        with zf.open(artifact["archive_path"]) as src, file_path.open("wb") as dest:
                            while block := src.read(1024 * 1024):
                                digest.update(block)
                                dest.write(block)
                        if digest.hexdigest() != artifact["sha256"]:
                            self._set_file(task_id, file_id, "FAILED", "Bundle 文件哈希变化")
                            continue
                        # Preview/check is a snapshot; verify once more immediately before publish.
                        remote = self._check_with_retry(client, repository, ecosystem, artifact, package)
                        if remote.state == "SAME":
                            self._set_file(task_id, file_id, "SKIPPED")
                            continue
                        if remote.state == "CONFLICT":
                            self._set_file(task_id, file_id, "CONFLICT", "远端同身份内容不同")
                            continue
                        if remote.state != "ABSENT":
                            self._set_file(task_id, file_id, "UNKNOWN", remote.reason)
                            continue
                        if self.credential(owner) != password:
                            self._set_task(task_id, "NEEDS_AUTH", "后台授权已撤销或过期")
                            return
                        if not client.authenticate():
                            with self.lock:
                                self.credentials.pop(owner, None)
                            self._set_task(task_id, "NEEDS_AUTH", "Nexus 账号授权失效")
                            return
                        self._set_file(task_id, file_id, "UPLOADING")
                        with self.sessions.begin() as db:
                            db.get(TaskFile, file_id).attempts += 1
                        publish_error = None
                        try:
                            client.publish(repository, ecosystem, artifact, package, file_path)
                        except NexusFailure as exc:
                            publish_error = exc
                        self._set_file(task_id, file_id, "VERIFYING")
                        if not client.authenticate():
                            with self.lock:
                                self.credentials.pop(owner, None)
                            self._set_task(task_id, "NEEDS_AUTH", "Nexus 账号授权失效，发布结果待核实")
                            return
                        remote = self._check_with_retry(client, repository, ecosystem, artifact, package)
                        if remote.state == "SAME":
                            self._set_file(task_id, file_id, "UPLOADED")
                        elif remote.state == "CONFLICT":
                            self._set_file(task_id, file_id, "CONFLICT", "发布后远端字节不同")
                        else:
                            self._set_file(task_id, file_id, "UNKNOWN", (publish_error.code if publish_error else remote.reason) or "发布结果无法核实")
                except NexusFailure as exc:
                    if exc.code == "NEXUS_AUTH_EXPIRED":
                        with self.lock:
                            self.credentials.pop(owner, None)
                        self._set_task(task_id, "NEEDS_AUTH", "Nexus 授权失效")
                        return
                    self._set_file(task_id, file_id, "UNKNOWN", exc.code)
            with self.sessions() as db:
                statuses = [r.status for r in db.scalars(select(TaskFile).where(TaskFile.task_id == task_id))]
            if statuses and all(s in {"UPLOADED", "SKIPPED"} for s in statuses):
                self._set_task(task_id, "COMPLETED")
            elif any(s in {"UPLOADED", "SKIPPED"} for s in statuses):
                self._set_task(task_id, "PARTIAL")
            else:
                self._set_task(task_id, "FAILED")
        finally:
            client.close()

    def _check_with_retry(self, client, repository, ecosystem, artifact, package):
        for attempt in range(3):
            try:
                return client.check(repository, ecosystem, artifact, package)
            except NexusFailure as exc:
                if not exc.retryable or attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def cleanup_finished_bundles(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.bundle_retention_days)).isoformat()
        with storage_lock(self.settings.data_dir):
            with self.sessions.begin() as db:
                tasks = db.scalars(select(Task).where(Task.status.in_(["COMPLETED", "PARTIAL", "FAILED", "EXPIRED"]), Task.completed_at < cutoff, Task.zip_path.is_not(None))).all()
                for task in tasks:
                    task.zip_path = None
                    db.add(Audit(task_id=task.id, actor="system", event="BUNDLE_CLEANUP"))
            with self.sessions() as db:
                referenced = {Path(path).resolve() for path in db.scalars(select(Task.zip_path).where(Task.zip_path.is_not(None)))}
            for path in (self.settings.data_dir / "bundles").glob("*.zip"):
                if path.resolve() not in referenced:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        # Preserve the on-disk orphan for a later cleanup pass.
                        pass


def _task_dict(task: Task) -> dict:
    return {"id": task.id, "status": task.status, "owner": task.owner, "repository": task.repository, "ecosystem": task.ecosystem, "created_at": task.created_at, "updated_at": task.updated_at, "zip_sha256": task.zip_sha256, "error": task.error}


def create_app(settings: Settings | None = None, nexus_client_factory: Callable[..., NexusClient] = NexusClient) -> FastAPI:
    settings = settings or Settings()
    parsed_url = urlsplit(settings.nexus_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise ValueError("Nexus URL must be an HTTP(S) base URL without credentials, query or fragment")
    if unquote(parsed_url.path) != parsed_url.path or any(part in {".", ".."} for part in parsed_url.path.split("/")):
        raise ValueError("Nexus URL path is invalid")
    if not settings.allow_insecure_http and parsed_url.scheme != "https":
        raise ValueError("Nexus URL must use HTTPS")
    if not settings.repositories or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) or ecosystem not in {"pypi", "npm"} for name, ecosystem in settings.repositories.items()):
        raise ValueError("Repository whitelist must contain safe names mapped to pypi/npm")
    runtime = Runtime(settings, nexus_client_factory)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        runtime.start()
        yield
        runtime.shutdown()

    app = FastAPI(title="PackFerry Importer", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(UploadBodyLimit, max_bytes=settings.max_bundle_bytes)

    @app.exception_handler(APIError)
    async def api_error(_request, exc: APIError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}})

    @app.exception_handler(NexusFailure)
    async def nexus_error(_request, exc: NexusFailure):
        return JSONResponse(status_code=503 if exc.retryable else 401, content={"error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse(status_code=422, content={"error": {"code": "BAD_REQUEST", "message": "请求字段无效", "retryable": False}})

    @app.middleware("http")
    async def transport_guard(request: Request, call_next):
        if request.url.path.startswith("/api/v1/") and not settings.allow_insecure_http and request.url.scheme != "https":
            return JSONResponse(status_code=403, content={"error": {"code": "TLS_REQUIRED", "message": "请通过 HTTPS 访问", "retryable": False}})
        return await call_next(request)

    def require_tls(request: Request):
        if not settings.allow_insecure_http and request.url.scheme != "https":
            raise APIError(403, "TLS_REQUIRED", "请通过 HTTPS 访问")

    def owned(request: Request, task_id: str, *, write=False) -> tuple[str, Task]:
        owner = runtime.principal(request, write=write)
        with runtime.sessions() as db:
            task = db.get(Task, task_id)
            if not task or (task.owner != owner and owner not in settings.maintainers):
                raise APIError(404, "NOT_FOUND", "任务不存在")
            return owner, task

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        return {"status": "ready" if runtime.worker is None or runtime.worker.is_alive() else "unready"}

    @app.post("/api/v1/auth/login")
    def login(request: Request, body: LoginBody):
        require_tls(request)
        runtime.authorize(body.username, body.password)
        token, csrf = runtime.issue_token(body.username)
        response = JSONResponse({"username": body.username, "token": token, "csrf_token": csrf})
        response.set_cookie("packferry_session", token, httponly=True, secure=not settings.allow_insecure_http, samesite="strict", max_age=settings.session_ttl_seconds)
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request):
        username = runtime.principal(request, write=True)
        with runtime.lock:
            for issued, entry in list(runtime.tokens.items()):
                if entry[0] == username:
                    runtime.tokens.pop(issued, None)
            runtime.credentials.pop(username, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("packferry_session")
        return response

    @app.get("/api/v1/auth/me")
    def me(request: Request):
        return {"username": runtime.principal(request)}

    @app.get("/api/v1/repositories")
    def repositories(request: Request):
        runtime.principal(request)
        return [{"name": name, "ecosystem": ecosystem} for name, ecosystem in settings.repositories.items()]

    @app.post("/api/v1/imports", status_code=201)
    async def upload(request: Request, bundle: UploadFile = File(...), repository: str = Form(...), idempotency_key: str | None = Header(None)):
        owner = runtime.principal(request, write=True)
        if repository not in settings.repositories:
            raise APIError(422, "BAD_REPOSITORY", "目标仓库不在白名单")
        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 255):
            raise APIError(422, "BAD_IDEMPOTENCY_KEY", "幂等键无效")
        fd, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=settings.data_dir / "bundles")
        os.close(fd)
        temp_path = Path(temp_name)
        digest, size = hashlib.sha256(), 0
        try:
            with temp_path.open("wb") as out:
                while chunk := await bundle.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_bundle_bytes:
                        raise APIError(413, "BUNDLE_TOO_LARGE", "Bundle 超过大小限制")
                    digest.update(chunk)
                    out.write(chunk)
            fingerprint = f"{repository}:{digest.hexdigest()}"
            if idempotency_key:
                with runtime.sessions() as db:
                    record = db.scalar(select(Idempotency).where(Idempotency.actor == owner, Idempotency.key == idempotency_key))
                    if record:
                        if record.operation != "upload" or record.fingerprint != fingerprint:
                            raise APIError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求")
                        return _task_dict(db.get(Task, record.task_id))
            try:
                manifest_obj = await run_in_threadpool(validate_bundle, temp_path, BundleLimits(max_zip_size=settings.max_bundle_bytes))
                manifest = manifest_obj.model_dump(mode="json") if hasattr(manifest_obj, "model_dump") else manifest_obj
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                raise APIError(422, "INVALID_BUNDLE", str(exc)) from exc
            if manifest["ecosystem"] != settings.repositories[repository]:
                raise APIError(422, "ECOSYSTEM_MISMATCH", "Bundle 生态与仓库不一致")
            task_id = str(uuid.uuid4())
            final_path = settings.data_dir / "bundles" / f"{task_id}.zip"

            def persist_upload():
                with storage_lock(settings.data_dir):
                    os.replace(temp_path, final_path)
                    try:
                        with runtime.sessions.begin() as db:
                            task = Task(id=task_id, owner=owner, repository=repository, ecosystem=manifest["ecosystem"], status="READY", bundle_id=manifest["bundle_id"], zip_sha256=digest.hexdigest(), zip_path=str(final_path), manifest_json=json.dumps(manifest, ensure_ascii=False))
                            db.add(task)
                            for artifact in manifest["artifacts"]:
                                db.add(TaskFile(task_id=task_id, artifact_id=artifact["artifact_id"], filename=artifact["filename"], sha256=artifact["sha256"]))
                            db.add(Audit(task_id=task_id, actor=owner, event="UPLOAD", detail=json.dumps({"repository": repository, "zip_sha256": digest.hexdigest()})))
                            if idempotency_key:
                                db.add(Idempotency(actor=owner, key=idempotency_key, operation="upload", fingerprint=fingerprint, task_id=task_id))
                        return _task_dict(task)
                    except IntegrityError as exc:
                        final_path.unlink(missing_ok=True)
                        if idempotency_key:
                            with runtime.sessions() as db:
                                record = db.scalar(select(Idempotency).where(Idempotency.actor == owner, Idempotency.key == idempotency_key))
                                if record and record.operation == "upload" and record.fingerprint == fingerprint:
                                    return _task_dict(db.get(Task, record.task_id))
                        raise APIError(409, "IDEMPOTENCY_CONFLICT", "幂等键冲突，请查询操作结果") from exc
                    except Exception:
                        final_path.unlink(missing_ok=True)
                        raise

            return await run_in_threadpool(persist_upload)
        finally:
            temp_path.unlink(missing_ok=True)

    @app.get("/api/v1/operations/{key}")
    def operation(request: Request, key: str):
        owner = runtime.principal(request)
        with runtime.sessions() as db:
            record = db.scalar(select(Idempotency).where(Idempotency.actor == owner, Idempotency.key == key))
            if not record:
                raise APIError(404, "NOT_FOUND", "操作不存在")
            return {"operation": record.operation, "task": _task_dict(db.get(Task, record.task_id))}

    @app.get("/api/v1/imports")
    def list_imports(request: Request, limit: int = 50, offset: int = 0):
        owner = runtime.principal(request)
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        with runtime.sessions() as db:
            query = select(Task)
            count_query = select(func.count(Task.id))
            if owner not in settings.maintainers:
                query = query.where(Task.owner == owner)
                count_query = count_query.where(Task.owner == owner)
            rows = db.scalars(query.order_by(Task.created_at.desc()).offset(offset).limit(limit)).all()
            return {"items": [_task_dict(t) for t in rows], "total": db.scalar(count_query) or 0}

    @app.get("/api/v1/imports/{task_id}")
    def detail(request: Request, task_id: str):
        _, task = owned(request, task_id)
        return _task_dict(task)

    @app.get("/api/v1/imports/{task_id}/preview")
    def preview(request: Request, task_id: str):
        _, task = owned(request, task_id)
        manifest = json.loads(task.manifest_json)
        return {"id": task.id, "status": task.status, "repository": task.repository, "ecosystem": task.ecosystem, "package_count": len(manifest["packages"]), "artifact_count": len(manifest["artifacts"]), "warnings": manifest["warnings"], "verification": manifest["verification"]}

    @app.get("/api/v1/imports/{task_id}/files")
    def files(request: Request, task_id: str, limit: int = 100, offset: int = 0):
        owned(request, task_id)
        limit, offset = min(max(limit, 1), 500), max(offset, 0)
        with runtime.sessions() as db:
            rows = db.scalars(select(TaskFile).where(TaskFile.task_id == task_id).order_by(TaskFile.id).offset(offset).limit(limit)).all()
            total = db.scalar(select(func.count(TaskFile.id)).where(TaskFile.task_id == task_id)) or 0
            return {"items": [{"artifact_id": r.artifact_id, "filename": r.filename, "status": r.status, "error": r.error} for r in rows], "total": total}

    @app.post("/api/v1/imports/{task_id}/confirm")
    def confirm(request: Request, task_id: str, idempotency_key: str | None = Header(None)):
        owner, _ = owned(request, task_id, write=True)
        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 255):
            raise APIError(422, "BAD_IDEMPOTENCY_KEY", "幂等键无效")
        try:
            with runtime.sessions.begin() as db:
                task = db.get(Task, task_id)
                if task.owner != owner:
                    raise APIError(403, "OWNER_REQUIRED", "仅原任务所有者可确认")
                if idempotency_key:
                    record = db.scalar(select(Idempotency).where(Idempotency.actor == owner, Idempotency.key == idempotency_key))
                    if record:
                        if record.operation != "confirm" or record.task_id != task_id:
                            raise APIError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求")
                        return _task_dict(task)
                changed = db.execute(update(Task).where(Task.id == task_id, Task.status == "READY").values(status="QUEUED", updated_at=utcnow()))
                if changed.rowcount != 1:
                    raise APIError(409, "BAD_STATE", "任务当前状态不能确认")
                db.add(Audit(task_id=task_id, actor=owner, event="CONFIRM"))
                if idempotency_key:
                    db.add(Idempotency(actor=owner, key=idempotency_key, operation="confirm", fingerprint=task_id, task_id=task_id))
        except (IntegrityError, APIError) as exc:
            if isinstance(exc, APIError) and exc.code != "BAD_STATE":
                raise
            if idempotency_key:
                for _ in range(5):
                    with runtime.sessions() as db:
                        record = db.scalar(select(Idempotency).where(Idempotency.actor == owner, Idempotency.key == idempotency_key))
                        if record:
                            if record.operation == "confirm" and record.task_id == task_id:
                                return _task_dict(db.get(Task, task_id))
                            raise APIError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求")
                    time.sleep(0.02)
            if isinstance(exc, APIError):
                raise
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "幂等键冲突，请查询操作结果") from exc
        runtime.wake.set()
        with runtime.sessions() as db:
            return _task_dict(db.get(Task, task_id))

    @app.post("/api/v1/imports/{task_id}/authorize")
    def reauthorize(request: Request, task_id: str, body: AuthorizeBody):
        owner, task = owned(request, task_id, write=True)
        if task.owner != owner:
            raise APIError(403, "OWNER_REQUIRED", "仅原任务所有者可重新授权")
        if task.status not in {"NEEDS_AUTH", "INTERRUPTED"}:
            raise APIError(409, "BAD_STATE", "任务当前状态无需重新授权")
        runtime.authorize(owner, body.password)
        with runtime.sessions.begin() as db:
            task = db.get(Task, task_id)
            task.status, task.error, task.updated_at = "QUEUED", None, utcnow()
            db.add(Audit(task_id=task_id, actor=owner, event="REAUTHORIZE"))
        runtime.wake.set()
        return _task_dict(task)

    @app.get("/api/v1/imports/{task_id}/report")
    def report(request: Request, task_id: str):
        _, task = owned(request, task_id)
        with runtime.sessions() as db:
            rows = db.scalars(select(TaskFile).where(TaskFile.task_id == task_id).order_by(TaskFile.id)).all()
            return {**_task_dict(task), "files": [{"artifact_id": r.artifact_id, "filename": r.filename, "sha256": r.sha256, "status": r.status, "error": r.error, "attempts": r.attempts} for r in rows]}

    @app.get("/api/v1/audit/events")
    def audit(request: Request, limit: int = 100, offset: int = 0):
        owner = runtime.principal(request)
        if owner not in settings.maintainers:
            raise APIError(403, "MAINTAINER_REQUIRED", "需要维护者权限")
        limit, offset = min(max(limit, 1), 500), max(offset, 0)
        with runtime.sessions() as db:
            rows = db.scalars(select(Audit).order_by(Audit.id.desc()).offset(offset).limit(limit)).all()
            return {"items": [{"id": r.id, "task_id": r.task_id, "actor": r.actor, "event": r.event, "detail": json.loads(r.detail), "created_at": r.created_at} for r in rows], "total": db.scalar(select(func.count(Audit.id))) or 0}

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend" / "importer" / "dist"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="importer-ui")
    else:
        @app.get("/")
        def frontend_missing():
            return JSONResponse(status_code=503, content={"error": {"code": "UI_NOT_BUILT", "message": "Importer Web UI 尚未构建", "retryable": False}})

    return app
