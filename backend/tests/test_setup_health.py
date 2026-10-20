"""Tests for the Setup Health registry (setup_health_service): the probe matrix
over settings fixtures, the critical-vs-recommended severity mapping (HTTPS is
never critical), and inclusion of the section in the doctor report + API."""
import pytest

from app.services.setup_health_service import SetupHealthService


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def set_setting(key, value, vtype='string'):
    from app import db
    from app.models.system_settings import SystemSettings
    SystemSettings.set(key=key, value=value, value_type=vtype)
    db.session.commit()


def add_dns_provider():
    from app import db
    from app.models.email import DNSProviderConfig
    db.session.add(DNSProviderConfig(name='CF', provider='cloudflare', api_key='x'))
    db.session.commit()


def add_email_provider(tested_ok=True):
    from app import db
    from app.models.email_provider import EmailProviderConnection
    row = EmailProviderConnection(name='SMTP', provider='smtp', is_default=True,
                                  is_active=True, uses_notifications=True,
                                  last_test_ok=tested_ok)
    db.session.add(row)
    db.session.commit()
    return row


def add_backup_policy(enabled=True):
    from app import db
    from app.models.backup_policy import BackupPolicy
    row = BackupPolicy(target_type='application', target_id=1, enabled=enabled,
                       schedule_cron='0 3 * * *')
    db.session.add(row)
    db.session.commit()
    return row


def items_by_key(result):
    return {c['key']: c for c in result['items']}


# --------------------------------------------------------------------------- #
# Registry shape / normalization
# --------------------------------------------------------------------------- #

def test_evaluate_returns_normalized_check_shape(app):
    result = SetupHealthService.evaluate()
    assert 'items' in result and 'summary' in result
    for c in result['items']:
        # doctor _check shape …
        assert set(('key', 'title', 'status', 'detail', 'repairable',
                    'repair_ref')).issubset(c)
        assert c['status'] in ('ok', 'warn', 'fail')
        # … plus setup extras
        assert c['section'] == 'setup'
        assert c['severity'] in ('critical', 'recommended')
        assert c['scope'] == 'panel'
        assert c['fix']['kind'] in ('link', 'repair')


def test_summary_counts_and_score(app):
    result = SetupHealthService.evaluate()
    s = result['summary']
    assert s['total'] == len(result['items'])
    assert s['ok'] + s['critical_open'] + s['recommended_open'] == s['total']
    assert 0 <= s['score'] <= 100


# --------------------------------------------------------------------------- #
# Probe matrix — each item ok / not-ok by settings state
# --------------------------------------------------------------------------- #

def test_public_ip_ok_when_set(app):
    set_setting('server_public_ip', '203.0.113.7')
    c = items_by_key(SetupHealthService.evaluate())['setup.public_ip']
    assert c['status'] == 'ok'


def test_public_ip_recommended_when_unset_and_not_per_site(app):
    # No base domain → publishing_gaps short-circuits to no_base_domain, so the
    # missing IP is only a recommendation, not silent breakage.
    c = items_by_key(SetupHealthService.evaluate())['setup.public_ip']
    assert c['status'] == 'warn'
    assert c['severity'] == 'recommended'


def test_public_ip_critical_when_per_site_mode_without_ip(app):
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'per-site')
    c = items_by_key(SetupHealthService.evaluate())['setup.public_ip']
    assert c['status'] == 'fail'
    assert c['severity'] == 'critical'


def test_base_domain_ok_when_set(app):
    set_setting('sites_base_domain', 'apps.example.com')
    c = items_by_key(SetupHealthService.evaluate())['setup.base_domain']
    assert c['status'] == 'ok'


def test_base_domain_recommended_when_unset(app):
    c = items_by_key(SetupHealthService.evaluate())['setup.base_domain']
    assert c['status'] == 'warn'
    assert c['severity'] == 'recommended'


def test_dns_provider_ok_when_connected(app):
    add_dns_provider()
    c = items_by_key(SetupHealthService.evaluate())['setup.dns_provider']
    assert c['status'] == 'ok'


def test_dns_provider_critical_when_per_site_without_provider(app):
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'per-site')
    c = items_by_key(SetupHealthService.evaluate())['setup.dns_provider']
    assert c['status'] == 'fail'
    assert c['severity'] == 'critical'


def test_dns_provider_recommended_when_wildcard_without_provider(app):
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'wildcard')
    c = items_by_key(SetupHealthService.evaluate())['setup.dns_provider']
    assert c['status'] == 'warn'
    assert c['severity'] == 'recommended'


def test_email_delivery_ok_when_tested(app):
    add_email_provider(tested_ok=True)
    c = items_by_key(SetupHealthService.evaluate())['setup.email_delivery']
    assert c['status'] == 'ok'


