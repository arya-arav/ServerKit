"""The file integrity checker ignores its own database, and an accepted
baseline clears the security posture's 24h integrity count at once.
Fork: arya-arav/ServerKit."""
import json
from datetime import datetime, timedelta

import pytest

from app.services.security_service import SecurityService


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """A monitored /etc that also holds the integrity database (as in production)."""
    root = tmp_path / 'etc'
    (root / 'serverkit').mkdir(parents=True)
    (root / 'app.conf').write_text('a=1\n')
    monkeypatch.setattr(SecurityService, 'CONFIG_DIR', str(root / 'serverkit'))
    monkeypatch.setattr(SecurityService, 'INTEGRITY_DB', str(root / 'serverkit' / 'file_integrity.json'))
    monkeypatch.setattr(SecurityService, 'get_config',
                        classmethod(lambda cls: {'file_integrity': {'monitored_paths': [str(root)]}}))
    monkeypatch.setattr(SecurityService, '_log_alert', classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(SecurityService, '_send_security_notification', classmethod(lambda cls, *a, **k: None))
    return root


def test_a_fresh_baseline_reports_no_changes(etc):
    SecurityService.initialize_integrity_database()

    result = SecurityService.check_file_integrity()

    assert result['success'], result
    assert all(not v for v in result['changes'].values()), result['changes']


def test_re_baselining_does_not_report_the_database_as_modified(etc):
    SecurityService.initialize_integrity_database()
    SecurityService.initialize_integrity_database()

    result = SecurityService.check_file_integrity()

    assert all(not v for v in result['changes'].values()), result['changes']


def test_real_changes_are_still_reported(etc):
    SecurityService.initialize_integrity_database()
    (etc / 'app.conf').write_text('a=2\n')

    result = SecurityService.check_file_integrity()

    assert [m['path'] for m in result['changes']['modified']] == [str(etc / 'app.conf')]


def _summary_with_events(monkeypatch, events):
    monkeypatch.setattr(SecurityService, 'get_security_events',
                        classmethod(lambda cls, limit=100: {'events': events}))
    return SecurityService.get_security_summary()['recent_alerts']


def test_integrity_events_before_the_baseline_are_not_counted(etc, monkeypatch):
    before = (datetime.now() - timedelta(hours=2)).isoformat()
    SecurityService.initialize_integrity_database()        # accepted after the event
    after = (datetime.now() + timedelta(seconds=1)).isoformat()

    alerts = _summary_with_events(monkeypatch, [
        {'timestamp': before, 'type': 'integrity'},
        {'timestamp': after, 'type': 'integrity'},
        {'timestamp': before, 'type': 'malware'},
    ])

    assert alerts['integrity_changes'] == 1
    assert alerts['malware_detections'] == 1


def test_without_a_baseline_the_24h_window_still_applies(etc, monkeypatch):
    alerts = _summary_with_events(monkeypatch, [
        {'timestamp': (datetime.now() - timedelta(hours=2)).isoformat(), 'type': 'integrity'},
        {'timestamp': (datetime.now() - timedelta(hours=30)).isoformat(), 'type': 'integrity'},
    ])

    assert alerts['integrity_changes'] == 1
