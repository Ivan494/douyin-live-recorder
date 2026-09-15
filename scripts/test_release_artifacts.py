import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_artifacts as release


def package(tmp_path, extra=None, profiles=None):
    payload = {name: b'fixture' for name in release.FILES}
    payload[release.APP + 'profiles.json'] = json.dumps(profiles or []).encode()
    payload[release.APP + 'settings.json'] = json.dumps(release.PUBLIC_SETTINGS).encode()
    manifest = {'version': '1.2.3', 'files': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    payload['release-manifest.json'] = json.dumps(manifest).encode()
    payload.update(extra or {})
    path = tmp_path / 'release.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    return path


def test_clean_package_is_accepted(tmp_path):
    assert release.verify_zip(package(tmp_path))['version'] == '1.2.3'


@pytest.mark.parametrize('name', ['douyindownload/_automation/mobile_session.json', 'logs/app.log', '../escape.txt', 'C:/Users/private.txt'])
def test_extra_private_or_unsafe_file_is_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        release.verify_zip(package(tmp_path, {name: b'private'}))


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
