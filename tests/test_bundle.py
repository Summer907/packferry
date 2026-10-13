from zipfile import ZipFile
from hashlib import sha256

import pytest

from packages.common.models import Manifest
from packages.common.bundle import BundleError, BundleLimits, build_bundle, validate_bundle
from packages.common.hashes import blob_path


@pytest.fixture
def sample(tmp_path):
    raw = tmp_path / 'demo-1.0-py3-none-any.whl'
    with ZipFile(raw, 'w') as wheel:
        wheel.writestr('demo-1.0.dist-info/METADATA', 'Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n')
        wheel.writestr('demo/__init__.py', '')
    digest = sha256(raw.read_bytes()).hexdigest()
    manifest = Manifest.model_validate({
        'ecosystem': 'pypi', 'input': {'normalized_constraints': ['demo==1.0']},
        'environments': [{'environment_id': 'win'}],
        'packages': [{'node_id': 'demo@1.0', 'name': 'demo', 'version': '1.0', 'source': 'https://pypi.org/simple/demo/'}],
        'artifacts': [{'artifact_id': 'demo-wheel', 'filename': raw.name, 'type': 'wheel', 'archive_path': blob_path(digest),
                       'size': raw.stat().st_size, 'sha256': digest, 'source': 'https://files.pythonhosted.org/demo.whl',
                       'package_node_ids': ['demo@1.0']}],
        'resolutions': [{'environment_id': 'win', 'nodes': ['demo@1.0'], 'selected_artifacts': ['demo-wheel'], 'status': 'COMPLETE'}],
        'verification': {'resolution_status': 'COMPLETE', 'collection_status': 'COMPLETE'},
    })
    return manifest, {digest: raw}


def test_roundtrip_and_physical_dedup(sample, tmp_path):
    manifest, sources = sample
    second = manifest.artifacts[0].model_copy(update={'artifact_id': 'second'})
    manifest.artifacts.append(second)
    result = build_bundle(manifest, sources, tmp_path / 'roundtrip.zip')
    assert validate_bundle(result).bundle_id == manifest.bundle_id
    with ZipFile(result) as archive:
        assert len(archive.namelist()) == 3


@pytest.mark.parametrize('entry', ['../escape', '/absolute', 'a\\b', '%2e%2e/escape', 'C:/file', 'manifest.JSON'])
def test_unsafe_paths(sample, tmp_path, entry):
    manifest, sources = sample
    result = build_bundle(manifest, sources, tmp_path / 'bad.zip')
    with ZipFile(result, 'a') as archive:
        archive.writestr(entry, b'bad')
    with pytest.raises(BundleError):
        validate_bundle(result)


def test_corrupt_cache_does_not_replace_output(sample, tmp_path):
    manifest, sources = sample
    output = tmp_path / 'bundle.zip'
    output.write_bytes(b'previous')
    next(iter(sources.values())).write_bytes(b'corrupt')
    with pytest.raises(BundleError):
        build_bundle(manifest, sources, output)
    assert output.read_bytes() == b'previous'


def test_carrier_metadata_rejected(sample, tmp_path):
    manifest, sources = sample
    raw = next(iter(sources.values()))
    with ZipFile(raw, 'w') as archive:
        archive.writestr('demo-1.0.dist-info/METADATA', 'Name: demo\nVersion: 1.0\nX-PackFerry-Carrier: true\n')
    digest = sha256(raw.read_bytes()).hexdigest()
    manifest.artifacts[0] = manifest.artifacts[0].model_copy(update={'sha256': digest, 'size': raw.stat().st_size, 'archive_path': blob_path(digest)})
    with pytest.raises(BundleError, match='carrier'):
        build_bundle(manifest, {digest: raw}, tmp_path / 'carrier.zip')


def test_size_limit(sample, tmp_path):
    manifest, sources = sample
    result = build_bundle(manifest, sources, tmp_path / 'bundle.zip')
    with pytest.raises(BundleError):
        validate_bundle(result, BundleLimits(max_zip_size=10))


def test_identity_mismatch(sample, tmp_path):
    manifest, sources = sample
    manifest.packages[0].name = 'other'
    with pytest.raises(BundleError):
        build_bundle(manifest, sources, tmp_path / 'bundle.zip')


def test_dangling_reference(sample):
    manifest, _ = sample
    manifest.packages[0].dependencies = ['absent']
    with pytest.raises(ValueError):
        Manifest.model_validate(manifest.model_dump())


def test_wheel_generator_carrier_rejected(sample, tmp_path):
    manifest, sources = sample
    raw = next(iter(sources.values()))
    with ZipFile(raw, 'a') as wheel:
        wheel.writestr('demo-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: packferry-metadata-carrier\n')
    digest = sha256(raw.read_bytes()).hexdigest()
    manifest.artifacts[0] = manifest.artifacts[0].model_copy(update={'sha256': digest, 'size': raw.stat().st_size, 'archive_path': blob_path(digest)})
    with pytest.raises(BundleError, match='carrier'):
        build_bundle(manifest, {digest: raw}, tmp_path / 'carrier.zip')


def test_central_directory_entry_limit(sample, tmp_path):
    manifest, sources = sample
    result = build_bundle(manifest, sources, tmp_path / 'bundle.zip')
    with pytest.raises(BundleError, match='central-directory'):
        validate_bundle(result, BundleLimits(max_entries=2))
