"""POST /api/v1/backups/schedules/<id>/run queues one run of a schedule.
Fork: arya-arav/ServerKit."""
import pytest


@pytest.fixture
def schedule(app, tmp_path, monkeypatch):
    from app.services.backup_service import BackupService
    monkeypatch.setattr(BackupService, 'CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(BackupService, 'BACKUP_CONFIG', str(tmp_path / 'backups.json'))
    result = BackupService.add_schedule('nightly', 'files', '/srv/site', '03:00')
    return result.get('schedule') or BackupService.get_config()['schedules'][0]


def test_run_now_queues_a_backup_job(client, auth_headers, schedule):
    from app.jobs.models import Job
    from app.services.backup_service import BACKUP_JOB_KIND

    resp = client.post(f"/api/v1/backups/schedules/{schedule['id']}/run", headers=auth_headers)

    assert resp.status_code == 202, resp.get_json()
    job = Job.query.get(resp.get_json()['job_id'])
    assert job.kind == BACKUP_JOB_KIND
    assert job.get_payload() == {'schedule_id': schedule['id']}


def test_run_now_unknown_schedule_is_404(client, auth_headers, schedule):
    resp = client.post('/api/v1/backups/schedules/nope/run', headers=auth_headers)
    assert resp.status_code == 404


def test_run_now_needs_admin(client, developer_headers, schedule):
    resp = client.post(f"/api/v1/backups/schedules/{schedule['id']}/run", headers=developer_headers)
    assert resp.status_code == 403
