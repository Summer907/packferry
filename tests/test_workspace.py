"""Task observation, local favorites and permission regressions for the workspace UI."""
import json
import sqlite3
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.exporter.api import create_app as exporter_app
from apps.importer import Settings, create_app as importer_app
from apps.importer.db import Audit, TaskFile
from tests.test_importer import FakeNexus, bundle, login


class ExportStub:
    def __init__(self, root):
        self.root = root

    def export(self, request, output):
        self.observer('resolving', '解析与收集目标环境：custom')
        self.observer('bundling', '生成 Bundle ZIP')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'fixture')
        return SimpleNamespace(model_dump=lambda **_: {'verification': {'resolution_status': 'PARTIAL', 'collection_status': 'PARTIAL', 'offline_install_status': 'NOT_VERIFIED'}})


def wait(client, task_id):
    for _ in range(100):
        data = client.get(f'/api/v1/exports/{task_id}').json()
        if data['status'] not in {'QUEUED', 'RUNNING'}:
            return data
        time.sleep(.01)
    raise AssertionError('task did not finish')


def test_export_history_favorites_events_and_rerun_survive_restart(tmp_path):
    app = exporter_app(tmp_path, token='secret', service_factory=ExportStub)
    environment = {'environment_id': 'custom', 'runtime': 'cpython', 'version': '3.14', 'platform': 'manylinux_2_28_aarch64', 'abi': 'cp314', 'libc': 'glibc'}
    with TestClient(app) as client:
        assert client.get('/api/v1/favorites').status_code == 401
        client.post('/api/v1/local/session', json={'token': 'secret'})
        assert client.put('/api/v1/favorites/one', json={'name': 'ARM', 'environment': environment}).status_code == 200
        client.put('/api/v1/favorites/one', json={'name': 'ARM updated', 'environment': environment})
        assert len(client.get('/api/v1/favorites').json()['items']) == 1
        body = {'ecosystem': 'pypi', 'constraints': ['requests>=2'], 'environments': [environment]}
        task_id = client.post('/api/v1/exports', json=body).json()['id']
        result = wait(client, task_id)
        assert result['status'] == 'COMPLETED'
        assert result['manifest']['verification']['offline_install_status'] == 'NOT_VERIFIED'
        assert result['progress']['stage'] == 'completed'
        assert result['progress']['total'] is None
        events = client.get(f'/api/v1/exports/{task_id}/events?limit=2').json()
        assert len(events['items']) == 2 and events['total'] >= 4
        listing = client.get('/api/v1/exports?status=COMPLETED&limit=1').json()
        assert listing['total'] == 1 and 'manifest' not in listing['items'][0]
        rerun = client.post(f'/api/v1/exports/{task_id}/rerun').json()
        repeated = wait(client, rerun['id'])
        assert repeated['rerun_of'] == task_id
        assert repeated['request'] == result['request']
    with TestClient(exporter_app(tmp_path, token='new')) as client:
        client.post('/api/v1/local/session', json={'token': 'new'})
        assert client.get('/api/v1/favorites').json()['items'][0]['name'] == 'ARM updated'
        assert client.get('/api/v1/exports').json()['total'] == 2
        assert client.delete('/api/v1/favorites/one').status_code == 200
        assert client.get('/api/v1/favorites').json()['items'] == []


def test_old_export_database_upgrade_and_interrupted_progress(tmp_path):
    with sqlite3.connect(tmp_path / 'exporter.db') as db:
        db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY,status TEXT NOT NULL,request TEXT NOT NULL,manifest TEXT,error TEXT,created_at TEXT NOT NULL)')
        db.execute("INSERT INTO tasks(id,status,request,created_at) VALUES('old','RUNNING','{}','2026')")
    with TestClient(exporter_app(tmp_path, token='secret')) as client:
        client.post('/api/v1/local/session', json={'token': 'secret'})
        result = client.get('/api/v1/exports/old').json()
        assert result['status'] == 'INTERRUPTED' and result['progress'] is None
        assert client.get('/api/v1/exports/old/events').json()['items'] == []


def test_import_counts_use_all_files_and_events_are_owner_scoped(tmp_path):
    path = bundle(tmp_path)
    app = importer_app(Settings(data_dir=tmp_path / 'data', nexus_url='http://nexus.test', repositories={'npm-hosted': 'npm'}, allow_insecure_http=True, run_worker=False), FakeNexus)
    with TestClient(app) as client:
        alice, bob = login(client, 'alice'), login(client, 'bob')
        with path.open('rb') as source:
            task_id = client.post('/api/v1/imports', headers=alice, data={'repository': 'npm-hosted'}, files={'bundle': ('bundle.zip', source, 'application/zip')}).json()['id']
        with app.state.runtime.sessions.begin() as db:
            # More records than the page size; include unknown and conflict as processed, never successful.
            for i in range(105):
                db.add(TaskFile(task_id=task_id, artifact_id=f'extra-{i}', filename=f'{i}.tgz', sha256='0' * 64, status='UNKNOWN' if i < 100 else 'CONFLICT'))
            db.add(Audit(task_id=task_id, actor='alice', event='FILE_UNKNOWN', detail=json.dumps({'artifact_id': 'safe-id', 'error': 'https://user:secret@example.test'})))
        result = client.get(f'/api/v1/imports/{task_id}', headers=alice).json()
        assert result['progress']['total'] == 106
        assert result['progress']['completed'] == 105
        assert result['counts']['UNKNOWN'] == 100 and result['counts'].get('UPLOADED', 0) == 0
        files = client.get(f'/api/v1/imports/{task_id}/files?status=CONFLICT&limit=2', headers=alice).json()
        assert files['total'] == 5 and len(files['items']) == 2
        events = client.get(f'/api/v1/imports/{task_id}/events', headers=alice)
        assert 'secret' not in events.text and 'safe-id' in events.text
        assert client.get(f'/api/v1/imports/{task_id}/events', headers=bob).status_code == 404
        assert client.get(f'/api/v1/imports/{task_id}', headers=bob).status_code == 404
        assert client.get('/api/v1/imports?status=READY', headers=alice).json()['total'] == 1
        assert client.get('/api/v1/imports?status=FAILED', headers=alice).json()['total'] == 0
