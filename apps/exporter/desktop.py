"""Thin native shell; all package work stays in the authenticated local API."""
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import httpx
import uvicorn

from .api import create_app
from .runtime import prepare_runtime


class LocalServer:
    def __init__(self, data_dir=None):
        self.token = secrets.token_urlsafe(32)
        self.app = create_app(data_dir=data_dir, token=self.token)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=self.port,
                                                    access_log=False, log_level='warning'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.socket]}, daemon=True)

    def start(self):
        self.thread.start()
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if client.get(self.url + '/health/ready').status_code == 200:
                        return self.url + '/#token=' + self.token
                except httpx.HTTPError:
                    pass
                if not self.thread.is_alive():
                    break
                time.sleep(0.1)
        self.stop()
        raise RuntimeError('PackFerry local server did not become ready')

    def stop(self):
        self.app.state.tasks.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.socket.close()


class NativeApi:
    def __init__(self, server):
        # pywebview recursively exposes public object attributes; keep all state private.
        self._server = server
        self._window = None
        self._last_output = {}

    def select_project(self):
        import webview
        result = self._window.create_file_dialog(webview.FileDialog.OPEN,
            file_types=('Project files (*.txt;*.toml;*.json)',), allow_multiple=False)
        return str(result[0]) if result else None

    def save_bundle(self, task_id):
        import webview
        task = self._server.app.state.tasks.get(task_id)
        if task['status'] != 'COMPLETED':
            raise ValueError('Bundle is not ready')
        source = self._server.app.state.tasks.root / 'bundles' / f'{task_id}.zip'
        result = self._window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename=f'packferry-{task_id}.zip', file_types=('ZIP Bundle (*.zip)',))
        if not result:
            return None
        destination = Path(result[0] if isinstance(result, (list, tuple)) else result)
        if destination.resolve() != source.resolve():
            shutil.copyfile(source, destination)
        self._last_output[task_id] = destination.resolve()
        return str(destination)

    def open_output(self, task_id):
        # No arbitrary path execution capability is exposed to JavaScript.
        destination = self._last_output.get(task_id)
        if destination is None:
            return False
        if sys.platform == 'win32':
            os.startfile(destination.parent)
        else:
            subprocess.Popen(['xdg-open', str(destination.parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True


def run_web(data_dir=None):
    server = LocalServer(data_dir)
    try:
        url = server.start()
        webbrowser.open(url)
        while server.thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


def main():
    prepare_runtime()
    try:
        import webview
    except ImportError:
        raise SystemExit('Install desktop support: uv sync --extra desktop (Windows requires WebView2)')
    renderer = 'edgechromium' if sys.platform == 'win32' else 'qt'
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    server = LocalServer()
    try:
        url = server.start()
        bridge = NativeApi(server)
        window = webview.create_window('PackFerry — 离线包准备', url, js_api=bridge,
                                       width=1200, height=840, min_size=(900, 640))
        bridge._window = window

        def closing():
            if server.app.state.tasks.running():
                return window.create_confirmation_dialog('任务正在运行', '退出会中断当前任务。确认退出？下次可重新执行并复用缓存。')
            return True

        window.events.closing += closing
        # pywebview opens links with target=_blank in the system browser.
        webview.start(gui=renderer, debug=False)
    except Exception as exc:
        raise SystemExit(f'Desktop startup failed ({type(exc).__name__}); check WebView2/Qt with packferry doctor') from None
    finally:
        server.stop()


if __name__ == '__main__':
    main()
