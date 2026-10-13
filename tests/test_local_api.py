
from fastapi.testclient import TestClient
from html.parser import HTMLParser
import pytest

from apps.exporter.api import create_app


def test_loopback_auth_and_origin(tmp_path):
    app = create_app(tmp_path, token='test-secret')
    with TestClient(app) as client:
        assert client.get('/api/v1/exports/none').status_code == 401
        assert client.post('/api/v1/local/session', json={'token': 'wrong'}).status_code == 401
        assert client.post('/api/v1/local/session', json={'token': 'test-secret'}, headers={'origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/v1/local/session', json={'token': 'test-secret'}).status_code == 200
        assert client.get('/api/v1/exports/none').status_code == 404
        assert client.get('/health/ready', headers={'Host': 'evil.example'}).status_code == 403


def test_recovery_marks_interrupted(tmp_path):
    app = create_app(tmp_path, token='test')
    tasks = app.state.tasks
    with tasks.connect() as db:
        db.execute("INSERT INTO tasks(id,status,request,created_at) VALUES ('x','RUNNING','{}','today')")
    tasks.close()
    recovered = create_app(tmp_path, token='new')
    assert recovered.state.tasks.get('x')['status'] == 'INTERRUPTED'
    recovered.state.tasks.close()


def test_static_assets_have_executable_mime_and_csp(tmp_path):
    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                src = dict(attrs).get('src')
                if src:
                    self.scripts.append(src)

    with TestClient(create_app(tmp_path, token='test')) as client:
        page = client.get('/')
        if page.status_code == 503:
            pytest.skip('Frontend build is not available in Python-only checkout')
        assert page.status_code == 200
        assert "script-src 'self'" in page.headers['content-security-policy']
        parser = Assets()
        parser.feed(page.text)
        assert parser.scripts
        for script in parser.scripts:
            response = client.get(script)
            assert response.status_code == 200
            assert response.headers['content-type'].split(';')[0] in ['application/javascript', 'text/javascript']


def test_second_server_cannot_interrupt_active_server(tmp_path):
    first = create_app(tmp_path, token='first')
    try:
        with first.state.tasks.connect() as db:
            db.execute("INSERT INTO tasks(id,status,request,created_at) VALUES ('active','RUNNING','{}','today')")
        with pytest.raises(RuntimeError, match='already'):
            create_app(tmp_path, token='second')
        assert first.state.tasks.get('active')['status'] == 'RUNNING'
    finally:
        first.state.tasks.close()
