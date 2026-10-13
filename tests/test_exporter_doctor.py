from pathlib import Path

from apps.exporter.cli import doctor_report


def test_doctor_checks_actual_python_runtime_and_pinned_versions(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("apps.exporter.cli.python_executable", lambda: str(tmp_path / "runtime" / "python.exe"))
    monkeypatch.setattr("apps.exporter.cli.shutil.which", lambda name: str(tmp_path / name))
    monkeypatch.setattr("apps.exporter.cli._webview2_version", lambda: "154.0.0.0")

    def fake_run(command, timeout=10):
        joined = " ".join(command)
        if command[0].endswith("python.exe") and "pip.__version__" in joined:
            return "25.1.1\n"
        if command[0].endswith("python.exe"):
            return "3.12.12\n"
        return {"uv": "uv 0.11.29\n", "node": "v26.4.0\n", "npm": "11.17.0\n"}[command[0]]

    monkeypatch.setattr("apps.exporter.adapters._run", fake_run)
    report = doctor_report(tmp_path / "cache")
    assert report["versions"]["python"] == "3.12.12"
    assert report["versions"]["pip"] == "25.1.1"
    assert all(report["checks"][name] for name in ("uv", "pip", "node", "npm", "python", "cache_writable"))
