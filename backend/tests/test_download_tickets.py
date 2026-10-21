"""One-time download links. Fork: arya-arav/ServerKit.

Upstream opened downloads as /files/download?token=<login JWT>: the token
leaked into logs and history, and the API never read it, so every download
401'd. Now the page gets a signed, 60-second, single-use ticket.
"""
import time

import pytest

from app.services import archive_service, download_ticket_service
from app.services.archive_service import ArchiveService
from app.services.file_service import FileService


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'srv' / 'site'
    root.mkdir(parents=True)
    (root / 'index.php').write_text('<?php echo 1;\n')
    monkeypatch.setattr(FileService, 'ALLOWED_ROOTS', [str(tmp_path / 'srv')])
    monkeypatch.setattr(FileService, 'READ_ONLY_ROOTS', [])
    monkeypatch.setattr(archive_service, 'DOWNLOAD_DIR', str(tmp_path / 'downloads'))
    return root


def _ticket(client, headers, path):
    resp = client.post('/api/v1/files/download-link', headers=headers, json={'path': path})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['ticket']


def test_a_ticket_downloads_once_without_a_login_header(client, auth_headers, site):
    ticket = _ticket(client, auth_headers, str(site / 'index.php'))
    resp = client.get(f'/api/v1/files/fetch/{ticket}')
    assert resp.status_code == 200
    assert resp.data == b'<?php echo 1;\n'
    assert 'attachment' in resp.headers['Content-Disposition']
    assert client.get(f'/api/v1/files/fetch/{ticket}').status_code == 404


def test_forged_and_expired_tickets_are_refused(client, auth_headers, site, monkeypatch):
    ticket = _ticket(client, auth_headers, str(site / 'index.php'))
    assert client.get(f'/api/v1/files/fetch/{ticket[:-2]}xx').status_code == 404
    real_time = time.time
    monkeypatch.setattr('time.time', lambda: real_time() + download_ticket_service.TTL + 5)
    assert client.get(f'/api/v1/files/fetch/{ticket}').status_code == 404


def test_link_needs_login_and_an_allowed_file(client, auth_headers, site, tmp_path):
    assert client.post('/api/v1/files/download-link', json={'path': str(site / 'index.php')}).status_code == 401
    outside = tmp_path / 'secret.txt'
    outside.write_text('x')
    assert client.post('/api/v1/files/download-link', headers=auth_headers,
                       json={'path': str(outside)}).status_code == 403
    assert client.post('/api/v1/files/download-link', headers=auth_headers,
                       json={'path': str(site)}).status_code == 404


def test_a_zip_job_ticket_is_bound_to_its_owner(client, auth_headers, app, site):
    from app.jobs.service import JobService
    from app.jobs.models import Job
    from app import db
    with app.app_context():
        mine = JobService.enqueue(archive_service.DOWNLOAD_JOB_KIND, payload={},
                                  owner_type='user', owner_id='999')
        ArchiveService.build_download([str(site)], mine.id)
        mine.status = Job.STATUS_SUCCEEDED
        mine.set_result({'download_name': 'site.zip'})
        db.session.commit()
        forged = download_ticket_service.issue('1', 'job', mine.id)   # not the owner
        owner = download_ticket_service.issue('999', 'job', mine.id)
    assert client.get(f'/api/v1/files/fetch/{forged}').status_code == 404
    resp = client.get(f'/api/v1/files/fetch/{owner}')
    assert resp.status_code == 200 and resp.data[:2] == b'PK'
