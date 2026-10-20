"""Tests for the doctor's site-DNS sweep: detection of domains that no longer
resolve, one-click repair via a connected provider, and the newly-broken
admin notification (doctor_service._dns_checks / _repair_dns / run_doctor_job)."""
import json
import socket

import pytest

from app.services import doctor_service
from app.services.doctor_service import (
    DNS_CHECK_MAX_DOMAINS,
    DoctorService,
    LAST_REPORT_KEY,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def site(app):
    """An Application to hang test Domain rows off."""
    from werkzeug.security import generate_password_hash
    from app import db
    from app.models import User
    from app.models.application import Application

    user = User(email='dns@test.local', username='dnsuser',
                password_hash=generate_password_hash('x'),
                role=User.ROLE_ADMIN, is_active=True)
    db.session.add(user)
    db.session.flush()
    row = Application(name='dnssite', app_type='static', user_id=user.id)
    db.session.add(row)
    db.session.commit()
    return row


def add_domain(site, name):
    from app import db
    from app.models.domain import Domain
    db.session.add(Domain(name=name, application_id=site.id))
    db.session.commit()


def add_provider():
    from app import db
    from app.models.email import DNSProviderConfig
    db.session.add(DNSProviderConfig(name='CF', provider='cloudflare', api_key='x'))
    db.session.commit()


def fake_resolver(table):
    """A _resolve_host_ips stand-in: ``table[host]`` is a list of IPs, or an
    exception instance to raise."""
    def resolve(host):
        result = table[host]
        if isinstance(result, Exception):
            raise result
        return result
    return resolve


def set_server_ip(monkeypatch, ip):
    from app.services.site_domain_service import SiteDomainService
    monkeypatch.setattr(SiteDomainService, 'server_ip', classmethod(lambda cls: ip))


def checks_by_key(checks):
    return {c['key']: c for c in checks}


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_no_domains_is_a_single_ok_check(app):
    checks = DoctorService._dns_checks()
    assert len(checks) == 1
    assert checks[0]['key'] == 'dns.resolve'
    assert checks[0]['status'] == 'ok'


def test_dev_and_ip_hostnames_are_skipped(app, site, monkeypatch):
    for name in ('mysite.lvh.me', 'localhost', '10.0.0.5', 'noDotHost',
                 'box.internal', 'demo.test'):
        add_domain(site, name)
    set_server_ip(monkeypatch, None)
    checks = DoctorService._dns_checks()
    assert len(checks) == 1
    assert checks[0]['status'] == 'ok'
    assert 'No public site domains' in checks[0]['detail']


def test_unresolved_with_provider_and_ip_is_repairable(app, site, monkeypatch):
    add_domain(site, 'gone.example.com')
    add_provider()
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(doctor_service, '_resolve_host_ips',
                        fake_resolver({'gone.example.com': socket.gaierror(socket.EAI_NONAME, 'Name or service not known')}))

    checks = checks_by_key(DoctorService._dns_checks())
    c = checks['dns.resolve.gone.example.com']
    assert c['status'] == 'fail'
    assert c['repairable'] is True
    assert c['repair_ref'] == {'kind': 'dns', 'host': 'gone.example.com'}
    assert '203.0.113.7' in c['detail']


def test_unresolved_without_provider_gives_manual_instructions(app, site, monkeypatch):
    add_domain(site, 'gone.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(doctor_service, '_resolve_host_ips',
                        fake_resolver({'gone.example.com': socket.gaierror(socket.EAI_NONAME, 'Name or service not known')}))

    c = checks_by_key(DoctorService._dns_checks())['dns.resolve.gone.example.com']
    assert c['status'] == 'fail'
    assert c['repairable'] is False
    assert 'connect a DNS provider' in c['detail']
    assert 'gone.example.com → 203.0.113.7' in c['detail']


def test_unresolved_without_server_ip_points_at_settings(app, site, monkeypatch):
    add_domain(site, 'gone.example.com')
    add_provider()
    set_server_ip(monkeypatch, None)
    monkeypatch.setattr(doctor_service, '_resolve_host_ips',
                        fake_resolver({'gone.example.com': socket.gaierror(socket.EAI_NONAME, 'Name or service not known')}))

    c = checks_by_key(DoctorService._dns_checks())['dns.resolve.gone.example.com']
    assert c['status'] == 'fail'
    assert c['repairable'] is False
    assert 'server public IP' in c['detail']


def test_resolving_domains_ok_or_warn_by_target(app, site, monkeypatch):
    add_domain(site, 'here.example.com')
    add_domain(site, 'elsewhere.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(doctor_service, '_resolve_host_ips', fake_resolver({
        'here.example.com': ['203.0.113.7'],
        'elsewhere.example.com': ['198.51.100.1'],
    }))

    checks = checks_by_key(DoctorService._dns_checks())
    assert checks['dns.resolve.here.example.com']['status'] == 'ok'
    other = checks['dns.resolve.elsewhere.example.com']
    assert other['status'] == 'warn'
    assert '198.51.100.1' in other['detail']


