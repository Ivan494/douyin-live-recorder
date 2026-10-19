import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_artifacts as release

REAL_ALLOWLIST_LOADER = release.load_internal_allowlist
FIXTURE_INTERNAL = {
    release.INTERNAL + 'python311.dll',
    release.INTERNAL + 'package/library.dat',
}


@pytest.fixture(autouse=True)
def small_fixture_inventory(monkeypatch):
    monkeypatch.setattr(release, 'load_internal_allowlist', lambda: FIXTURE_INTERNAL)


def package(tmp_path, extra=None, profiles=None, bundled=None, signed=False):
    payload = {name: b'fixture' for name in release.FILES}
    payload[release.INTERNAL + 'python311.dll'] = b'MZruntime'
    payload[release.INTERNAL + 'package/library.dat'] = b'public data'
    payload.update(bundled or {})
    payload[release.APP + 'profiles.json'] = json.dumps(profiles or []).encode()
    payload[release.APP + 'settings.json'] = json.dumps(release.PUBLIC_SETTINGS).encode()
    manifest = {
        'version': '1.2.3',
        'signatures_required': signed,
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
    }
    payload['release-manifest.json'] = json.dumps(manifest).encode()
    payload.update(extra or {})
    path = tmp_path / 'release.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    return path


def test_clean_package_is_accepted(tmp_path):
    assert release.verify_zip(package(tmp_path))['version'] == '1.2.3'


def test_checked_in_inventory_is_sorted_and_matches_reviewed_build():
    names = REAL_ALLOWLIST_LOADER()
    assert len(names) == 1175
    assert release.INTERNAL + 'certifi/cacert.pem' in names
    assert release.INTERNAL + 'python311.dll' in names


@pytest.mark.parametrize('name', ['douyindownload/_automation/mobile_session.json', 'logs/app.log', '../escape.txt', 'C:/Users/private.txt'])
def test_extra_private_or_unsafe_file_is_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        release.verify_zip(package(tmp_path, {name: b'private'}))


def test_colon_inside_internal_path_is_rejected_before_extraction(tmp_path):
    with pytest.raises(ValueError, match='Unsafe archive path'):
        release.verify_zip(package(tmp_path, bundled={release.INTERNAL + 'C:/escape.dll': b'MZbad'}))


@pytest.mark.parametrize('name', [
    'douyindownload/_automation/_internal/mobile_session.json',
    'douyindownload/_automation/_internal/config_backups/backup.json',
    'douyindownload/_automation/_internal/.env',
    'douyindownload/_automation/_internal/recording.mkv',
    'douyindownload/_automation/_internal/private.pem',
])
def test_private_file_inside_bundle_is_rejected_even_with_valid_hash(tmp_path, name):
    with pytest.raises(ValueError, match='private'):
        release.verify_zip(package(tmp_path, bundled={name: b'private'}))


def test_arbitrary_extra_bundle_file_is_rejected_even_with_valid_hash(tmp_path):
    with pytest.raises(ValueError, match='unexpected'):
        release.verify_zip(package(tmp_path, bundled={release.INTERNAL + 'credentials.json': b'private'}))


def test_unlisted_bundle_file_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='manifest'):
        release.verify_zip(package(tmp_path, extra={release.INTERNAL + 'unexpected.dll': b'MZunexpected'}))


def test_require_signatures_rejects_unsigned_build(tmp_path):
    with pytest.raises(ValueError, match='signing-required'):
        release.verify_zip(package(tmp_path), require_signatures=True)


def test_signed_build_checks_all_pe_files_and_hidden_pe_content(tmp_path, monkeypatch):
    checked = []
    monkeypatch.setattr(release, 'load_internal_allowlist', lambda: FIXTURE_INTERNAL | {release.INTERNAL + 'hidden.bin'})
    monkeypatch.setattr(release, 'verify_signatures', lambda paths: checked.extend(path.as_posix() for path in paths))
    path = package(tmp_path, bundled={release.INTERNAL + 'hidden.bin': b'MZnative'}, signed=True)
    release.verify_zip(path, require_signatures=True)
    assert len(checked) == 6
    assert any(name.endswith('/hidden.bin') for name in checked)
    assert any(name.endswith('/ffmpeg.exe') for name in checked)


def test_signed_stage_fails_before_manifest_when_signature_check_fails(tmp_path, monkeypatch):
    directory = tmp_path / 'stage'
    with zipfile.ZipFile(package(tmp_path)) as archive:
        archive.extractall(directory)
    (directory / 'release-manifest.json').unlink()
    def reject(_paths):
        raise ValueError('NotSigned')
    monkeypatch.setattr(release, 'verify_signatures', reject)
    with pytest.raises(ValueError, match='NotSigned'):
        release.stage_manifest(directory, '1.2.3', require_signatures=True)
    assert not (directory / 'release-manifest.json').exists()


def test_stage_must_match_generated_pyinstaller_inventory(tmp_path):
    directory = tmp_path / 'stage'
    with zipfile.ZipFile(package(tmp_path)) as archive:
        archive.extractall(directory)
    (directory / 'release-manifest.json').unlink()
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    shutil.copy2(directory / release.APP / 'DouyinLiveRecorder.exe', bundle)
    shutil.copytree(directory / release.INTERNAL, bundle / '_internal')
    (bundle / '_internal/package/library.dat').unlink()
    with pytest.raises(ValueError, match='PyInstaller output'):
        release.stage_manifest(directory, '1.2.3', bundle=bundle)


def test_stage_rejects_unexpected_empty_directory(tmp_path):
    directory = tmp_path / 'stage'
    with zipfile.ZipFile(package(tmp_path)) as archive:
        archive.extractall(directory)
    (directory / 'release-manifest.json').unlink()
    (directory / 'orphan').mkdir()
    with pytest.raises(ValueError, match='empty staging directories'):
        release.stage_manifest(directory, '1.2.3')


@pytest.mark.parametrize('extra', [
    {'README.txt/child': b'conflict'},
    {'README.txt/': b''},
])
def test_zip_rejects_file_directory_prefix_conflict(tmp_path, extra):
    with pytest.raises(ValueError, match='prefix conflict'):
        release.verify_zip(package(tmp_path, extra=extra))


def test_zip_rejects_unexpected_empty_directory(tmp_path):
    with pytest.raises(ValueError, match='unexpected directory'):
        release.verify_zip(package(tmp_path, extra={'CON/': b''}))


def test_nonempty_profiles_rejected_even_with_valid_hash(tmp_path):
    with pytest.raises(ValueError, match='defaults'):
        release.verify_zip(package(tmp_path, profiles=[{'id': 'personal'}]))


def test_modified_binary_rejected(tmp_path):
    with pytest.raises(ValueError, match='Hash mismatch'):
        release.verify_zip(package(tmp_path, {'DouyinLiveRecorder.exe': b'tampered'}))


def test_private_setting_rejected():
    with pytest.raises(ValueError):
        release.validate_defaults([], {**release.PUBLIC_SETTINGS, 'cookies': 'private'})


@pytest.mark.parametrize('version', ['../other', '1.2.3/../../other', 'v1.2.3', ''])
def test_version_cannot_escape_build_directory(version):
    with pytest.raises(ValueError):
        release.version_value(version)


def test_public_source_is_sanitized():
    release.verify_source()
