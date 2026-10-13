"""Content-addressed storage for verified original distribution files."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import BinaryIO


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._db() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS source_index (source_key TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / "index.sqlite3", timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def lookup(self, source_key: str) -> tuple[str, Path] | None:
        with self._db() as connection:
            row = connection.execute("SELECT sha256 FROM source_index WHERE source_key=?", (source_key,)).fetchone()
            if row is None:
                return None
            valid = self.verify(row[0])
            if valid is None:
                connection.execute("DELETE FROM source_index WHERE source_key=?", (source_key,))
                return None
            return row[0], valid

    def record(self, source_key: str, digest: str) -> None:
        if self.verify(digest) is None:
            raise ValueError("Cannot index an unverified artifact")
        with self._db() as connection:
            connection.execute("INSERT OR REPLACE INTO source_index(source_key,sha256) VALUES (?,?)", (source_key, digest))

    def path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid SHA-256 digest")
        return self.root / "sha256" / digest[:2] / digest

    def verify(self, digest: str) -> Path | None:
        path = self.path(digest)
        if not path.is_file():
            return None
        if self.hash_file(path) != digest:
            path.unlink(missing_ok=True)
            return None
        return path

    @staticmethod
    def hash_file(path: Path) -> str:
        from .adapters import check_cancelled
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                check_cancelled()
                digest.update(block)
        return digest.hexdigest()

    def put_file(self, source: Path, expected_sha256: str | None = None) -> tuple[str, Path]:
        with Path(source).open("rb") as stream:
            return self.put_stream(stream, expected_sha256)

    def put_stream(self, stream: BinaryIO, expected_sha256: str | None = None) -> tuple[str, Path]:
        from .adapters import check_cancelled
        staging_dir = self.root / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="artifact-", dir=staging_dir)
        stage = Path(name)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as target:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    check_cancelled()
                    digest.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            actual = digest.hexdigest()
            if expected_sha256 and actual != expected_sha256:
                raise ValueError("Artifact SHA-256 mismatch")
            destination = self.path(actual)
            destination.parent.mkdir(parents=True, exist_ok=True)
            existing = self.verify(actual)
            if existing:
                return actual, existing
            os.replace(stage, destination)
            return actual, destination
        finally:
            stage.unlink(missing_ok=True)