def test_domain_cap_is_reported(app, site, monkeypatch):
    for i in range(DNS_CHECK_MAX_DOMAINS + 2):
        add_domain(site, f'site{i}.example.com')
    set_server_ip(monkeypatch, None)
    monkeypatch.setattr(
        doctor_service, '_resolve_host_ips', lambda host: ['198.51.100.1'])

    checks = DoctorService._dns_checks()
    assert len(checks) == DNS_CHECK_MAX_DOMAINS + 1
    cap = [c for c in checks if c['key'] == 'dns.resolve'][0]
    assert cap['status'] == 'warn'
    assert '2 more domain(s)' in cap['detail']


# --------------------------------------------------------------------------- #
# Repair
# --------------------------------------------------------------------------- #

def test_repair_dns_creates_record_via_provider(app, site, monkeypatch):
    from app.services.dns_provider_service import DNSProviderService

    add_domain(site, 'gone.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    calls = []
    monkeypatch.setattr(
        DNSProviderService, 'ensure_a_record',
        classmethod(lambda cls, d, ip: calls.append((d, ip)) or
                    {'created': True, 'provider': 'CF',
                     'record': {'type': 'A', 'name': d, 'value': ip}}))

    results = DoctorService.repair([{'kind': 'dns', 'host': 'gone.example.com'}])

    assert calls == [('gone.example.com', '203.0.113.7')]
    assert results[0]['success'] is True
    assert results[0]['record']['name'] == 'gone.example.com'


def test_repair_dns_rejects_unmanaged_hostname(app, site, monkeypatch):
    from app.services.dns_provider_service import DNSProviderService

    add_domain(site, 'gone.example.com')
    monkeypatch.setattr(
        DNSProviderService, 'ensure_a_record',
        classmethod(lambda cls, d, ip: pytest.fail('must not reach the provider')))

    result = DoctorService._repair_dns('attacker.example.net')
    assert result['success'] is False
    assert 'Not a managed site domain' in result['error']


def test_repair_dns_surfaces_provider_degradation(app, site, monkeypatch):
    from app.services.dns_provider_service import DNSProviderService

    add_domain(site, 'gone.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(
        DNSProviderService, 'ensure_a_record',
        classmethod(lambda cls, d, ip: {
            'created': False, 'reason': 'no_provider',
            'message': f'No connected DNS provider manages {d} — add this record manually.'}))

    result = DoctorService._repair_dns('gone.example.com')
    assert result['success'] is False
    assert 'add this record manually' in result['error']


# --------------------------------------------------------------------------- #
# Notification (job)
# --------------------------------------------------------------------------- #

class _JobStub:
    def get_payload(self):
        return {}


def _stub_other_checks(monkeypatch):
    monkeypatch.setattr(DoctorService, '_drift_checks', classmethod(lambda cls: []))
    monkeypatch.setattr(DoctorService, '_service_checks', classmethod(lambda cls: []))


def test_doctor_job_notifies_only_newly_broken_domains(app, site, monkeypatch):
    import app.plugins_sdk as sdk
    from app.services.settings_service import SettingsService

    add_domain(site, 'old-broken.example.com')
    add_domain(site, 'new-broken.example.com')
    add_provider()
    set_server_ip(monkeypatch, '203.0.113.7')
    _stub_other_checks(monkeypatch)
    monkeypatch.setattr(doctor_service, '_resolve_host_ips', fake_resolver({
        'old-broken.example.com': socket.gaierror(socket.EAI_NONAME, 'Name or service not known'),
        'new-broken.example.com': socket.gaierror(socket.EAI_NONAME, 'Name or service not known'),
    }))

    # Previous report already knew about old-broken.
    SettingsService.set(LAST_REPORT_KEY, json.dumps({'checks': [
        {'key': 'dns.resolve.old-broken.example.com', 'status': 'fail'},
    ]}))

    sent = []
    monkeypatch.setattr(sdk.notify, 'send',
                        lambda event, to, data=None, **kw: sent.append((event, to, data)))

    summary = DoctorService.run_doctor_job(_JobStub())

    assert summary['dns_new_failures'] == ['new-broken.example.com']
    assert len(sent) == 1
    event, to, data = sent[0]
    assert event == 'dns.unresolved'
    assert to == 'admins'
    assert data['count'] == 1
    assert data['domains'] == ['new-broken.example.com']

    # Second sweep: nothing new — no re-alert.
    sent.clear()
    summary = DoctorService.run_doctor_job(_JobStub())
    assert summary['dns_new_failures'] == []
    assert sent == []


def test_doctor_job_no_notification_when_all_resolve(app, site, monkeypatch):
    import app.plugins_sdk as sdk

    add_domain(site, 'fine.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    _stub_other_checks(monkeypatch)
    monkeypatch.setattr(doctor_service, '_resolve_host_ips',
                        fake_resolver({'fine.example.com': ['203.0.113.7']}))

    sent = []
    monkeypatch.setattr(sdk.notify, 'send', lambda *a, **kw: sent.append(a))

    DoctorService.run_doctor_job(_JobStub())
    assert sent == []


def test_dns_event_in_notification_catalog():
    from app.notifications import catalog
    entry = catalog.get('dns.unresolved')
    assert entry is not None
    assert entry['severity'] == 'warning'
    assert entry['category'] == 'system'


def test_doctor_schedule_is_seeded(app):
    from app.jobs.builtin_handlers import seed_builtin_schedules
    from app.jobs.models import ScheduledJob
    from app.services.doctor_service import DOCTOR_JOB_KIND, DOCTOR_SCHEDULE_NAME

    seed_builtin_schedules()
    row = ScheduledJob.query.filter_by(name=DOCTOR_SCHEDULE_NAME).first()
    assert row is not None
    assert row.kind == DOCTOR_JOB_KIND
    assert row.interval_seconds == 86400


# --------------------------------------------------------------------------- #
# Cloudflare-proxied domains (fork: arya-arav/ServerKit)
# --------------------------------------------------------------------------- #

def test_cloudflare_proxied_domain_is_ok_not_wrong_server(app, site, monkeypatch):
    add_domain(site, 'proxied.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(doctor_service, '_resolve_host_ips', fake_resolver({
        'proxied.example.com': ['104.21.55.11', '172.67.143.233',
                                '2606:4700:3030::6815:370b'],
    }))
    c = checks_by_key(DoctorService._dns_checks())['dns.resolve.proxied.example.com']
    assert c['status'] == 'ok'
    assert 'Cloudflare' in c['detail']


def test_mixed_cloudflare_and_other_ips_still_warns(app, site, monkeypatch):
    add_domain(site, 'mixed.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(doctor_service, '_resolve_host_ips', fake_resolver({
        'mixed.example.com': ['104.21.55.11', '198.51.100.9'],
    }))
    c = checks_by_key(DoctorService._dns_checks())['dns.resolve.mixed.example.com']
    assert c['status'] == 'warn'
    assert 'not this server' in c['detail']


def test_unrelated_ip_still_warns(app, site, monkeypatch):
    add_domain(site, 'elsewhere.example.com')
    set_server_ip(monkeypatch, '203.0.113.7')
    monkeypatch.setattr(doctor_service, '_resolve_host_ips', fake_resolver({
        'elsewhere.example.com': ['198.51.100.9'],
    }))
    c = checks_by_key(DoctorService._dns_checks())['dns.resolve.elsewhere.example.com']
    assert c['status'] == 'warn'
