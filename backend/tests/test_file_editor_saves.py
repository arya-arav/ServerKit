"""Saving from the file editor. Fork: arya-arav/ServerKit.

- a save made against an old version of the file is refused (409 file_changed)
  instead of silently overwriting an edit made on the server meanwhile
- the pre-save copy goes to the backup folder, never next to the file, where a
  web server would serve wp-config.php.bak as plain text
- only the newest copies per file are kept
"""
import os

import pytest

from app.services.file_service import FileService


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'srv' / 'site'
    root.mkdir(parents=True)
    (root / 'wp-config.php').write_text("<?php define('DB_PASSWORD', 'x');\n")
    monkeypatch.setattr(FileService, 'ALLOWED_ROOTS', [str(tmp_path / 'srv')])
    monkeypatch.setattr(FileService, 'READ_ONLY_ROOTS', [])
    monkeypatch.setattr(FileService, 'EDIT_BACKUP_DIR', str(tmp_path / 'backups' / 'file-edits'))
    return root


def test_read_returns_a_version_and_save_with_it_succeeds(site):
    path = str(site / 'wp-config.php')
    version = FileService.read_file(path)['version']

    result = FileService.write_file(path, '<?php // edited\n', expected_version=version)

    assert open(path).read() == '<?php // edited\n'
    assert result['version'] != version


def test_a_save_against_an_old_version_is_refused(site):
    from app.exceptions import ConflictError
    path = str(site / 'wp-config.php')
    version = FileService.read_file(path)['version']
    stat = os.stat(path)
    with open(path, 'w') as f:
        f.write('<?php // changed on the server\n')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    with pytest.raises(ConflictError) as exc:
        FileService.write_file(path, '<?php // mine\n', expected_version=version)

    assert exc.value.code == 'file_changed'
    assert open(path).read() == '<?php // changed on the server\n'


def test_the_backup_is_kept_out_of_the_site(site, tmp_path):
    path = str(site / 'wp-config.php')

    result = FileService.write_file(path, '<?php // new\n')

    assert not os.path.exists(path + '.bak')
    assert sorted(os.listdir(site)) == ['wp-config.php']
    backup = result['backup_path']
    assert backup.startswith(str(tmp_path / 'backups' / 'file-edits'))
    assert open(backup).read() == "<?php define('DB_PASSWORD', 'x');\n"
    assert oct(os.stat(backup).st_mode & 0o777) == '0o600'


def test_only_the_newest_backups_are_kept(site, monkeypatch):
    monkeypatch.setattr(FileService, 'EDIT_BACKUPS_KEPT', 3)
    path = str(site / 'wp-config.php')
    for i in range(6):
        FileService.write_file(path, f'<?php // {i}\n')

    kept = os.listdir(FileService.EDIT_BACKUP_DIR)
    assert len(kept) == 3
    contents = sorted(open(os.path.join(FileService.EDIT_BACKUP_DIR, f)).read() for f in kept)
    assert contents == ['<?php // 2\n', '<?php // 3\n', '<?php // 4\n']


def test_write_api_returns_409_for_a_stale_version(client, auth_headers, site):
    path = str(site / 'wp-config.php')
    resp = client.post('/api/v1/files/write', headers=auth_headers,
                       json={'path': path, 'content': 'x', 'expected_version': '1'})
    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'file_changed'
