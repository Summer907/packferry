"""Bounded, read-only Bundle validation. Never extract untrusted archives."""
from dataclasses import dataclass
from email.parser import BytesParser
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import tarfile
import tempfile
from urllib.parse import unquote
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile

from packaging.utils import canonicalize_name, parse_wheel_filename, parse_sdist_filename
from packaging.version import Version
from pydantic import ValidationError

from .hashes import file_sha256
from .models import Manifest
from .archives import bounded_tar_info


class BundleError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_BUNDLE"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BundleLimits:
    max_zip_size: int = 5 * 1024**3
    max_entries: int = 100_000
    max_file_size: int = 2 * 1024**3
    max_total_size: int = 10 * 1024**3
    max_ratio: int = 200
    max_metadata_size: int = 16 * 1024**2
    max_inner_entries: int = 100_000


def _json(data: bytes):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise BundleError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise BundleError("Invalid JSON") from exc


def _safe_path(name: str):
    decoded = name
    # Recursive encoding tricks are rejected rather than interpreted.
    for _ in range(3):
        decoded = unquote(decoded)
    if decoded != name or '\\' in name or '\x00' in name or ':' in name:
        raise BundleError("Encoded or invalid archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or not name or any(p in {"", ".", ".."} for p in name.split('/')):
        raise BundleError("Unsafe archive path")


def _entries(archive: ZipFile, limits: BundleLimits):
    entries = archive.infolist()
    if len(entries) > limits.max_entries:
        raise BundleError("Too many archive entries", "LIMIT_EXCEEDED")
    seen = set()
    total = 0
    for item in entries:
        _safe_path(item.filename)
        key = item.filename.casefold()
        if key in seen:
            raise BundleError("Duplicate or case-conflicting archive entries")
        seen.add(key)
        mode = item.external_attr >> 16
        if item.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or item.flag_bits & 1:
            raise BundleError("Only unencrypted regular files are permitted")
        total += item.file_size
        if item.file_size > limits.max_file_size or total > limits.max_total_size:
            raise BundleError("Archive size limit exceeded", "LIMIT_EXCEEDED")
        if item.file_size / max(item.compress_size, 1) > limits.max_ratio:
            raise BundleError("Archive compression ratio exceeded", "LIMIT_EXCEEDED")
    return entries


def _preflight_zip(stream, max_entries: int):
    """Bound central-directory allocation before ZipFile constructs ZipInfo objects."""
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(max(0, length - 65557))
    tail = stream.read(65557)
    marker = tail.rfind(b'PK\x05\x06')
    if marker < 0 or marker + 22 > len(tail):
        raise BundleError('Missing ZIP end-of-central-directory')
    _, disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment = struct.unpack_from('<4s4H2IH', tail, marker)
    if marker + 22 + comment != len(tail) or disk or cd_disk:
        raise BundleError('Split or malformed ZIP is not supported')
    if entries == 65535 or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        eocd_offset = length - len(tail) + marker
        if eocd_offset < 20:
            raise BundleError('Invalid ZIP64 locator')
        stream.seek(eocd_offset - 20)
        locator = stream.read(20)
        signature, zip64_disk, offset, disks = struct.unpack('<4sIQI', locator)
        if signature != b'PK\x06\x07' or zip64_disk or disks != 1:
            raise BundleError('Invalid ZIP64 locator')
        stream.seek(offset)
        record = stream.read(56)
        if len(record) != 56:
            raise BundleError('Missing ZIP64 end record')
        signature, size, _, _, disk, cd_disk, disk_entries, entries, cd_size, cd_offset = struct.unpack('<4sQ2H2I4Q', record)
        if signature != b'PK\x06\x06' or size < 44 or disk or cd_disk:
            raise BundleError('Invalid ZIP64 end record')
    if entries != disk_entries or entries > max_entries or cd_size > max_entries * 1024:
        raise BundleError('ZIP central-directory limit exceeded', 'LIMIT_EXCEEDED')
    if cd_offset + cd_size > length:
        raise BundleError('ZIP central directory lies outside archive')
    stream.seek(0)


def _bounded_read(archive: ZipFile, name: str, limit: int):
    if archive.getinfo(name).file_size > limit:
        raise BundleError("Metadata size limit exceeded", "LIMIT_EXCEEDED")
    with archive.open(name) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BundleError("Metadata size limit exceeded", "LIMIT_EXCEEDED")
    return data


def summary(manifest: Manifest) -> dict:
    return {
        "bundle_id": manifest.bundle_id, "ecosystem": manifest.ecosystem,
        "package_count": len(manifest.packages), "artifact_count": len(manifest.artifacts),
        "environment_count": len(manifest.environments),
        "verification": manifest.verification.model_dump(),
        "warnings": [w.model_dump() for w in manifest.warnings],
    }


def _metadata_identity(data: bytes, artifact_type: str):
    metadata = BytesParser().parsebytes(data, headersonly=True)
    if metadata.get("X-PackFerry-Carrier") or metadata.get("X-Packferry-Bridge"):
        raise BundleError("Resolver metadata carrier cannot be delivered")
    if len(metadata.get_all('Name', [])) != 1 or len(metadata.get_all('Version', [])) != 1:
        raise BundleError(f"Missing {artifact_type} identity metadata")
    return metadata["Name"], metadata["Version"]


def _artifact_identity(fileobj, artifact, limits: BundleLimits):
    if artifact.type == "wheel":
        try:
            filename_name, filename_version, _, _ = parse_wheel_filename(artifact.filename)
        except Exception as exc:
            raise BundleError("Invalid Wheel filename") from exc
        _preflight_zip(fileobj, limits.max_inner_entries)
        with ZipFile(fileobj) as wheel:
            if len(wheel.infolist()) > limits.max_inner_entries:
                raise BundleError("Wheel entry limit exceeded")
            names = wheel.namelist()
            metadata = [n for n in names if n.endswith('.dist-info/METADATA')]
            if len(set(names)) != len(names) or len(metadata) != 1:
                raise BundleError("Ambiguous Wheel metadata")
            if any("packferry-carrier" in n.lower() or "packferry_bridge" in n.lower() for n in names):
                raise BundleError("Resolver metadata carrier cannot be delivered")
            wheel_files = [n for n in names if n.endswith('.dist-info/WHEEL')]
            for wheel_file in wheel_files:
                headers = BytesParser().parsebytes(_bounded_read(wheel, wheel_file, limits.max_metadata_size), headersonly=True)
                if 'packferry' in headers.get('Generator', '').lower() and any(
                    marker in headers.get('Generator', '').lower() for marker in ['carrier', 'bridge']
                ):
                    raise BundleError('Resolver metadata carrier cannot be delivered')
            name, version = _metadata_identity(_bounded_read(wheel, metadata[0], limits.max_metadata_size), "Wheel")
        if canonicalize_name(name) != filename_name or Version(version) != filename_version:
            raise BundleError("Wheel filename and metadata identity differ")
        return name, version
    if artifact.type == "sdist":
        try:
            filename_name, filename_version = parse_sdist_filename(artifact.filename)
        except Exception as exc:
            raise BundleError("Invalid sdist filename") from exc
        if artifact.filename.endswith('.zip'):
            _preflight_zip(fileobj, limits.max_inner_entries)
            with ZipFile(fileobj) as archive:
                if len(archive.infolist()) > limits.max_inner_entries:
                    raise BundleError("sdist entry limit exceeded")
                names = archive.namelist()
                candidates = [n for n in names if len(n.split('/')) == 2 and n.endswith('/PKG-INFO')]
                if len(set(names)) != len(names) or len(candidates) != 1:
                    raise BundleError("Missing or ambiguous sdist PKG-INFO")
                data = _bounded_read(archive, candidates[0], limits.max_metadata_size)
        else:
            data = None
            total = 0
            # Streaming tar prevents getmembers() from allocating unbounded metadata.
            with tarfile.open(fileobj=fileobj, mode='r|gz', tarinfo=bounded_tar_info(limits.max_metadata_size, limits.max_inner_entries)) as archive:
                count = 0
                for entry in archive:
                    count += 1
                    total += entry.size
                    if count > limits.max_inner_entries or total > limits.max_total_size:
                        raise BundleError("sdist expansion limit exceeded")
                    if len(entry.name.split('/')) == 2 and entry.name.endswith('/PKG-INFO'):
                        if data is not None or not entry.isfile() or entry.size > limits.max_metadata_size:
                            raise BundleError("Invalid sdist PKG-INFO")
                        stream = archive.extractfile(entry)
                        if stream is None:
                            raise BundleError("Missing PKG-INFO")
                        data = stream.read(limits.max_metadata_size + 1)
            if data is None:
                raise BundleError("Missing static sdist PKG-INFO")
        name, version = _metadata_identity(data, "sdist")
        if canonicalize_name(name) != filename_name or Version(version) != filename_version:
            raise BundleError("sdist filename and metadata identity differ")
        return name, version
    data = None
    total = 0
    with tarfile.open(fileobj=fileobj, mode='r|gz', tarinfo=bounded_tar_info(limits.max_metadata_size, limits.max_inner_entries)) as archive:
        count = 0
        for entry in archive:
            count += 1
            total += entry.size
            if count > limits.max_inner_entries or total > limits.max_total_size:
                raise BundleError("npm tarball expansion limit exceeded")
            if entry.name == 'package/package.json':
                if data is not None or not entry.isfile() or entry.size > limits.max_metadata_size:
                    raise BundleError("Invalid npm package metadata")
                stream = archive.extractfile(entry)
                if stream is None:
                    raise BundleError("Missing package.json")
                data = stream.read(limits.max_metadata_size + 1)
    if data is None:
        raise BundleError("Missing npm package/package.json")
    package = _json(data)
    if not isinstance(package, dict) or not isinstance(package.get("name"), str) or not isinstance(package.get("version"), str):
        raise BundleError("Invalid npm package identity")
    return package['name'], package['version']


def validate_bundle(path: Path, limits: BundleLimits | None = None) -> Manifest:
    limits = limits or BundleLimits()
    path = Path(path)
    try:
        if path.stat().st_size > limits.max_zip_size:
            raise BundleError("Bundle exceeds upload limit", "LIMIT_EXCEEDED")
        with path.open('rb') as stream:
            _preflight_zip(stream, limits.max_entries)
        with ZipFile(path) as archive:
            entries = _entries(archive, limits)
            manifest = Manifest.model_validate(_json(_bounded_read(archive, 'manifest.json', limits.max_metadata_size)))
            expected = {'manifest.json', 'reports/summary.json'} | {a.archive_path for a in manifest.artifacts}
            if {e.filename for e in entries} != expected:
                raise BundleError("Bundle contains missing or undeclared files")
            if _json(_bounded_read(archive, 'reports/summary.json', limits.max_metadata_size)) != summary(manifest):
                raise BundleError("Summary contradicts manifest")
            packages = {p.node_id: p for p in manifest.packages}
            for artifact in manifest.artifacts:
                if archive.getinfo(artifact.archive_path).file_size != artifact.size:
                    raise BundleError("Artifact size differs from manifest")
                # Seekable bounded temporary file allows inner Wheel validation without RAM allocation.
                with tempfile.TemporaryFile() as raw:
                    digest = sha256()
                    size = 0
                    with archive.open(artifact.archive_path) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            size += len(chunk)
                            if size > limits.max_file_size or size > artifact.size:
                                raise BundleError("Artifact exceeds declared size")
                            digest.update(chunk)
                            raw.write(chunk)
                    if size != artifact.size or digest.hexdigest() != artifact.sha256:
                        raise BundleError("Artifact SHA-256 mismatch")
                    raw.seek(0)
                    name, version = _artifact_identity(raw, artifact, limits)
                for node_id in artifact.package_node_ids:
                    node = packages[node_id]
                    names_equal = name == node.name if manifest.ecosystem == 'npm' else canonicalize_name(name) == canonicalize_name(node.name)
                    versions_equal = version == node.version if manifest.ecosystem == 'npm' else Version(version) == Version(node.version)
                    if not names_equal or not versions_equal:
                        raise BundleError("Original artifact identity differs from package")
            return manifest
    except BundleError:
        raise
    except (BadZipFile, KeyError, ValidationError, tarfile.TarError, OSError, ValueError, EOFError, RuntimeError, RecursionError, struct.error) as exc:
        raise BundleError("Invalid Bundle structure or artifact metadata") from exc


def build_bundle(manifest: Manifest, sources: dict[str, Path], output: Path) -> Path:
    manifest = Manifest.model_validate(manifest.model_dump())
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.packferry-', suffix='.zip', dir=output.parent)
    os.close(fd)
    staged = Path(temporary)
    try:
        with ZipFile(staged, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', manifest.model_dump_json(indent=2))
            archive.writestr('reports/summary.json', json.dumps(summary(manifest), ensure_ascii=False))
            written = set()
            for artifact in manifest.artifacts:
                source = Path(sources[artifact.sha256])
                if source.stat().st_size != artifact.size or file_sha256(source) != artifact.sha256:
                    raise BundleError("Source artifact is missing or corrupt")
                if artifact.archive_path not in written:
                    archive.write(source, artifact.archive_path)
                    written.add(artifact.archive_path)
        validate_bundle(staged)
        os.replace(staged, output)
        return output
    finally:
        staged.unlink(missing_ok=True)
