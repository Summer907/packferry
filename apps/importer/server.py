"""Uvicorn entry point: ``uvicorn apps.importer.server:app``."""
import json
import os
from pathlib import Path

import yaml

from .app import Settings, create_app


def _config() -> dict:
    path = os.environ.get("PACKFERRY_CONFIG")
    if not path:
        return {}
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(result, dict):
        raise ValueError("PACKFERRY_CONFIG must contain a YAML mapping")
    return result


def _repositories(config: dict) -> dict[str, str]:
    value = os.environ.get("PACKFERRY_REPOSITORIES_JSON")
    result = json.loads(value) if value is not None else config.get("repositories", {})
    if not isinstance(result, dict) or not all(isinstance(k, str) and v in {"pypi", "npm"} for k, v in result.items()):
        raise ValueError("PACKFERRY_REPOSITORIES_JSON must map repository names to pypi/npm")
    return result


def load_settings() -> Settings:
    config = _config()
    maintainers = os.environ.get("PACKFERRY_MAINTAINERS")
    return Settings(
        data_dir=Path(os.environ.get("PACKFERRY_DATA_DIR", config.get("data_dir", "/var/lib/packferry"))),
        nexus_url=os.environ.get("PACKFERRY_NEXUS_URL", config.get("nexus_url", "")),
        repositories=_repositories(config),
        maintainers={v.strip() for v in maintainers.split(",") if v.strip()} if maintainers is not None else set(config.get("maintainers", [])),
        max_bundle_bytes=int(os.environ.get("PACKFERRY_MAX_BUNDLE_BYTES", config.get("max_bundle_bytes", 5 * 1024**3))),
        bundle_retention_days=int(os.environ.get("PACKFERRY_BUNDLE_RETENTION_DAYS", config.get("bundle_retention_days", 30))),
        allow_insecure_http=os.environ.get("PACKFERRY_ALLOW_INSECURE_HTTP") == "1",
        verify_nexus_tls=os.environ.get("PACKFERRY_VERIFY_NEXUS_TLS", "1") == "1",
    )


app = create_app(load_settings())
