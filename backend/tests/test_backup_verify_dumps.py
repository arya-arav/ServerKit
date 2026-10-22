"""Tier-1 verification of single-file backups (database dumps, path-list archives).

Database backups are gzip'd SQL dumps, not tar archives. The verifier used to
open every primary artifact as a tar, so every database backup read as
"invalid header" and Doctor's repair recorded a failure for a good dump. Policy
runs also never wrote their manifest or ran the verifier, so every new run sat
at "not verified" with no stored checksum. These tests pin both fixes, and that
a single-file run never lists (and hashes) the shared backups folder it lives in.
"""
import gzip
import io
import os
import tarfile
from types import SimpleNamespace

from app import db
from app.services import backup_verify_service as bv
from app.services.backup_policy_service import BackupPolicyService


def _write_sql_gz(dest, body=b'CREATE TABLE t (id int);\n-- Dump completed\n'):
    with gzip.open(dest, 'wb') as gz:
        gz.write(body * 50)


def _mk_run(policy_id, storage_path, primary):
    from app.models.backup_run import BackupRun
    run = BackupRun(policy_id=policy_id, kind='full', status='success', storage_path=storage_path)
    meta = {'engine': 'database', 'kind': 'full', 'primary_archive': primary, 'incremental': False}
    run.set_metadata(meta)
    db.session.add(run)
    db.session.commit()
    return run, meta


def test_a_gzipped_sql_dump_verifies_as_hashed(app, tmp_path):
    with app.app_context():
        policy = BackupPolicyService.get_or_create_policy('database', 9301, target_meta={'db_name': 'shop'})
        dump = str(tmp_path / 'mysql_shop_20260101_000000.sql.gz')
        _write_sql_gz(dump)
        run, meta = _mk_run(policy.id, dump, dump)

        bv.write_manifest(run, meta)
        probes = bv.verify_run_tier1(run, meta)

        assert probes['listed'][0]['ok'] is True
        assert run.verify_level == 'hashed'
        assert run.verify_error is None
        assert run.checksum_sha256 == bv.sha256_file(dump)


def test_a_corrupt_gzip_dump_is_unreadable(app, tmp_path):
    with app.app_context():
        policy = BackupPolicyService.get_or_create_policy('database', 9302, target_meta={'db_name': 'shop'})
        dump = tmp_path / 'mysql_shop_bad.sql.gz'
        _write_sql_gz(str(dump))
        data = dump.read_bytes()
        dump.write_bytes(data[: len(data) // 2])  # truncated mid-stream
        run, meta = _mk_run(policy.id, str(dump), str(dump))

        bv.verify_run_tier1(run, meta)

        assert run.verify_level == 'none'
        assert 'gzip stream is corrupt' in run.verify_error


def test_a_broken_tar_gz_still_fails_as_a_tar(app, tmp_path):
    """A gzip'd tar with a cut-off member must not pass as a plain gzip blob."""
    with app.app_context():
        policy = BackupPolicyService.get_or_create_policy('files', 9303, target_meta={'paths': ['/x']})
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            payload = b'A' * 4096
            info = tarfile.TarInfo('data.bin')
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        archive = tmp_path / 'files.tar.gz'
        with gzip.open(str(archive), 'wb') as gz:
            gz.write(buf.getvalue()[:1024])  # header + part of the member, valid gzip framing
        run, meta = _mk_run(policy.id, str(archive), str(archive))

        bv.verify_run_tier1(run, meta)

        assert run.verify_level == 'none'
        assert 'gzip stream is corrupt' not in (run.verify_error or '')


def test_a_single_file_run_does_not_list_its_shared_folder(app, tmp_path):
    with app.app_context():
        policy = BackupPolicyService.get_or_create_policy('database', 9304, target_meta={'db_name': 'shop'})
        dump = str(tmp_path / 'mysql_shop_new.sql.gz')
        _write_sql_gz(dump)
        _write_sql_gz(str(tmp_path / 'mysql_shop_older.sql.gz'))  # another run's dump
        run, meta = _mk_run(policy.id, dump, dump)

        manifest = bv.write_manifest(run, meta)

        # only this run's own dump, never the other run's file next to it
        assert [a['name'] for a in manifest['artifacts']] == ['mysql_shop_new.sql.gz']
        assert manifest['primary_sha256'] == bv.sha256_file(dump)
        assert not os.path.exists(tmp_path / bv.MANIFEST_NAME)


def test_a_policy_run_writes_its_checksum_and_verifies(app, tmp_path, monkeypatch):
    from app.models.backup_run import BackupRun
    from app.services.backup_service import BackupService

    dump = str(tmp_path / 'mysql_shop_run.sql.gz')

    def fake_backup_database(db_type, db_name, user=None, password=None, host='localhost', **_kw):
        _write_sql_gz(dump)
        return {'success': True, 'backup': {'name': os.path.basename(dump), 'path': dump,
                                            'size': os.path.getsize(dump)}}

    monkeypatch.setattr(BackupService, 'backup_database', staticmethod(fake_backup_database))
    monkeypatch.setattr(BackupPolicyService, '_notify', staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(BackupPolicyService, 'apply_retention', classmethod(lambda cls, p: None))

    with app.app_context():
        policy = BackupPolicyService.get_or_create_policy(
            'database', 9305, target_subtype='mysql', target_meta={'db_name': 'shop'})
        job = SimpleNamespace(id=None, get_payload=lambda: {'policy_id': policy.id})

        result = BackupPolicyService.run_backup_policy_job(job)
        run = BackupRun.query.get(result['run_id'])

        assert result['status'] == 'success'
        assert run.checksum_sha256 == bv.sha256_file(dump)
        assert run.verify_level == 'hashed'
        assert run.verify_error is None
