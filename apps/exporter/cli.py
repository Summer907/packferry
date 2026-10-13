"""PackFerry outside-network command line entry point."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from packages.common.models import Environment
from .runtime import prepare_runtime, python_executable
from .services import ExportRequest, ExportService


def _data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PackFerry"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "packferry"


def _webview2_version() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}"),
    )
    for hive, key in locations:
        try:
            with winreg.OpenKey(hive, key) as handle:
                version = str(winreg.QueryValueEx(handle, "pv")[0])
                if version and version != "0.0.0.0":
                    return version
        except OSError:
            continue
    return None


def doctor_report(data_dir: Path | None = None) -> dict:
    from .adapters import _run

    expected = {"uv": "0.11.29", "pip": "25.1.1", "node": "26.4.0", "npm": "11.17.0"}
    tools: dict[str, str | None] = {name: shutil.which(name) for name in ("uv", "node", "npm")}
    try:
        tools["python"] = python_executable()
    except RuntimeError:
        tools["python"] = None
    versions: dict[str, str | None] = {}
    commands = {
        "uv": ["uv", "--version"],
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "pip": [tools["python"] or "", "-c", "import pip; print(pip.__version__)"],
        "python": [tools["python"] or "", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
    }
    for name, command in commands.items():
        if not tools.get("python" if name in ("pip", "python") else name):
            versions[name] = None
            continue
        try:
            output = _run(command, timeout=10).strip()
            match = re.search(r"\d+\.\d+(?:\.\d+)?", output)
            versions[name] = match.group(0) if match else None
        except (OSError, RuntimeError):
            versions[name] = None
    checks = {name: versions.get(name) == version for name, version in expected.items()}
    checks["python"] = (versions.get("python") or '').startswith("3.12.")
    target = data_dir or _data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=target) as probe:
            probe.write(b'ok')
        checks["cache_writable"] = True
    except OSError:
        checks["cache_writable"] = False
    checks["pywebview"] = importlib.util.find_spec("webview") is not None
    desktop = {"renderer": "edgechromium" if sys.platform == "win32" else "qt"}
    if sys.platform == "win32":
        desktop["webview2_version"] = _webview2_version()
        checks["webview2"] = desktop["webview2_version"] is not None
    elif sys.platform.startswith("linux"):
        checks["pyside6"] = importlib.util.find_spec("PySide6") is not None
    else:
        checks["platform"] = False
    return {"tools": tools, "versions": versions, "expected": expected, "checks": checks, "desktop": desktop, "ready": all(checks.values())}


def main(argv: list[str] | None = None) -> int:
    prepare_runtime()
    parser = argparse.ArgumentParser(prog="packferry-exporter")
    commands = parser.add_subparsers(dest="command")
    export = commands.add_parser("export", help="Resolve and export a single-ecosystem Bundle")
    export.add_argument("--ecosystem", choices=("pypi", "npm"), required=True)
    export.add_argument("--package", action="append", default=[])
    export.add_argument("--project", type=Path)
    export.add_argument("--environment", action="append", default=[], help="id:runtime:version:platform:abi[:libc]")
    export.add_argument("--production-only", action="store_true")
    export.add_argument("--reuse", action="store_true", help="Reuse a verified historical resolution for identical input and tools")
    export.add_argument("--ignore-lock", action="store_true")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--data-dir", type=Path, default=_data_dir())
    export.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor", help="Check embedded package tools")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--data-dir", type=Path, default=_data_dir())
    web = commands.add_parser("web", help="Run the local browser interface")
    web.add_argument("--data-dir", type=Path, default=_data_dir())
    commands.add_parser("desktop", help="Run the pywebview shell")
    args = parser.parse_args(argv)
    if args.command is None:
        from .desktop import main as desktop_main
        desktop_main()
        return 0
    if args.command == "doctor":
        report = doctor_report(args.data_dir)
        print(json.dumps(report, ensure_ascii=False) if args.json else "\n".join(f"{name}: {'OK' if ready else 'MISSING/MISMATCH'}" for name, ready in report["checks"].items()))
        return 0 if report["ready"] else 3
    if args.command == "web":
        from .desktop import run_web
        run_web(args.data_dir)
        return 0
    if args.command == "desktop":
        from .desktop import main as desktop_main
        return desktop_main()
    try:
        environments = []
        for value in args.environment:
            parts = value.split(":")
            if len(parts) not in (5, 6):
                raise ValueError("Environment format is id:runtime:version:platform:abi[:libc]")
            environments.append(Environment(environment_id=parts[0], runtime=parts[1], version=parts[2], platform=parts[3], abi=parts[4], libc=parts[5] if len(parts) == 6 else None))
        request = ExportRequest(ecosystem=args.ecosystem, constraints=args.package, project_path=args.project, environments=environments, production_only=args.production_only, resolution_policy="reuse" if args.reuse else "fresh", lock_policy="ignore" if args.ignore_lock else "prefer")
        manifest = ExportService(args.data_dir).export(request, args.output)
        result = {"bundle": str(args.output.resolve()), "bundle_id": manifest.bundle_id, "resolution_status": manifest.verification.resolution_status, "collection_status": manifest.verification.collection_status, "warnings": [warning.model_dump(mode="json") for warning in manifest.warnings]}
        print(json.dumps(result, ensure_ascii=False) if args.json else f"{result['bundle']} — {result['resolution_status']}/{result['collection_status']}")
        return 0 if result["resolution_status"] == result["collection_status"] == "COMPLETE" else 2
    except (OSError, ValueError, RuntimeError) as exc:
        # Pydantic and third-party errors may echo the full input, including URL secrets.
        from pydantic import ValidationError
        message = 'Invalid export input or unsupported source' if isinstance(exc, ValidationError) else str(exc)
        message = re.sub(r'https?://[^\s\"\'<>]+', '[source URL omitted]', message)
        if args.json:
            print(json.dumps({"error": {"code": "EXPORT_FAILED", "message": message, "retryable": False}}, ensure_ascii=False))
        else:
            print(f"Export failed: {message}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
