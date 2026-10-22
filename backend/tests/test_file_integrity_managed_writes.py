"""The panel's own config writes don't raise file-integrity alerts; nothing else changes.

Fork: arya-arav/ServerKit. Adding a domain re-renders the nginx vhost, which
the monitor then reported as "changes detected". Only the entries for the
files the panel wrote move to the new state: a hand edit elsewhere, or a
later edit to the same file, still alerts.
"""
import os

import pytest

from app.services.file_integrity_service import FileIntegrityService
from app.utils import system as system_utils


@pytest.fixture
def fim(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    available = tmp_path / 'sites-available'
    sites = tmp_path / 'sites-enabled'
    confd = tmp_path / 'conf.d'
    for d in (available, sites, confd):
        d.mkdir()
    monkeypatch.setattr(FileIntegrityService, 'STATE_DIR', str(state))
    monkeypatch.setattr(FileIntegrityService, 'NGINX_ROOTS', [str(sites), str(confd)])
    monkeypatch.setattr(FileIntegrityService, 'SYSTEMD_ROOT', str(tmp_path / 'no-systemd'))
    (available / 'shop').write_text('server { server_name shop.example.com; }')
    os.symlink(available / 'shop', sites / 'shop')
    (confd / 'gzip.conf').write_text('gzip on;')
    FileIntegrityService.baseline('nginx')
    return {'available': available, 'sites': sites, 'confd': confd}


def _changes():
    r = FileIntegrityService.check('nginx')
    return r['added'] + r['removed'] + [m['path'] for m in r['modified']]


def test_a_panel_rewrite_through_the_enabled_link_is_not_reported(fim):
    (fim['available'] / 'shop').write_text('server { server_name shop.example.com app.example.com; }')
    updated = FileIntegrityService.record_managed_write(str(fim['available'] / 'shop'))
    assert updated == ['nginx:sites-enabled/shop']
    assert _changes() == []


def test_hand_edits_elsewhere_still_alert(fim):
    (fim['confd'] / 'gzip.conf').write_text('gzip off;  # changed by hand')
    (fim['available'] / 'shop').write_text('server { listen 81; }')
    FileIntegrityService.record_managed_write(str(fim['available'] / 'shop'))
    assert _changes() == ['conf.d/gzip.conf']


def test_an_edit_after_the_panel_write_still_alerts(fim):
    FileIntegrityService.record_managed_write(str(fim['available'] / 'shop'))
    (fim['available'] / 'shop').write_text('server { return 301 https://evil.example; }')
    assert _changes() == ['sites-enabled/shop']


def test_enable_and_delete_are_recorded(fim):
    (fim['available'] / 'blog').write_text('server { server_name blog.example.com; }')
    os.symlink(fim['available'] / 'blog', fim['sites'] / 'blog')
    FileIntegrityService.record_managed_write(str(fim['sites'] / 'blog'))
    assert _changes() == []

    os.remove(fim['sites'] / 'shop')
    FileIntegrityService.record_managed_write(str(fim['sites'] / 'shop'))
    assert _changes() == []


def test_without_a_baseline_nothing_happens(tmp_path, monkeypatch):
    monkeypatch.setattr(FileIntegrityService, 'STATE_DIR', str(tmp_path / 'none'))
    assert FileIntegrityService.record_managed_write('/etc/nginx/sites-available/x') == []


def test_write_helper_reports_only_watched_paths(monkeypatch):
    seen = []
    monkeypatch.setattr(FileIntegrityService, 'record_managed_write', lambda *p: seen.extend(p))
    system_utils.note_managed_write('/etc/nginx/sites-available/shop', '/var/www/site/index.php')
    system_utils.note_managed_write('/home/user/notes.txt')
    assert seen == ['/etc/nginx/sites-available/shop']
