"""Compress, extract and zip-for-download. Fork: arya-arav/ServerKit.

Extraction treats every archive as hostile: zip slip, absolute paths, links,
bombs and setuid bits are all refused or neutralised, and nothing is
overwritten without asking.
"""
import io
import os
import stat
import tarfile
import zipfile

import pytest

from app.exceptions import ConflictError, ValidationError
from app.services import archive_service
from app.services.archive_service import ArchiveService, archive_kind, archive_stem
from app.services.file_service import FileService


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'srv' / 'site'
    (root / 'css').mkdir(parents=True)
    (root / 'index.php').write_text('<?php echo 1;\n')
    (root / 'css' / 'app.css').write_text('body{}\n')
    monkeypatch.setattr(FileService, 'ALLOWED_ROOTS', [str(tmp_path / 'srv')])
    monkeypatch.setattr(FileService, 'READ_ONLY_ROOTS', [])
    monkeypatch.setattr(archive_service, 'DOWNLOAD_DIR', str(tmp_path / 'downloads'))
    return root


def _zip(path, entries):
    """entries: {name: bytes or None (dir)} or ZipInfo-tuples."""
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            if isinstance(data, zipfile.ZipInfo):
                zf.writestr(data, b'')
            elif data is None:
                zf.writestr(name if name.endswith('/') else name + '/', b'')
            else:
                zf.writestr(name, data)
    return str(path)


def test_names_and_kinds():
    assert archive_kind('/a/site.tar.gz') == 'tar'
    assert archive_kind('/a/site.ZIP') == 'zip'
    assert archive_kind('/a/site.rar') is None
    assert archive_stem('/a/site.tar.gz') == 'site'


# ── compress ────────────────────────────────────────────────────────

@pytest.mark.parametrize('fmt', ['zip', 'tar.gz', 'tar'])
def test_compress_then_extract_round_trips(site, fmt):
    dest = str(site.parent / f'backup{archive_service.FORMATS[fmt]}')
    ArchiveService.check_compress([str(site)], dest, fmt)
    result = ArchiveService.compress([str(site)], dest, fmt)
    assert result['files'] == 2 and os.path.isfile(dest)
    assert not any(n.endswith('.part') for n in os.listdir(site.parent))

    out = site.parent / 'restored'
    ArchiveService.extract(dest, str(out))
    assert (out / 'site' / 'css' / 'app.css').read_text() == 'body{}\n'


def test_compress_refuses_an_existing_name_and_a_wrong_extension(site):
    (site.parent / 'taken.zip').write_bytes(b'x')
    with pytest.raises(ConflictError):
        ArchiveService.check_compress([str(site)], str(site.parent / 'taken.zip'), 'zip')
    with pytest.raises(ValidationError):
        ArchiveService.check_compress([str(site)], str(site.parent / 'x.zip'), 'tar.gz')


def test_compress_inside_the_folder_skips_its_own_archive(site):
    dest = str(site / 'site.zip')
    ArchiveService.compress([str(site)], dest, 'zip')
    with zipfile.ZipFile(dest) as zf:
        assert not any(n.endswith('site.zip') or '.part' in n for n in zf.namelist())


