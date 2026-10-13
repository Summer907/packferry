"""Lifecycle tests; native WebView smoke runs only when explicitly enabled."""
import os
import sys

import httpx
import pytest

from apps.exporter.desktop import LocalServer, NativeApi


def test_real_loopback_lifecycle(tmp_path):
    server = LocalServer(tmp_path)
    try:
        launch_url = server.start()
        assert launch_url.startswith('http://127.0.0.1:')
        assert '#token=' in launch_url
        with httpx.Client(trust_env=False) as client:
            assert client.get(server.url + '/health/ready').status_code == 200
            assert client.get(server.url + '/api/v1/exports/x').status_code == 401
            response = client.post(server.url + '/api/v1/local/session', json={'token': server.token})
            assert response.status_code == 200
            assert client.get(server.url + '/api/v1/exports/x').status_code == 404
    finally:
        server.stop()
    assert not server.thread.is_alive()


def test_bridge_has_no_public_state(tmp_path):
    server = LocalServer(tmp_path)
    try:
        api = NativeApi(server)
        public = {name for name in dir(api) if not name.startswith('_')}
        assert public == {'select_project', 'save_bundle', 'open_output'}
        assert api.open_output('unknown') is False
    finally:
        server.app.state.tasks.close()
        server.socket.close()


@pytest.mark.skipif(os.environ.get('PACKFERRY_NATIVE_SMOKE') != '1', reason='Requires a desktop session and native web engine')
def test_native_engine_loads_local_vue(tmp_path):
    import webview
    from apps.exporter.api import static_dir
    assert (static_dir('exporter') / 'index.html').is_file(), 'Build Vue first'
    server = LocalServer(tmp_path)
    loaded = []
    errors = []
    try:
        window = webview.create_window('PackFerry test', server.start(), hidden=True)

        def check():
            try:
                value = window.evaluate_js("document.querySelector('#app').innerText")
                loaded.append(value)
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                window.destroy()

        window.events.loaded += check
        webview.start(gui='edgechromium' if sys.platform == 'win32' else 'qt')
    finally:
        server.stop()
    assert not errors
    assert loaded and 'PackFerry' in loaded[0]
