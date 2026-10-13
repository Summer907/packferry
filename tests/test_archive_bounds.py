import gzip
import io
import tarfile

import pytest

from packages.common.archives import bounded_tar_info
from packages.common.models import Input, Warning


def test_pax_allocation_rejected_before_payload_read():
    header = tarfile.TarInfo('pax')
    header.type = tarfile.XHDTYPE
    header.size = 1024**3
    stream = io.BytesIO(gzip.compress(header.tobuf()))
    with pytest.raises(ValueError, match='extension metadata'):
        with tarfile.open(fileobj=stream, mode='r|gz', tarinfo=bounded_tar_info(1024)) as archive:
            next(iter(archive))


def test_small_pax_metadata_remains_supported():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
        header = tarfile.TarInfo('package/package.json')
        header.pax_headers = {'comment': 'ordinary metadata'}
        payload = b'{"name":"demo","version":"1.0.0"}'
        header.size = len(payload)
        archive.addfile(header, io.BytesIO(payload))
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode='r|gz', tarinfo=bounded_tar_info()) as archive:
        entry = next(iter(archive))
        assert entry.name == 'package/package.json'
        assert archive.extractfile(entry).read() == payload


@pytest.mark.parametrize('source', ['demo @ https://user:password@example.org/a.whl', 'demo @ https://example.org/a.whl?token=secret', 'demo @ file:///local/project'])
def test_manifest_input_cannot_include_sensitive_source(source):
    with pytest.raises(ValueError):
        Input(normalized_constraints=[source])


def test_warning_cannot_include_credentials():
    with pytest.raises(ValueError):
        Warning(code='FAILED', message='Failed https://user:password@example.org/file')