def test_zip_skips_symlinks_and_tar_keeps_them_as_links(site, tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_text('outside')
    os.symlink(secret, site / 'link.txt')

    zip_result = ArchiveService.compress([str(site)], str(site.parent / 's.zip'), 'zip')
    assert zip_result['skipped'] == 1
    with zipfile.ZipFile(site.parent / 's.zip') as zf:
        assert 'site/link.txt' not in zf.namelist()

    ArchiveService.compress([str(site)], str(site.parent / 's.tar'), 'tar')
    with tarfile.open(site.parent / 's.tar') as tf:
        assert tf.getmember('site/link.txt').issym()


# ── extract: hostile archives ───────────────────────────────────────

@pytest.mark.parametrize('name', ['../evil.txt', '/etc/evil.txt', 'a/../../evil.txt', 'C:/evil.txt'])
def test_zip_slip_is_refused_before_anything_is_written(site, name):
    archive = _zip(site.parent / 'bad.zip', {'ok.txt': b'fine', name: b'evil'})
    out = site.parent / 'out'
    with pytest.raises(ValidationError):
        ArchiveService.extract(archive, str(out))
    assert not (out / 'ok.txt').exists()
    assert not (site.parent / 'evil.txt').exists()


def test_tar_slip_and_links_are_neutralised(site, tmp_path):
    archive = str(site.parent / 'links.tar')
    with tarfile.open(archive, 'w') as tf:
        info = tarfile.TarInfo('ok.txt')
        info.size = 4
        tf.addfile(info, io.BytesIO(b'fine'))
        link = tarfile.TarInfo('passwd')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        tf.addfile(link)
        hard = tarfile.TarInfo('hard')
        hard.type = tarfile.LNKTYPE
        hard.linkname = '/etc/shadow'
        tf.addfile(hard)
    out = site.parent / 'out'
    result = ArchiveService.extract(archive, str(out))
    assert (out / 'ok.txt').read_text() == 'fine'
    assert not os.path.lexists(out / 'passwd')
    assert not os.path.lexists(out / 'hard')
    assert result['skipped'] == 2

    evil = str(site.parent / 'evil.tar')
    with tarfile.open(evil, 'w') as tf:
        info = tarfile.TarInfo('../escape.txt')
        info.size = 1
        tf.addfile(info, io.BytesIO(b'x'))
    with pytest.raises(ValidationError):
        ArchiveService.extract(evil, str(out))


def test_writes_never_follow_a_symlink_already_in_the_destination(site, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    out = site.parent / 'out'
    out.mkdir()
    os.symlink(outside, out / 'linked')
    archive = _zip(site.parent / 'a.zip', {'linked/x.txt': b'x'})
    with pytest.raises(ValidationError):
        ArchiveService.extract(archive, str(out))
    assert not (outside / 'x.txt').exists()


def test_a_root_dot_entry_is_fine(site):
    archive = str(site.parent / 'dot.tar')
    with tarfile.open(archive, 'w') as tf:
        tf.add(str(site), arcname='.')
    out = site.parent / 'out'
    ArchiveService.extract(archive, str(out))
    assert (out / 'index.php').exists()


def test_zip_bomb_ratio_is_refused(site, monkeypatch):
    monkeypatch.setattr(archive_service, 'RATIO_MIN_BYTES', 1024)
    monkeypatch.setattr(archive_service, 'MAX_RATIO', 50)
    archive = _zip(site.parent / 'bomb.zip', {'zeros.bin': b'\0' * 1024 * 1024})
    with pytest.raises(ValidationError, match='bomb'):
        ArchiveService.extract(archive, str(site.parent / 'out'))


def test_total_size_cap(site, monkeypatch):
    monkeypatch.setattr(archive_service, 'MAX_EXTRACT_BYTES', 10)
    archive = _zip(site.parent / 'big.zip', {'a.txt': b'0123456789abc'})
    with pytest.raises(ValidationError, match='too large'):
        ArchiveService.extract(archive, str(site.parent / 'out'))


def test_setuid_bits_are_stripped(site):
    archive = str(site.parent / 'suid.tar')
    with tarfile.open(archive, 'w') as tf:
        info = tarfile.TarInfo('run.sh')
        info.size = 2
        info.mode = 0o4755
        tf.addfile(info, io.BytesIO(b'#!'))
    out = site.parent / 'out'
    ArchiveService.extract(archive, str(out))
    mode = os.stat(out / 'run.sh').st_mode
    assert not mode & stat.S_ISUID
    assert mode & 0o777 == 0o755


def test_damaged_archive_is_a_clear_error(site):
    bad = site.parent / 'broken.zip'
    bad.write_bytes(b'not a zip')
    with pytest.raises(ValidationError, match='damaged'):
        ArchiveService.extract(str(bad), str(site.parent / 'out'))


# ── extract: overwriting ────────────────────────────────────────────

def test_conflicts_are_reported_and_nothing_is_written(site):
    archive = _zip(site.parent / 'u.zip', {'index.php': b'NEW', 'new.txt': b'n'})
    result = ArchiveService.extract(archive, str(site))
    assert result['conflicts'] == 1 and result['examples'] == ['index.php']
    assert (site / 'index.php').read_text() == '<?php echo 1;\n'
    assert not (site / 'new.txt').exists()

    result = ArchiveService.extract(archive, str(site), overwrite=True)
    assert result['replaced'] == 1
    assert (site / 'index.php').read_bytes() == b'NEW'


def test_a_file_over_a_folder_is_refused(site):
    archive = _zip(site.parent / 'clash.zip', {'css': b'file now'})
    with pytest.raises(ConflictError):
        ArchiveService.extract(archive, str(site), overwrite=True)
    assert (site / 'css').is_dir()


# ── zip for download ────────────────────────────────────────────────

def test_build_download_and_expiry(site, tmp_path):
    result = ArchiveService.build_download([str(site)], 'job-1')
    path = ArchiveService.download_path('job-1')
    assert result['download_name'] == 'site.zip' and path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    ArchiveService.cleanup_downloads(now=os.path.getmtime(path) + archive_service.DOWNLOAD_TTL + 1)
    assert ArchiveService.download_path('job-1') is None


# ── routes ──────────────────────────────────────────────────────────

def test_compress_route_queues_a_job_the_caller_can_read(client, auth_headers, site):
    resp = client.post('/api/v1/files/compress', headers=auth_headers, json={
        'paths': [str(site)], 'dest': str(site.parent / 'site.zip'), 'format': 'zip'})
    assert resp.status_code == 202
    job_id = resp.get_json()['job_id']
    resp = client.get(f'/api/v1/files/jobs/{job_id}', headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()['kind'] == archive_service.COMPRESS_JOB_KIND


def test_job_routes_hide_other_jobs(client, auth_headers, app):
    from app.jobs.service import JobService
    with app.app_context():
        other = JobService.enqueue(archive_service.COMPRESS_JOB_KIND, payload={},
                                   owner_type='user', owner_id='999').id
        unrelated = JobService.enqueue('monitoring.speedtest.run', payload={}).id
    for job_id in (other, unrelated):
        assert client.get(f'/api/v1/files/jobs/{job_id}', headers=auth_headers).status_code == 404
        assert client.post(f'/api/v1/files/jobs/{job_id}/download-link', headers=auth_headers).status_code == 404


def test_extract_route_validates_before_queueing(client, auth_headers, site):
    resp = client.post('/api/v1/files/extract', headers=auth_headers, json={
        'path': str(site / 'index.php'), 'dest': str(site)})
    assert resp.status_code == 400
