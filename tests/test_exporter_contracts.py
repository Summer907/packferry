import pytest
from zipfile import ZipFile

from apps.exporter.services import ExportService
from apps.exporter.adapters import _platform_for_uv, _marker_context, load_python_input
from apps.exporter.python_bridge import _read_static_metadata, _read_build_requirements
from packages.common.models import Environment


def test_project_file_prefers_adjacent_pylock(tmp_path):
    project = tmp_path / 'pyproject.toml'
    project.write_text('[project]\nname="demo"\n', encoding='utf-8')
    lock = tmp_path / 'pylock.toml'
    lock.write_text('lock-version="1.0"\n', encoding='utf-8')
    assert ExportService._pylock_path(project, True) == lock
    assert ExportService._pylock_path(project, False) is None


@pytest.mark.parametrize('environment', [
    Environment(environment_id='pypy', runtime='pypy'),
    Environment(environment_id='musl', platform='linux_x86_64', libc='musl'),
])
def test_unverified_runtime_cannot_use_cpython_glibc_tags(environment):
    with pytest.raises(ValueError):
        _platform_for_uv(environment)


def test_target_implementation_version_does_not_use_host():
    context = _marker_context(Environment(environment_id='py39', version='3.9.20', abi='cp39'))
    assert context['implementation_version'] == '3.9.20'


def test_static_zip_sdist_metadata_and_build_requirements(tmp_path):
    path = tmp_path / 'demo-1.0.zip'
    with ZipFile(path, 'w') as archive:
        archive.writestr('demo-1.0/PKG-INFO', 'Metadata-Version: 2.3\nName: demo\nVersion: 1.0\nRequires-Dist: helper>=1\n')
        archive.writestr('demo-1.0/pyproject.toml', '[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="setuptools.build_meta"\n')
    _, requirements = _read_static_metadata(path, 'sdist', 'demo', '1.0')
    assert str(requirements[0]) == 'helper>=1'
    assert _read_build_requirements(path) == ['setuptools>=68']


def test_direct_url_input_rejected_without_echoing_password():
    with pytest.raises(ValueError) as error:
        load_python_input(['demo @ https://user:very-secret-password@example.org/a.whl'], None)
    assert 'very-secret-password' not in str(error.value)


def test_cancel_before_export_cannot_be_reset(tmp_path):
    from apps.exporter.services import ExportRequest
    service = ExportService(tmp_path)
    service.cancel()
    with pytest.raises(RuntimeError, match='cancelled'):
        service.export(ExportRequest(ecosystem='pypi', constraints=['six==1.17.0']), tmp_path / 'bundle.zip')
