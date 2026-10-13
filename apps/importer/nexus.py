from __future__ import annotations

import base64
import hashlib
import html.parser
import json
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
from packages.common.archives import bounded_tar_info


class NexusFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class RemoteResult:
    state: str  # ABSENT, SAME, CONFLICT, UNKNOWN
    reason: str = ""


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)


def _identity(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("userId", "userid", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("data", "result", "user"):
        found = _identity(payload.get(key))
        if found:
            return found
    return None


class NexusClient:
    """Nexus 3.96.4 CE adapter; credentials are only held by caller memory."""

    def __init__(self, base_url: str, username: str, password: str, *, verify_tls: bool = True, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = httpx.Client(auth=(username, password), verify=verify_tls, timeout=timeout, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def authenticate(self) -> bool:
        try:
            response = self.client.post(
                f"{self.base_url}/service/extdirect",
                json={"action": "rapture_Security", "method": "getUser", "data": [], "type": "rpc", "tid": 1},
            )
        except httpx.HTTPError as exc:
            raise NexusFailure("NEXUS_UNAVAILABLE", "Nexus 身份服务不可达", retryable=True) from exc
        if response.status_code in (401, 403):
            return False
        if response.status_code != 200:
            raise NexusFailure("NEXUS_AUTH_ERROR", f"Nexus 身份服务返回 {response.status_code}", retryable=response.status_code >= 500)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NexusFailure("NEXUS_AUTH_ERROR", "Nexus 身份响应无效") from exc
        identity = _identity(payload)
        return bool(identity and identity.lower() not in {'anonymous', 'anonymoususer'} and identity == self.username)

    def _get(self, url: str) -> httpx.Response:
        try:
            response = self.client.get(url)
        except httpx.HTTPError as exc:
            raise NexusFailure("NEXUS_QUERY_FAILED", "Nexus 查询失败", retryable=True) from exc
        if response.status_code in (401, 403):
            raise NexusFailure("NEXUS_AUTH_EXPIRED", "Nexus 授权失效")
        if response.status_code in (429, 503):
            raise NexusFailure("NEXUS_BUSY", "Nexus 暂时不可用", retryable=True)
        if response.status_code >= 500:
            raise NexusFailure("NEXUS_QUERY_FAILED", f"Nexus 查询返回 {response.status_code}", retryable=True)
        return response

    def _trusted_artifact_url(self, url: str, repository: str) -> bool:
        base, target = urlparse(self.base_url), urlparse(url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc) or target.username or target.password or target.query:
            return False
        raw_path = target.path
        decoded_path = unquote(raw_path)
        if unquote(decoded_path) != decoded_path or "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
            return False
        prefix = f"{base.path.rstrip('/')}/repository/{repository}/"
        return decoded_path.startswith(prefix)

    def check(self, repository: str, ecosystem: str, artifact: dict, package: dict) -> RemoteResult:
        """Read actual remote bytes; any uncertain result stays UNKNOWN."""
        expected = artifact["sha256"]
        if ecosystem == "pypi":
            normalized = re.sub(r"[-_.]+", "-", package["name"]).lower()
            index_url = f"{self.base_url}/repository/{quote(repository)}/simple/{quote(normalized)}/"
            index = self._get(index_url)
            if index.status_code == 404:
                return RemoteResult("ABSENT")
            if index.status_code != 200:
                return RemoteResult("UNKNOWN", f"Simple API 返回 {index.status_code}")
            links = _Links()
            links.feed(index.text)
            matches = []
            for href in links.hrefs:
                url = urljoin(index_url, href)
                if unquote(urlparse(url).path.rsplit("/", 1)[-1]) == artifact["filename"]:
                    if not self._trusted_artifact_url(url, repository):
                        return RemoteResult("UNKNOWN", "Simple API 制品 URL 越界")
                    matches.append(url.split("#", 1)[0])
            if not matches:
                return RemoteResult("ABSENT")
            if len(matches) != 1:
                return RemoteResult("UNKNOWN", "Simple API 同名文件不唯一")
            url = matches[0]
        elif ecosystem == "npm":
            name = package["name"]
            metadata_url = f"{self.base_url}/repository/{quote(repository)}/{quote(name, safe='')}"
            metadata = self._get(metadata_url)
            if metadata.status_code == 404:
                return RemoteResult("ABSENT")
            if metadata.status_code != 200:
                return RemoteResult("UNKNOWN", f"npm Registry 返回 {metadata.status_code}")
            try:
                payload = metadata.json()
            except ValueError:
                return RemoteResult("UNKNOWN", "npm Registry 元数据不是 JSON")
            versions = payload.get("versions") if isinstance(payload, dict) else None
            if not isinstance(versions, dict):
                return RemoteResult("UNKNOWN", "npm Registry versions 无效")
            if package["version"] not in versions:
                return RemoteResult("ABSENT")
            version = versions[package["version"]]
            dist = version.get("dist") if isinstance(version, dict) else None
            url = dist.get("tarball") if isinstance(dist, dict) else None
            if not isinstance(url, str) or not self._trusted_artifact_url(url, repository):
                return RemoteResult("UNKNOWN", "npm tarball URL 越界")
        else:
            raise NexusFailure("BAD_ECOSYSTEM", "不支持的生态")

        try:
            with self.client.stream("GET", url) as response:
                if response.status_code in (401, 403):
                    raise NexusFailure("NEXUS_AUTH_EXPIRED", "Nexus 授权失效")
                if response.status_code != 200:
                    return RemoteResult("UNKNOWN", f"制品读回返回 {response.status_code}")
                digest = hashlib.sha256()
                size = 0
                for block in response.iter_bytes():
                    size += len(block)
                    if size > artifact["size"]:
                        return RemoteResult("CONFLICT", "远端文件长度不同")
                    digest.update(block)
            return RemoteResult("SAME" if size == artifact["size"] and digest.hexdigest() == expected else "CONFLICT")
        except httpx.HTTPError:
            return RemoteResult("UNKNOWN", "制品读回失败")

    def publish(self, repository: str, ecosystem: str, artifact: dict, package: dict, file_path: Path) -> None:
        if ecosystem == "pypi":
            env = os.environ.copy()
            env.update({"TWINE_USERNAME": self.username, "TWINE_PASSWORD": self.password, "TWINE_NON_INTERACTIVE": "1"})
            url = f"{self.base_url}/repository/{quote(repository)}/"
            try:
                done = subprocess.run(
                    [sys.executable, "-m", "twine", "upload", "--non-interactive", "--disable-progress-bar", "--repository-url", url, str(file_path)],
                    env=env, capture_output=True, timeout=600, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise NexusFailure("PUBLISH_UNKNOWN", "PyPI 发布结果不明", retryable=False) from exc
            if done.returncode:
                output = (done.stdout + done.stderr).decode(errors="replace")
                if "401" in output or "403" in output:
                    raise NexusFailure("NEXUS_AUTH_EXPIRED", "Nexus 授权失效")
                # A failed upload can still have reached Nexus. Caller must reconcile.
                raise NexusFailure("PUBLISH_UNKNOWN", "PyPI 发布结果不明")
            return
        if ecosystem != "npm":
            raise NexusFailure("BAD_ECOSYSTEM", "不支持的生态")
        try:
            with tarfile.open(file_path, mode="r|gz", tarinfo=bounded_tar_info(1024 * 1024, 100_000)) as tf:
                metadata = None
                for index, entry in enumerate(tf):
                    if index > 100_000:
                        raise ValueError("too many npm tar entries")
                    if entry.name != "package/package.json":
                        continue
                    if metadata is not None or not entry.isfile() or entry.size > 1024 * 1024:
                        raise ValueError("invalid package.json")
                    metadata_file = tf.extractfile(entry)
                    if metadata_file is None:
                        raise ValueError("missing package.json")
                    metadata = json.load(metadata_file)
                if metadata is None:
                    raise ValueError("missing package.json")
        except (tarfile.TarError, OSError, ValueError, KeyError) as exc:
            raise NexusFailure("BAD_NPM_TARBALL", "npm tarball 元数据无效") from exc
        if metadata.get("name") != package["name"] or metadata.get("version") != package["version"]:
            raise NexusFailure("BAD_NPM_IDENTITY", "npm tarball 与 Manifest 身份不一致")
        name = package["name"]
        version = package["version"]
        tarball_name = artifact["filename"]
        sha1, sha512 = hashlib.sha1(), hashlib.sha512()
        with file_path.open("rb") as source:
            while block := source.read(1024 * 1024):
                sha1.update(block)
                sha512.update(block)
        digest = sha1.hexdigest()
        integrity = "sha512-" + base64.b64encode(sha512.digest()).decode()
        version_data = dict(metadata)
        version_data["dist"] = {"shasum": digest, "integrity": integrity, "tarball": f"{self.base_url}/repository/{quote(repository)}/{quote(name, safe='')}/-/{quote(tarball_name)}"}
        prefix = (json.dumps({"_id": name, "name": name, "dist-tags": {"latest": version}, "versions": {version: version_data}}, separators=(",", ":"))[:-1]
                  + ',"_attachments":{' + json.dumps(tarball_name) + ':{"content_type":"application/octet-stream","data":"').encode()
        size = file_path.stat().st_size
        suffix = f'","length":{size}}}}}}}'.encode()

        def content():
            yield prefix
            with file_path.open("rb") as source:
                while block := source.read(3 * 256 * 1024):
                    yield base64.b64encode(block)
            yield suffix

        encoded_size = ((size + 2) // 3) * 4
        try:
            response = self.client.put(
                f"{self.base_url}/repository/{quote(repository)}/{quote(name, safe='')}",
                content=content(),
                headers={"Content-Type": "application/json", "Content-Length": str(len(prefix) + encoded_size + len(suffix))},
                timeout=600,
            )
        except httpx.HTTPError as exc:
            raise NexusFailure("PUBLISH_UNKNOWN", "npm 发布结果不明") from exc
        if response.status_code in (401, 403):
            raise NexusFailure("NEXUS_AUTH_EXPIRED", "Nexus 授权失效")
        if response.status_code not in (200, 201, 202):
            raise NexusFailure("PUBLISH_UNKNOWN", f"npm 发布返回 {response.status_code}")
