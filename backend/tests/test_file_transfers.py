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


# ── ownership: the panel runs as root ───────────────────────────────

@pytest.fixture
def chowns(monkeypatch):
    """Pretend to be root and record lchown calls instead of making them."""
    calls = {}
    monkeypatch.setattr(FileService, '_can_chown', staticmethod(lambda: True))
    monkeypatch.setattr('os.lchown', lambda path, uid, gid: calls.__setitem__(str(path), (uid, gid)))
    return calls


def test_copy_keeps_each_items_owner(site, chowns, monkeypatch):
    import os
    real_lstat = os.lstat

    def fake_lstat(path):
        st = real_lstat(path)
        uid = 1001 if str(path).endswith('index.php') else 33
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, uid, uid,
                               st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
    monkeypatch.setattr('os.lstat', fake_lstat)

    FileService.copy(str(site), str(site.parent / 'site-copy'))

    assert chowns[str(site.parent / 'site-copy')] == (33, 33)
    assert chowns[str(site.parent / 'site-copy' / 'index.php')] == (1001, 1001)
    assert chowns[str(site.parent / 'site-copy' / 'css')] == (33, 33)


def test_new_files_and_folders_take_the_parent_folders_owner(site, chowns):
    import os
    parent_owner = (os.stat(site).st_uid, os.stat(site).st_gid)
    FileService.create_file(str(site / 'new' / 'deep' / 'a.txt'))
    assert chowns[str(site / 'new')] == parent_owner
    assert chowns[str(site / 'new' / 'deep' / 'a.txt')] == parent_owner


def test_copy_keeps_symlinks_as_links(site, tmp_path):
    import os
    outside = tmp_path / 'secret.txt'
    outside.write_text('outside the allowed roots')
    os.symlink(outside, site / 'link.txt')
    FileService.copy(str(site), str(site.parent / 'site-copy'))
    copied = site.parent / 'site-copy' / 'link.txt'
    assert copied.is_symlink()
