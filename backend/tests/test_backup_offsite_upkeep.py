"""Off-site backup upkeep. Fork: arya-arav/ServerKit.

- a file backup's metadata says "synced" once its upload succeeded
- a scheduled upload of an already-synced backup is not sent twice
- the daily cleanup also deletes off-site copies past retention, only under
  the destination's path prefix, and never without one
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest


class FakeStorage:
    def __init__(self, prefix='serverkit-backups', objects=None):
        self.config = {'provider': 's3', 's3': {'path_prefix': prefix}}
        self.objects = dict(objects or {})
        self.uploads = []
        self.deleted = []

    def get_config(self):
        return self.config

    def upload_file(self, path, remote_key=None):
        self.uploads.append(path)
        return {'success': True, 'remote_key': f'serverkit-backups/files/{os.path.basename(path)}'}

    def upload_directory(self, path, remote_prefix=None):
        self.uploads.append(path)
        return {'success': True}

    def list_files(self, prefix_filter=None):
        return {'success': True, 'files': [
            {'key': k, 'size': 1, 'last_modified': v.isoformat()} for k, v in self.objects.items()]}

    def delete_file(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)
        return {'success': True}


@pytest.fixture
def svc(tmp_path, monkeypatch):
    from app.services.backup_service import BackupService
    from app.services.storage_provider_service import StorageProviderService
    base, cfg = tmp_path / 'backups', tmp_path / 'config'
    base.mkdir()
    cfg.mkdir()
    monkeypatch.setattr(BackupService, 'BACKUP_BASE_DIR', str(base))
    monkeypatch.setattr(BackupService, 'CONFIG_DIR', str(cfg))
    monkeypatch.setattr(BackupService, 'BACKUP_CONFIG', str(cfg / 'backups.json'))
    fake = FakeStorage()
    for name in ('get_config', 'upload_file', 'upload_directory', 'list_files', 'delete_file'):
        monkeypatch.setattr(StorageProviderService, name, getattr(fake, name))
    BackupService.fake = fake
    return BackupService


def _payload(tmp_path):
    p = tmp_path / 'site'
    p.mkdir()
    (p / 'index.php').write_text('<?php echo 1;')
    return str(p)


def test_file_backup_metadata_says_synced_after_auto_upload(app, svc, tmp_path):
    svc.fake.config['auto_upload'] = True

    res = svc.backup_files([_payload(tmp_path)], backup_name='site')

    meta_path = res['path'][:-len('.tar.gz')] + '.json'
    assert json.load(open(meta_path))['remote_status'] == 'synced'
    assert res['backup']['remote_status'] == 'synced'


def test_scheduled_upload_marks_metadata_and_does_not_upload_twice(app, svc, tmp_path):
    res = svc.backup_files([_payload(tmp_path)], backup_name='site')  # auto_upload off
    assert svc.fake.uploads == []

    svc._upload_scheduled(res)
    svc._upload_scheduled(res)

    assert len(svc.fake.uploads) == 1
    meta_path = res['path'][:-len('.tar.gz')] + '.json'
    assert json.load(open(meta_path))['remote_status'] == 'synced'


def test_cleanup_deletes_offsite_copies_past_retention(app, svc):
    now = datetime.now(timezone.utc)
    svc.fake.objects = {
        'serverkit-backups/files/old.tar.gz': now - timedelta(days=40),
        'serverkit-backups/files/new.tar.gz': now - timedelta(days=2),
    }
    svc.save_config({'retention_days': 30})

    res = svc.cleanup_old_backups()

    assert res['remote_deleted_count'] == 1
    assert svc.fake.deleted == ['serverkit-backups/files/old.tar.gz']


def test_remote_retention_can_differ_from_local(app, svc):
    now = datetime.now(timezone.utc)
    svc.fake.objects = {'serverkit-backups/files/a.tar.gz': now - timedelta(days=40)}
    svc.save_config({'retention_days': 30, 'remote_retention_days': 90})

    assert svc.cleanup_old_backups()['remote_deleted_count'] == 0


def test_cleanup_never_sweeps_a_bucket_without_a_prefix(app, svc):
    svc.fake.config['s3']['path_prefix'] = ''
    svc.fake.objects = {'someone-elses-file': datetime.now(timezone.utc) - timedelta(days=400)}

    res = svc.cleanup_old_backups()

    assert res['success'] is True
    assert svc.fake.deleted == []
    assert 'prefix' in res['remote_error']
