"""Trash, permissions and owners in the file manager. Fork: arya-arav/ServerKit.

- Delete moves items to the Recycle Bin as kind 'file'; restore puts them
  back (refusing to overwrite), purge and the 30-day expiry delete for good
- permanent delete removes a symlinked folder as a link, finds broken links,
  and refuses a whole root
- recursive chmod gives folders and files their own modes and never follows
  a link; chown is admin-only and changes links, not their targets
"""
import os
from datetime import datetime, timedelta

import pytest

from app.exceptions import NotFoundError, ValidationError
from app.services import file_trash_service, recycle_bin_service
from app.services.file_service import FileService


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'srv' / 'site'
    (root / 'css').mkdir(parents=True)
    (root / 'index.php').write_text('<?php\n')
    (root / 'css' / 'app.css').write_text('body{}\n')
    monkeypatch.setattr(FileService, 'ALLOWED_ROOTS', [str(tmp_path / 'srv')])
    monkeypatch.setattr(FileService, 'READ_ONLY_ROOTS', [])
    monkeypatch.setattr(file_trash_service, 'TRASH_DIR', str(tmp_path / 'trash'))
    return root


def _bin_item(path):
    return next(i for i in recycle_bin_service.list_deleted(kind='file') if i['description'].rstrip('/') == path)


# ── trash ───────────────────────────────────────────────────────────

def test_trash_moves_a_folder_into_the_recycle_bin(app, site, tmp_path):
    with app.app_context():
        file_trash_service.trash(str(site / 'css'), user_id=1)
        assert not (site / 'css').exists()
        item = _bin_item(str(site / 'css'))
        assert item['label'] == 'css' and item['noun'] == 'file'
        assert item['deleted_by_id'] == 1
        assert stat_mode(tmp_path / 'trash') == 0o700


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_restore_puts_it_back_and_clears_the_bin(app, site):
    with app.app_context():
        file_trash_service.trash(str(site / 'index.php'))
        item = _bin_item(str(site / 'index.php'))
        restored, err = recycle_bin_service.restore('file', item['id'])
        assert err is None
        assert (site / 'index.php').read_text() == '<?php\n'
        assert recycle_bin_service.list_deleted(kind='file') == []


def test_restore_refuses_to_overwrite(app, site):
    with app.app_context():
        file_trash_service.trash(str(site / 'index.php'))
        (site / 'index.php').write_text('new one')
        item = _bin_item(str(site / 'index.php'))
        restored, err = recycle_bin_service.restore('file', item['id'])
        assert restored is None and 'already exists' in err
        assert (site / 'index.php').read_text() == 'new one'


def test_purge_and_expiry_delete_for_good(app, site, tmp_path):
    with app.app_context():
        file_trash_service.trash(str(site / 'index.php'))
        file_trash_service.trash(str(site / 'css'))
        item = _bin_item(str(site / 'index.php'))
        ok, _ = recycle_bin_service.purge('file', item['id'])
        assert ok

        from app import db
        from app.models.trashed_file import TrashedFile
        row = TrashedFile.query.one()
        row.deleted_at = datetime.utcnow() - timedelta(days=31)
        db.session.commit()
        assert recycle_bin_service.purge_expired() == {'file': 1}
        assert os.listdir(tmp_path / 'trash') == []


def test_trash_route_and_root_guard(client, auth_headers, site):
    resp = client.post('/api/v1/files/trash', headers=auth_headers, json={'path': str(site / 'index.php')})
    assert resp.status_code == 200
    resp = client.post('/api/v1/files/trash', headers=auth_headers, json={'path': str(site.parent)})
    assert resp.status_code == 400
    assert site.parent.exists()


# ── permanent delete fixes ──────────────────────────────────────────

def test_delete_removes_a_symlinked_folder_as_a_link(site):
    os.symlink(site / 'css', site / 'styles')
    FileService.delete(str(site / 'styles'))
    assert not os.path.lexists(site / 'styles')
    assert (site / 'css' / 'app.css').exists()


def test_delete_finds_a_broken_link(site):
    os.symlink(site / 'gone', site / 'dangling')
    FileService.delete(str(site / 'dangling'))
    assert not os.path.lexists(site / 'dangling')


def test_delete_refuses_a_root(site):
    with pytest.raises(ValidationError):
        FileService.delete(str(site.parent))
    with pytest.raises(NotFoundError):
        FileService.delete(str(site / 'nope'))


# ── chmod / chown ───────────────────────────────────────────────────

def test_recursive_chmod_splits_folder_and_file_modes(site):
    result = FileService.change_permissions(str(site), '750', recursive=True, file_mode='640')
    assert stat_mode(site) == 0o750
    assert stat_mode(site / 'css') == 0o750
    assert stat_mode(site / 'index.php') == 0o640
    assert stat_mode(site / 'css' / 'app.css') == 0o640
    assert result['changed'] == 4


def test_recursive_chmod_never_follows_links(site, tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('x')
    os.chmod(outside, 0o600)
    os.symlink(outside, site / 'link')
    FileService.change_permissions(str(site), '755', recursive=True, file_mode='666')
    assert stat_mode(outside) == 0o600


def test_chmod_rejects_bad_modes(site):
    for mode in ('999', '4755', 'rwx'):
        with pytest.raises(ValidationError):
            FileService.change_permissions(str(site / 'index.php'), mode)


def test_chown_uses_lchown_and_validates_names(site, monkeypatch):
    calls = []
    monkeypatch.setattr('os.lchown', lambda p, u, g: calls.append((os.path.basename(p), u, g)))
    import grp
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    os.symlink('/etc/passwd', site / 'link')
    FileService.change_owner(str(site), me, group, recursive=True)
    names = {c[0] for c in calls}
    assert {'site', 'css', 'index.php', 'app.css', 'link'} <= names
    with pytest.raises(ValidationError):
        FileService.change_owner(str(site), 'no-such-user-xyz', group)


def test_chown_and_owner_list_are_admin_only(client, developer_headers, site):
    headers = developer_headers
    assert client.post('/api/v1/files/chown', headers=headers,
                       json={'path': str(site), 'owner': 'root', 'group': 'root'}).status_code == 403
    assert client.get('/api/v1/files/owners', headers=headers).status_code == 403


def test_owner_list(client, auth_headers):
    data = client.get('/api/v1/files/owners', headers=auth_headers).get_json()
    assert 'root' in data['users'] and 'root' in data['groups']