def test_email_delivery_warn_when_untested(app):
    add_email_provider(tested_ok=False)
    c = items_by_key(SetupHealthService.evaluate())['setup.email_delivery']
    assert c['status'] == 'warn'
    assert c['severity'] == 'recommended'


def test_email_delivery_warn_when_absent(app):
    c = items_by_key(SetupHealthService.evaluate())['setup.email_delivery']
    assert c['status'] == 'warn'


def test_backup_policy_ok_with_enabled_policy(app):
    add_backup_policy(enabled=True)
    c = items_by_key(SetupHealthService.evaluate())['setup.backup_policy']
    assert c['status'] == 'ok'


def test_backup_policy_warn_without_any(app, monkeypatch):
    from app.services.backup_service import BackupService
    monkeypatch.setattr(BackupService, 'get_config', classmethod(lambda cls: {'schedules': []}))
    add_backup_policy(enabled=False)
    c = items_by_key(SetupHealthService.evaluate())['setup.backup_policy']
    assert c['status'] == 'warn'


def test_backup_policy_ok_with_an_enabled_schedule(app, monkeypatch):
    """Fork: schedules from Backups → Schedules count as scheduled backups."""
    from app.services.backup_service import BackupService
    monkeypatch.setattr(BackupService, 'get_config', classmethod(
        lambda cls: {'schedules': [{'name': 'daily', 'enabled': True}]}))
    c = items_by_key(SetupHealthService.evaluate())['setup.backup_policy']
    assert c['status'] == 'ok'


def test_backup_policy_warn_when_every_schedule_is_off(app, monkeypatch):
    from app.services.backup_service import BackupService
    monkeypatch.setattr(BackupService, 'get_config', classmethod(
        lambda cls: {'schedules': [{'name': 'daily', 'enabled': False}]}))
    c = items_by_key(SetupHealthService.evaluate())['setup.backup_policy']
    assert c['status'] == 'warn'


def test_backup_offsite_warn_on_local_only(app):
    c = items_by_key(SetupHealthService.evaluate())['setup.backup_offsite']
    assert c['status'] == 'warn'
    assert c['severity'] == 'recommended'


def test_backup_offsite_ok_with_s3(app, monkeypatch):
    from app.services.storage_provider_service import StorageProviderService
    monkeypatch.setattr(StorageProviderService, 'get_config',
                        classmethod(lambda cls: {'provider': 's3'}))
    c = items_by_key(SetupHealthService.evaluate())['setup.backup_offsite']
    assert c['status'] == 'ok'


def test_canonical_domain_ok_when_set_no_overlap(app):
    set_setting('canonical_domain', 'panel.example.com')
    c = items_by_key(SetupHealthService.evaluate())['setup.canonical_domain']
    assert c['status'] == 'ok'


def test_canonical_domain_warn_when_unset(app):
    c = items_by_key(SetupHealthService.evaluate())['setup.canonical_domain']
    assert c['status'] == 'warn'


# --------------------------------------------------------------------------- #
# Host hardening — firewall (critical) + fail2ban (recommended), plan 73 item 8
#
# These items only exist on a Linux host this panel was installed onto (see
# SetupHealthService._manages_host_firewall). Everything below drives that gate
# explicitly so the assertions don't depend on the machine running the suite.
# --------------------------------------------------------------------------- #

@pytest.fixture
def managed_host(monkeypatch):
    """Pretend this panel owns a real Linux host's firewall."""
    from app.services.setup_health_service import SetupHealthService as S
    monkeypatch.setattr(S, '_manages_host_firewall', classmethod(lambda cls: True))


def fake_firewall(monkeypatch, **status):
    from app.services.firewall_service import FirewallService
    monkeypatch.setattr(FirewallService, 'get_status',
                        classmethod(lambda cls: status))


def fake_fail2ban(monkeypatch, **status):
    from app.services.fail2ban_jail_service import Fail2banJailService
    monkeypatch.setattr(Fail2banJailService, 'get_fail2ban_status',
                        classmethod(lambda cls: status))


def test_firewall_item_absent_on_an_unmanaged_host(app):
    """No /etc/serverkit (dev checkout, CI runner) or a container → the items
    drop out entirely rather than reporting a fake 'ok' that would inflate the
    score or a fake 'fail' the operator cannot act on."""
    keys = items_by_key(SetupHealthService.evaluate())
    assert 'setup.firewall' not in keys
    assert 'setup.fail2ban' not in keys


