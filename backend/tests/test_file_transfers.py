"""Moving, copying and uploading in the file manager. Fork: arya-arav/ServerKit.

- a folder can't be copied or moved inside itself (copytree would recurse
  into its own output)
- an upload never replaces an existing file unless the client asks
  (409 file_exists), and never replaces a folder
- an upload's filename is a name, not a path: "../x" stays in the folder
- a full file path as the destination recreates missing folders (folder upload)
"""
import io

import pytest

from app.exceptions import ValidationError
from app.services.file_service import FileService


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'srv' / 'site'
    (root / 'css').mkdir(parents=True)
    (root / 'index.php').write_text('<?php // old\n')
    monkeypatch.setattr(FileService, 'ALLOWED_ROOTS', [str(tmp_path / 'srv')])
    monkeypatch.setattr(FileService, 'READ_ONLY_ROOTS', [])
    return root


def test_copy_into_itself_is_refused(site):
    with pytest.raises(ValidationError) as exc:
        FileService.copy(str(site), str(site / 'css' / 'site'))
    assert exc.value.code == 'into_itself'
    assert not (site / 'css' / 'site').exists()


def test_move_into_itself_is_refused(site):
    with pytest.raises(ValidationError):
        FileService.move(str(site), str(site / 'css' / 'site'))
    assert (site / 'index.php').exists()


def test_copy_and_move_to_a_sibling_still_work(site, tmp_path):
    FileService.copy(str(site / 'css'), str(site / 'css-copy'))
    FileService.move(str(site / 'css-copy'), str(tmp_path / 'srv' / 'css-moved'))
    assert (tmp_path / 'srv' / 'css-moved').is_dir()
    assert not (site / 'css-copy').exists()


def _upload(client, headers, destination, name, data=b'new', **form):
    return client.post('/api/v1/files/upload', headers=headers, data={
        'destination': destination, 'file': (io.BytesIO(data), name), **form,
    }, content_type='multipart/form-data')


def test_upload_does_not_replace_an_existing_file_by_default(client, auth_headers, site):
    resp = _upload(client, auth_headers, str(site), 'index.php')
    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'file_exists'
    assert (site / 'index.php').read_text() == '<?php // old\n'


def test_upload_replaces_when_asked(client, auth_headers, site):
    resp = _upload(client, auth_headers, str(site), 'index.php', overwrite='1')
    assert resp.status_code == 201
    assert (site / 'index.php').read_bytes() == b'new'


def test_upload_never_replaces_a_folder(client, auth_headers, site):
    resp = _upload(client, auth_headers, str(site), 'css', overwrite='1')
    assert resp.status_code == 409
    assert (site / 'css').is_dir()


def test_upload_filename_cannot_climb_out(client, auth_headers, site):
    resp = _upload(client, auth_headers, str(site / 'css'), '../escape.txt')
    assert resp.status_code == 201
    assert (site / 'css' / 'escape.txt').exists()
    assert not (site / 'escape.txt').exists()


def test_folder_upload_creates_missing_folders(client, auth_headers, site):
    dest = str(site / 'assets' / 'img' / 'logo.svg')
    resp = _upload(client, auth_headers, dest, 'logo.svg', data=b'<svg/>')
    assert resp.status_code == 201
    assert (site / 'assets' / 'img' / 'logo.svg').read_bytes() == b'<svg/>'
