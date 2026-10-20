"""Scheduled backups of databases that run in Docker containers.
Fork: arya-arav/ServerKit.

Target ``container:<container>/<database>`` (or ``container:<container>`` for
all databases). The engine comes from the image; the dump runs inside the
container; a dump without its completion trailer is thrown away.
"""
import gzip
import os

import pytest

import app.services.backup_service as bs
from app.services.backup_service import BackupService


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    base, cfg = tmp_path / 'backups', tmp_path / 'config'
    base.mkdir()
    cfg.mkdir()
    monkeypatch.setattr(BackupService, 'BACKUP_BASE_DIR', str(base))
    monkeypatch.setattr(BackupService, 'CONFIG_DIR', str(cfg))
    monkeypatch.setattr(BackupService, 'BACKUP_CONFIG', str(cfg / 'backups.json'))
    monkeypatch.setattr(BackupService, '_auto_upload', classmethod(lambda cls, p, info: None))
    return base


def fake_docker(monkeypatch, image, dump, ok=True):
    calls = []

    def run_checked(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ['docker', 'inspect']:
            if image is None:
                return {'success': False, 'output': '', 'error': 'No such object'}
            return {'success': True, 'output': image + '\n', 'error': None}
        raise AssertionError(f'unexpected {cmd}')

    def run_to_gzip(cmd, dest, **kwargs):
        calls.append(cmd)
        if not ok:
            return {'success': False, 'error': 'Access denied for user'}
        with gzip.open(dest, 'wb') as f:
            f.write(dump.encode())
        return {'success': True, 'error': None}

    monkeypatch.setattr(bs, 'run_checked', run_checked)
    monkeypatch.setattr(bs, 'run_to_gzip', run_to_gzip)
    return calls


MARIADB_DUMP = 'CREATE TABLE t (id int);\n-- Dump completed on 2026-10-21  2:30:01\n'


def test_mariadb_container_dump_is_kept(app, dirs, monkeypatch):
    calls = fake_docker(monkeypatch, 'mariadb:10.11', MARIADB_DUMP)

    res = BackupService.backup_database('container', 'shop-db-1/shop')

    assert res['success'] is True, res
    path = res['backup']['path']
    assert os.path.basename(path).startswith('container_shop-db-1_shop_')
    assert 'CREATE TABLE' in gzip.open(path).read().decode()
    exec_cmd = calls[-1]
    assert exec_cmd[:3] == ['docker', 'exec', 'shop-db-1']
    assert exec_cmd[-1] == 'shop'
    assert 'MARIADB_ROOT_PASSWORD' in exec_cmd[5]         # credentials stay in the container


def test_whole_server_dump_when_no_database_is_named(app, dirs, monkeypatch):
    calls = fake_docker(monkeypatch, 'mysql:8.4', MARIADB_DUMP)

    assert BackupService.backup_database('container', 'shop-db')['success'] is True
    assert calls[-1][-1] == 'sh'                           # no database argument


def test_postgres_container_uses_pg_dump(app, dirs, monkeypatch):
    calls = fake_docker(monkeypatch, 'postgres:16-alpine',
                        'CREATE TABLE t();\n--\n-- PostgreSQL database dump complete\n--\n')

    assert BackupService.backup_database('container', 'app-db/app')['success'] is True
    assert 'pg_dump' in calls[-1][5]


def test_an_unfinished_dump_is_thrown_away(app, dirs, monkeypatch):
    fake_docker(monkeypatch, 'mariadb:11', 'CREATE TABLE t (id int);\nINSERT INTO t VALUES (1')

    res = BackupService.backup_database('container', 'db/app')

    assert res['success'] is False
    assert 'did not finish' in res['error']
    assert os.listdir(dirs / 'databases') == []


def test_dump_errors_are_reported(app, dirs, monkeypatch):
    fake_docker(monkeypatch, 'mariadb:11', '', ok=False)

    res = BackupService.backup_database('container', 'db/app')

    assert res['success'] is False and 'Access denied' in res['error']


@pytest.mark.parametrize('image', ['redis:7', 'nginx:1.28'])
def test_non_database_containers_are_refused(app, dirs, monkeypatch, image):
    fake_docker(monkeypatch, image, '')
    res = BackupService.backup_database('container', 'cache')
    assert res['success'] is False and 'not MariaDB' in res['error']


@pytest.mark.parametrize('target', ['-x/db', 'db;rm -rf /', 'db/a b', 'db/../etc'])
def test_odd_targets_never_reach_docker(app, dirs, monkeypatch, target):
    calls = fake_docker(monkeypatch, 'mariadb:11', MARIADB_DUMP)
    res = BackupService.backup_database('container', target)
    assert res['success'] is False
    assert calls == []


def test_schedule_target_with_container_prefix(app, dirs, monkeypatch):
    fake_docker(monkeypatch, 'mariadb:10.11', MARIADB_DUMP)
    BackupService.add_schedule('db nightly', 'database', 'container:shop-db-1/shop', '02:30')
    sched = BackupService.get_config()['schedules'][0]

    BackupService._run_scheduled_backup(sched)

    assert BackupService.get_config()['schedules'][0]['last_status'] == 'success'
    assert len(os.listdir(dirs / 'databases')) == 1