def test_firewall_critical_when_nothing_is_active(app, managed_host, monkeypatch):
    """The proving assertion: a firewall-less box is flagged CRITICAL."""
    fake_firewall(monkeypatch, any_installed=False, any_active=False,
                  active_firewall=None)
    fake_fail2ban(monkeypatch, installed=False)
    c = items_by_key(SetupHealthService.evaluate())['setup.firewall']
    assert c['status'] == 'fail'
    assert c['severity'] == 'critical'
    assert c['fix'] == {'kind': 'link', 'to': '/security/firewall'}


def test_firewall_ok_when_a_firewall_is_active(app, managed_host, monkeypatch):
    fake_firewall(monkeypatch, any_installed=True, any_active=True,
                  active_firewall='ufw')
    fake_fail2ban(monkeypatch, installed=False)
    c = items_by_key(SetupHealthService.evaluate())['setup.firewall']
    assert c['status'] == 'ok'
    assert 'ufw' in c['detail']


def test_firewall_installed_but_inactive_is_still_critical(app, managed_host, monkeypatch):
    # Installed-and-off filters exactly as much traffic as not installed at all.
    fake_firewall(monkeypatch, any_installed=True, any_active=False,
                  active_firewall='ufw')
    fake_fail2ban(monkeypatch, installed=False)
    c = items_by_key(SetupHealthService.evaluate())['setup.firewall']
    assert c['status'] == 'fail'
    assert c['severity'] == 'critical'


def test_firewall_probe_failure_drops_the_item(app, managed_host, monkeypatch):
    from app.services.firewall_service import FirewallService

    def boom(cls):
        raise OSError('sudo: command not found')

    monkeypatch.setattr(FirewallService, 'get_status', classmethod(boom))
    fake_fail2ban(monkeypatch, installed=False)
    # A probe that cannot answer must never take the whole sweep down, and must
    # never report a firewall it did not observe.
    keys = items_by_key(SetupHealthService.evaluate())
    assert 'setup.firewall' not in keys


def test_fail2ban_absent_is_recommended_not_critical(app, managed_host, monkeypatch):
    """The panel already throttles its own logins per-IP, so a missing fail2ban
    is defence-in-depth, not silent breakage."""
    fake_firewall(monkeypatch, any_active=True, active_firewall='ufw')
    fake_fail2ban(monkeypatch, installed=False, service_running=False, jails=[])
    c = items_by_key(SetupHealthService.evaluate())['setup.fail2ban']
    assert c['status'] == 'warn'
    assert c['severity'] == 'recommended'
    assert c['fix'] == {'kind': 'link', 'to': '/security/fail2ban'}


def test_fail2ban_installed_but_stopped_warns(app, managed_host, monkeypatch):
    fake_firewall(monkeypatch, any_active=True, active_firewall='ufw')
    fake_fail2ban(monkeypatch, installed=True, service_running=False, jails=[])
    c = items_by_key(SetupHealthService.evaluate())['setup.fail2ban']
    assert c['status'] == 'warn'


def test_fail2ban_running_without_an_ssh_jail_warns(app, managed_host, monkeypatch):
    # Running with only a per-site WordPress jail leaves sshd unprotected —
    # which is the jail the installer now writes.
    fake_firewall(monkeypatch, any_active=True, active_firewall='ufw')
    fake_fail2ban(monkeypatch, installed=True, service_running=True,
                  jails=['serverkit-blog'])
    c = items_by_key(SetupHealthService.evaluate())['setup.fail2ban']
    assert c['status'] == 'warn'
    assert 'SSH' in c['detail']


def test_fail2ban_ok_with_the_sshd_jail(app, managed_host, monkeypatch):
    fake_firewall(monkeypatch, any_active=True, active_firewall='ufw')
    fake_fail2ban(monkeypatch, installed=True, service_running=True,
                  jails=['sshd', 'serverkit-blog'])
    c = items_by_key(SetupHealthService.evaluate())['setup.fail2ban']
    assert c['status'] == 'ok'


def test_hardening_items_are_snoozable(app, managed_host, monkeypatch):
    """A box whose firewall lives at the provider edge must be able to mute the
    critical — otherwise the weekly nag becomes permanent noise. Snoozing needs
    the key registered in _ITEM_SCOPES."""
    fake_firewall(monkeypatch, any_active=False, active_firewall=None)
    fake_fail2ban(monkeypatch, installed=False)
    assert SetupHealthService.snooze('setup.firewall', days=30).get('success')
    assert SetupHealthService.snooze('setup.fail2ban', days=30).get('success')
    result = SetupHealthService.evaluate()
    assert items_by_key(result)['setup.firewall']['snoozed'] is True
    assert result['summary']['critical_open'] == 0


def test_firewall_critical_shows_up_in_the_fingerprint(app, managed_host, monkeypatch):
    """The nag has to be able to see it — a critical that never reaches the
    fingerprint never notifies anyone."""
    fake_firewall(monkeypatch, any_active=False, active_firewall=None)
    fake_fail2ban(monkeypatch, installed=False)
    assert 'setup.firewall' in SetupHealthService.fingerprint()


