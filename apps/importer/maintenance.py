"""Consistent SQLite plus retained Bundle backup for offline operations."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

import yaml

from .storage_lock import storage_lock


def backup(data_dir: Path, output: Path, config_path: Path | None = None) -> Path:
    data_dir, output = Path(data_dir), Path(output)
    database = data_dir / "importer.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(database)
    if output.resolve().is_relative_to((data_dir / "bundles").resolve()):
        raise ValueError("Backup output cannot be inside the Bundle directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with storage_lock(data_dir), tempfile.TemporaryDirectory(prefix="packferry-backup-") as temporary:
        snapshot = Path(temporary) / "importer.sqlite3"
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        fd, staged_name = tempfile.mkstemp(prefix=".packferry-backup-", suffix=".zip", dir=output.parent)
        os.close(fd)
        staged = Path(staged_name)
        try:
            with ZipFile(staged, "w", compression=ZIP_STORED, allowZip64=True) as archive:
                archive.write(snapshot, "importer.sqlite3")
                bundles = sorted((data_dir / "bundles").glob("*.zip"))
                for bundle in bundles:
                    archive.write(bundle, f"bundles/{bundle.name}")
                if config_path:
                    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
                    if not isinstance(raw, dict):
                        raise ValueError("Config must be a YAML mapping")
                    safe_keys = {"data_dir", "nexus_url", "repositories", "maintainers", "max_bundle_bytes", "bundle_retention_days"}
                    safe_config = {key: raw[key] for key in safe_keys if key in raw}
                    if "nexus_url" in safe_config:
                        url = urlsplit(str(safe_config["nexus_url"]))
                        if url.username or url.password or url.query or url.fragment:
                            safe_config.pop("nexus_url")
                    archive.writestr("config.yaml", yaml.safe_dump(safe_config, allow_unicode=True))
                archive.writestr("backup.json", json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "bundle_count": len(bundles)}))
            os.replace(staged, output)
        finally:
            staged.unlink(missing_ok=True)
    return output


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog="python -m apps.importer.maintenance")
    parser.add_argument("backup", choices=["backup"])
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("PACKFERRY_DATA_DIR", "/var/lib/packferry")))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    print(backup(args.data_dir, args.output, args.config))


if __name__ == "__main__":
    main()
