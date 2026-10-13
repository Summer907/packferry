"""Local Exporter API. Authenticated loopback only; the CLI shares ExportService."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError
from platformdirs import user_data_path

from packages.common.models import Environment


class ExportBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ecosystem: str
    constraints: list[str] = Field(default_factory=list)
    environments: list[Environment] = Field(default_factory=list)
    project_path: str | None = None
    resolution_policy: str = 'fresh'
    lock_policy: str = 'prefer'
    production_only: bool = False


class LocalTasks:
    def __init__(self, root: Path, service_factory=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'exporter.db'
        self._instance_lock = (self.root / 'exporter-api.lock').open('a+b')
        self._instance_lock.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self._instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._instance_lock.close()
            raise RuntimeError('An Exporter desktop/browser server is already using this data directory') from None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='packferry-export')
        self.service_factory = service_factory
        self.lock = threading.Lock()
        self.active = None
        self.closed = False
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL, manifest TEXT, error TEXT, created_at TEXT NOT NULL)')
            connection.execute("UPDATE tasks SET status='INTERRUPTED', error=? WHERE status IN ('QUEUED','RUNNING')",
                               (json.dumps({'code': 'INTERRUPTED', 'message': 'Previous process stopped; rerun to reuse verified cache', 'retryable': True}),))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, task_id: str):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Task not found')
        result = dict(row)
        for key in ['request', 'manifest', 'error']:
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def running(self):
        with self.connect() as connection:
            return connection.execute("SELECT count(*) FROM tasks WHERE status IN ('QUEUED','RUNNING')").fetchone()[0] > 0

    def submit(self, request):
        with self.lock:
            if self.closed:
                raise HTTPException(503, 'Exporter is stopping')
            task_id = str(uuid4())
            with self.connect() as connection:
                connection.execute('INSERT INTO tasks(id,status,request,created_at) VALUES(?,?,?,?)',
                                   (task_id, 'QUEUED', json.dumps(request.model_dump(mode='json')), datetime.now(timezone.utc).isoformat()))
            self.pool.submit(self._run, task_id, request)
        return self.get(task_id)

    def _run(self, task_id, request):
        try:
            with self.lock:
                if self.closed:
                    return
                if self.service_factory is None:
                    from .services import ExportService
                    service = ExportService(self.root)
                else:
                    service = self.service_factory(self.root)
                self.active = service
            with self.connect() as connection:
                connection.execute("UPDATE tasks SET status='RUNNING' WHERE id=?", (task_id,))
            output = self.root / 'bundles' / f'{task_id}.zip'
            manifest = service.export(request, output)
            data = manifest.model_dump(mode='json')
            with self.connect() as connection:
                connection.execute("UPDATE tasks SET status='COMPLETED',manifest=? WHERE id=?", (json.dumps(data), task_id))
        except Exception as exc:
            # Third-party subprocess stderr can include source credentials: never expose it.
            code = getattr(exc, 'code', 'EXPORT_FAILED')
            with self.connect() as connection:
                status = 'INTERRUPTED' if self.closed else 'FAILED'
                error = {'code': code, 'message': 'Export did not complete; inspect local diagnostics and retry', 'retryable': True}
                connection.execute('UPDATE tasks SET status=?,error=? WHERE id=?', (status, json.dumps(error), task_id))
        finally:
            with self.lock:
                self.active = None

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.active is not None and hasattr(self.active, 'cancel'):
                self.active.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.connect() as connection:
            connection.execute("UPDATE tasks SET status='INTERRUPTED' WHERE status IN ('QUEUED','RUNNING')")
        if not self._instance_lock.closed:
            self._instance_lock.close()


def static_dir(app: str):
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
    return root / 'frontend' / app / 'dist'


def create_app(data_dir: Path | None = None, token: str | None = None, service_factory=None):
    root = data_dir or user_data_path('PackFerry' if sys.platform == 'win32' else 'packferry', appauthor=False)
    secret = token or secrets.token_urlsafe(32)
    tasks = LocalTasks(root, service_factory)
    session = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        tasks.close()

    app = FastAPI(title='PackFerry Exporter', version='0.1.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.tasks = tasks
    app.state.local_token = secret

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({'error': {'code': 'INVALID_REQUEST', 'message': 'Invalid export request fields', 'retryable': False}}, 422)

    @app.exception_handler(ValidationError)
    async def invalid_model(request, exc):
        return JSONResponse({'error': {'code': 'INVALID_REQUEST', 'message': 'Choose one supported package/project input and resolution policy', 'retryable': False}}, 422)

    @app.middleware('http')
    async def local_guard(request: Request, call_next):
        host = urlsplit('http://' + request.headers.get('host', '')).hostname
        if host not in {'127.0.0.1', 'localhost', 'testserver'}:
            return JSONResponse({'error': {'code': 'INVALID_HOST', 'message': 'Loopback only', 'retryable': False}}, 403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error': {'code': 'INVALID_ORIGIN', 'message': 'Same-origin only', 'retryable': False}}, 403)
        if request.url.path.startswith('/api/') and request.url.path != '/api/v1/local/session':
            candidate = request.cookies.get('pf_local', '')
            if not secrets.compare_digest(candidate, session):
                return JSONResponse({'error': {'code': 'UNAUTHORIZED', 'message': 'Local session required', 'retryable': False}}, 401)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.post('/api/v1/local/session')
    async def login(request: Request):
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, 'Invalid JSON') from None
        candidate = data.get('token', '') if isinstance(data, dict) else ''
        if not isinstance(candidate, str) or not secrets.compare_digest(candidate, secret):
            raise HTTPException(401, 'Invalid local token')
        response = JSONResponse({'ok': True})
        response.set_cookie('pf_local', session, httponly=True, samesite='strict')
        return response

    @app.get('/health/ready')
    async def health():
        return {'ready': True}

    @app.post('/api/v1/exports', status_code=202)
    def submit(body: ExportBody):
        from .services import ExportRequest
        request = ExportRequest.model_validate(body.model_dump())
        result = tasks.submit(request)
        return {'id': result['id'], 'status': result['status']}

    @app.get('/api/v1/exports/{task_id}')
    def detail(task_id: str):
        return tasks.get(task_id)

    @app.get('/api/v1/exports/{task_id}/bundle')
    def bundle(task_id: str):
        task = tasks.get(task_id)
        path = tasks.root / 'bundles' / f'{task_id}.zip'
        if task['status'] != 'COMPLETED' or not path.is_file():
            raise HTTPException(409, 'Bundle is not ready')
        return FileResponse(path, filename=f'packferry-{task_id}.zip', media_type='application/zip')

    directory = static_dir('exporter')
    if directory.is_dir():
        app.mount('/', StaticFiles(directory=directory, html=True), name='ui')
    else:
        @app.get('/')
        def missing_ui():
            return JSONResponse({'error': {'code': 'UI_NOT_BUILT', 'message': 'Build frontend/exporter first', 'retryable': False}}, 503)
    return app