def test_manages_host_firewall_gate_is_conservative(app, monkeypatch):
    """The gate itself: Linux + install marker + not a container, all three."""
    import app.services.setup_health_service as m
    S = m.SetupHealthService
    monkeypatch.setattr(m.sys, 'platform', 'linux')
    monkeypatch.setattr(m.os.path, 'isdir', lambda p: p == m.HOST_CONFIG_DIR)

    monkeypatch.setattr(S, '_in_container', staticmethod(lambda: False))
    assert S._manages_host_firewall() is True

    # A container guest does not own the host's netfilter.
    monkeypatch.setattr(S, '_in_container', staticmethod(lambda: True))
    assert S._manages_host_firewall() is False

    # Neither does a dev checkout on Windows/macOS.
    monkeypatch.setattr(S, '_in_container', staticmethod(lambda: False))
    monkeypatch.setattr(m.sys, 'platform', 'win32')
    assert S._manages_host_firewall() is False

    # …nor a Linux box install.sh never touched.
    monkeypatch.setattr(m.sys, 'platform', 'linux')
    monkeypatch.setattr(m.os.path, 'isdir', lambda p: False)
    assert S._manages_host_firewall() is False


# --------------------------------------------------------------------------- #
# Severity mapping — HTTPS is NEVER critical (SSL optional by decree)
# --------------------------------------------------------------------------- #

def test_wildcard_cert_absent_when_https_off(app):
    # Wildcard cert item is only applicable once wildcard HTTPS is switched on.
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'wildcard')
    keys = items_by_key(SetupHealthService.evaluate())
    assert 'setup.wildcard_cert' not in keys


def test_wildcard_cert_never_critical_when_missing(app, monkeypatch):
    import sys
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'wildcard')
    set_setting('sites_https_enabled', 'true', vtype='boolean')
    # Pretend we're on the Linux host with the cert absent.
    monkeypatch.setattr(sys, 'platform', 'linux')
    import app.services.setup_health_service as m
    monkeypatch.setattr(m.os.path, 'exists', lambda p: False)
    c = items_by_key(SetupHealthService.evaluate())['setup.wildcard_cert']
    assert c['status'] == 'warn'            # NOT 'fail'
    assert c['severity'] == 'recommended'   # HTTPS never critical


def test_no_https_item_is_ever_critical(app, monkeypatch):
    """Across every settings permutation, an HTTPS/TLS item can never come back
    critical — the SSL-optional invariant, asserted directly."""
    import sys
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'wildcard')
    set_setting('sites_https_enabled', 'true', vtype='boolean')
    monkeypatch.setattr(sys, 'platform', 'linux')
    import app.services.setup_health_service as m
    monkeypatch.setattr(m.os.path, 'exists', lambda p: False)
    for c in SetupHealthService.evaluate()['items']:
        if 'cert' in c['key'] or 'https' in c['key']:
            assert c['severity'] != 'critical'


# --------------------------------------------------------------------------- #
# Doctor-report inclusion + API
# --------------------------------------------------------------------------- #

def test_doctor_report_includes_setup_section(app, monkeypatch):
    from app.services.doctor_service import DoctorService
    # Keep the sweep cheap/offline — only care that setup.* is present.
    monkeypatch.setattr(DoctorService, '_drift_checks', classmethod(lambda cls: []))
    monkeypatch.setattr(DoctorService, '_service_checks', classmethod(lambda cls: []))
    monkeypatch.setattr(DoctorService, '_dns_checks', classmethod(lambda cls: []))
    report = DoctorService.run()
    setup_keys = [c['key'] for c in report['checks'] if c['key'].startswith('setup.')]
    assert 'setup.public_ip' in setup_keys
    assert 'setup.dns_provider' in setup_keys


def test_api_returns_items_and_summary(app, client, auth_headers):
    res = client.get('/api/v1/setup-health', headers=auth_headers)
    assert res.status_code == 200
    body = res.get_json()
    assert 'items' in body and 'summary' in body
    assert body['summary']['total'] == len(body['items'])


def test_api_requires_admin(app, client):
    res = client.get('/api/v1/setup-health')
    assert res.status_code in (401, 422)


def test_fingerprint_changes_only_with_critical_set(app):
    # Fresh panel: no critical items (no per-site breakage) → empty fingerprint.
    fp_clean = SetupHealthService.fingerprint()
    set_setting('sites_base_domain', 'apps.example.com')
    set_setting('sites_dns_mode', 'per-site')
    fp_broken = SetupHealthService.fingerprint()
    assert fp_clean != fp_broken
    assert 'setup.public_ip' in fp_broken
