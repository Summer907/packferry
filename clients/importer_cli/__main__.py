from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx


EXIT_SUCCESS = 0
EXIT_PARTIAL = 2
EXIT_AUTH = 3
EXIT_FORMAT = 4
EXIT_NETWORK = 5
EXIT_TIMEOUT = 6
EXIT_INTERNAL = 7


def _request(client: httpx.Client, method: str, path: str, *, token: str | None = None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"网络错误: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"服务响应不是 JSON: HTTP {response.status_code}")
    if not isinstance(payload, dict):
        raise RuntimeError('服务响应不是 JSON 对象')
    if response.status_code >= 400:
        error = payload.get("error", {})
        raise ValueError(f"{error.get('code', 'HTTP_ERROR')}: {error.get('message', response.status_code)}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packferry-import")
    parser.add_argument("--url", default=os.environ.get("PACKFERRY_IMPORTER_URL"), help="Importer HTTPS base URL")
    parser.add_argument("--token", default=os.environ.get("PACKFERRY_TOKEN"), help="Short-lived API token (or PACKFERRY_TOKEN)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--allow-http", action="store_true", help="Local test only")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login")
    login.add_argument("username")
    login.add_argument("--password-stdin", action="store_true")
    submit = sub.add_parser("submit")
    submit.add_argument("bundle", type=Path)
    submit.add_argument("--repository", required=True)
    submit.add_argument("--idempotency-key")
    submit.add_argument("--confirm", action="store_true", help="Confirm the previewed Bundle for import")
    submit.add_argument("--wait", action="store_true")
    preview = sub.add_parser("preview")
    preview.add_argument("task_id")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("task_id")
    confirm.add_argument("--idempotency-key")
    authorize = sub.add_parser("authorize")
    authorize.add_argument("task_id")
    authorize.add_argument("--password-stdin", action="store_true")
    operation = sub.add_parser("operation")
    operation.add_argument("key")
    status = sub.add_parser("status")
    status.add_argument("task_id")
    wait = sub.add_parser("wait")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, default=3600)
    report = sub.add_parser("report")
    report.add_argument("task_id")
    args = parser.parse_args(argv)
    address = urlsplit(args.url or '')
    if not address.hostname or address.username or address.password or address.query or address.fragment:
        parser.error('--url must be a service URL without credentials, query or fragment')
    if address.scheme not in ({'http', 'https'} if args.allow_http else {'https'}):
        parser.error("--url must be HTTPS (use --allow-http only for local tests)")
    if args.command != "login" and not args.token:
        parser.error("--token or PACKFERRY_TOKEN is required")
    operation_key = None

    def error_output(code, message, retryable=False):
        error = {'error': {'code': code, 'message': message, 'retryable': retryable}}
        if operation_key:
            error['idempotency_key'] = operation_key
            error['operation_query'] = f'/api/v1/operations/{operation_key}'
        if args.json:
            print(json.dumps(error, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
            if operation_key:
                print(f'核实提交结果的幂等键: {operation_key}', file=sys.stderr)
    try:
        with httpx.Client(base_url=args.url.rstrip("/") + "/api/v1/", timeout=30, follow_redirects=False) as client:
            if args.command == "login":
                password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Nexus password: ")
                payload = _request(client, "POST", "auth/login", json={"username": args.username, "password": password})
                if args.json:
                    print(json.dumps({"username": payload["username"], "token": payload["token"]}, ensure_ascii=False))
                else:
                    print(f"PACKFERRY_TOKEN={payload['token']}")
                return EXIT_SUCCESS
            if args.command == "submit":
                key = args.idempotency_key or str(uuid.uuid4())
                operation_key = key
                if args.wait and not args.confirm:
                    raise ValueError('--wait 需要同时指定 --confirm；尚未提交 Bundle')
                path = args.bundle
                if not path.is_file():
                    raise ValueError("Bundle 文件不存在")
                with path.open("rb") as fh:
                    payload = _request(client, "POST", "imports", token=args.token, files={"bundle": (path.name, fh, "application/zip")}, data={"repository": args.repository}, headers={"Idempotency-Key": key}, timeout=httpx.Timeout(300, connect=10))
                task_id = payload["id"]
                if not args.confirm:
                    print(json.dumps({"id": task_id, "status": payload["status"], "idempotency_key": key}, ensure_ascii=False) if args.json else f"{task_id} READY (idempotency key: {key})")
                    return EXIT_SUCCESS
                _request(client, "POST", f"imports/{task_id}/confirm", token=args.token, headers={"Idempotency-Key": str(uuid.uuid4())})
                if not args.wait:
                    print(json.dumps({"id": task_id, "status": "QUEUED"}, ensure_ascii=False) if args.json else f"{task_id}: QUEUED")
                    return EXIT_SUCCESS
                args.command = "wait"
                args.task_id = task_id
                args.timeout = 3600
            if args.command == "preview":
                payload = _request(client, "GET", f"imports/{args.task_id}/preview", token=args.token)
            elif args.command == "confirm":
                payload = _request(client, "POST", f"imports/{args.task_id}/confirm", token=args.token, headers={"Idempotency-Key": args.idempotency_key or str(uuid.uuid4())})
            elif args.command == "authorize":
                password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Nexus password: ")
                payload = _request(client, "POST", f"imports/{args.task_id}/authorize", token=args.token, json={"password": password})
            elif args.command == "operation":
                payload = _request(client, "GET", f"operations/{args.key}", token=args.token)
            elif args.command == "status":
                payload = _request(client, "GET", f"imports/{args.task_id}", token=args.token)
            elif args.command == "report":
                payload = _request(client, "GET", f"imports/{args.task_id}/report", token=args.token)
            else:
                deadline = time.monotonic() + args.timeout
                while True:
                    payload = _request(client, "GET", f"imports/{args.task_id}", token=args.token)
                    if payload["status"] in {"COMPLETED", "PARTIAL", "FAILED", "EXPIRED", "NEEDS_AUTH", "INTERRUPTED"}:
                        break
                    if time.monotonic() >= deadline:
                        error_output('TIMEOUT', '等待任务超时；可继续查询原任务', True)
                        return EXIT_TIMEOUT
                    time.sleep(2)
            print(json.dumps(payload, ensure_ascii=False) if args.json else f"{payload.get('id', payload.get('task', {}).get('id', 'preview'))}: {payload.get('status', payload.get('task', {}).get('status', 'READY'))}")
            if args.command in {"preview", "confirm", "authorize", "operation"}:
                return EXIT_SUCCESS
            if payload["status"] == "COMPLETED":
                return EXIT_SUCCESS
            if payload["status"] in {"PARTIAL", "NEEDS_AUTH", "INTERRUPTED"}:
                return EXIT_PARTIAL
            return EXIT_INTERNAL if payload["status"] == "FAILED" else EXIT_SUCCESS
    except ValueError as exc:
        error_output('API_OR_INPUT_ERROR', str(exc))
        return EXIT_AUTH if any(x in str(exc) for x in ("SESSION", "CREDENTIAL", "AUTH")) else EXIT_FORMAT
    except RuntimeError as exc:
        error_output('NETWORK_OR_RESPONSE_ERROR', str(exc), True)
        return EXIT_NETWORK
    except OSError:
        error_output('LOCAL_FILE_ERROR', '无法读取本地 Bundle')
        return EXIT_FORMAT


if __name__ == "__main__":
    raise SystemExit(main())
