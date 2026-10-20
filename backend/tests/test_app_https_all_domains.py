"""HTTPS for a service covers every one of its domains
(SiteDomainService.enable_app_https). Fork: arya-arav/ServerKit.

A service's domains share one nginx vhost (server_name lists them all), but
"Enable SSL" used to request a certificate for the clicked name only and put it
on that whole vhost, so every alias showed a certificate name mismatch. The
service Settings tab issued one for the primary domain and never wired it into
nginx at all.
"""
import pytest


def _app_with_domains(db, names=('shop.example.com', 'www.shop.example.com')):
    from app.models.domain import Domain
    from factories import make_application
    a = make_application(db, name='shop', root_path='/srv/shop', port=8071)
    for i, name in enumerate(names):
        db.session.add(Domain(name=name, is_primary=(i == 0), application_id=a.id))
    db.session.commit()
    return a


@pytest.fixture
def certbot(monkeypatch):
    """Stub issuance; records each call's arguments."""
    from app.services.ssl_service import SSLService
    calls = []

    def fake_obtain(domains, email, webroot_path=None, use_nginx=True, cert_name=None):
        calls.append({'domains': list(domains), 'cert_name': cert_name})
        lineage = cert_name or domains[0]
        return {'success': True, 'domains': list(domains),
                'certificate_path': f'/etc/letsencrypt/live/{lineage}/fullchain.pem',
                'private_key_path': f'/etc/letsencrypt/live/{lineage}/privkey.pem'}

    monkeypatch.setattr(SSLService, 'obtain_certificate', classmethod(lambda cls, **kw: fake_obtain(**kw)))
    return calls


@pytest.fixture
def vhost(monkeypatch):
    from app.services.site_domain_service import SiteDomainService
    writes = []
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, app, force_type=None: writes.append(app.id) or
                                    {'nginx': {'success': True}, 'warning': None}))
    return writes


def test_one_certificate_names_every_domain_primary_first(app, certbot, vhost, monkeypatch):
    from app import db
    from app.models.domain import Domain
    from app.services.site_domain_service import SiteDomainService
    monkeypatch.setattr(SiteDomainService, '_resolves', staticmethod(lambda name: True))
    a = _app_with_domains(db)

    result = SiteDomainService.enable_app_https(a, 'ops@example.com')

    assert result['success'] is True
    assert certbot == [{'domains': ['shop.example.com', 'www.shop.example.com'],
                        'cert_name': 'shop.example.com'}]
    rows = Domain.query_active().filter_by(application_id=a.id).all()
    assert all(r.ssl_enabled for r in rows)
    assert {r.ssl_certificate_path for r in rows} == {'/etc/letsencrypt/live/shop.example.com/fullchain.pem'}
    assert vhost == [a.id]  # nginx re-rendered right away


def test_names_without_dns_are_skipped_not_fatal(app, certbot, vhost, monkeypatch):
    from app import db
    from app.models.domain import Domain
    from app.services.site_domain_service import SiteDomainService
    monkeypatch.setattr(SiteDomainService, '_resolves', staticmethod(lambda name: not name.startswith('www.')))
    a = _app_with_domains(db)

    result = SiteDomainService.enable_app_https(a, 'ops@example.com')

    assert result['success'] is True
    assert result['domains'] == ['shop.example.com']
    assert result['skipped'] == ['www.shop.example.com']
    alias = Domain.query_active().filter_by(name='www.shop.example.com').first()
    assert alias.ssl_enabled is False


def test_no_resolvable_name_requests_nothing(app, certbot, vhost, monkeypatch):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    monkeypatch.setattr(SiteDomainService, '_resolves', staticmethod(lambda name: False))
    a = _app_with_domains(db)

    result = SiteDomainService.enable_app_https(a, 'ops@example.com')

    assert result['success'] is False and 'DNS' in result['error']
    assert certbot == []


def test_unrenderable_app_falls_back_to_patching_the_site(app, certbot, monkeypatch):
    from app import db
    from app.services.nginx_service import NginxService
    from app.services.site_domain_service import SiteDomainService
    monkeypatch.setattr(SiteDomainService, '_resolves', staticmethod(lambda name: True))
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, app, force_type=None: {'nginx': None, 'warning': 'unsupported type'}))
    patched = []
    monkeypatch.setattr(NginxService, 'add_ssl_to_site',
                        classmethod(lambda cls, name, cert, key: patched.append((name, cert)) or {'success': True}))
    a = _app_with_domains(db)

    result = SiteDomainService.enable_app_https(a, 'ops@example.com')

    assert result['success'] is True and result['warning'] is None
    assert patched == [('shop', '/etc/letsencrypt/live/shop.example.com/fullchain.pem')]


def test_certbot_gets_a_pinned_cert_name(monkeypatch, fake_subprocess):
    from app.services.ssl_service import SSLService
    monkeypatch.setattr(SSLService, 'is_certbot_installed', classmethod(lambda cls: True))
    monkeypatch.setattr(SSLService, '_preflight_nginx_plugin', classmethod(lambda cls, binary: None))
    monkeypatch.setattr(SSLService, 'certbot_bin', classmethod(lambda cls: 'certbot'))
    fake_subprocess.script(['certbot'], returncode=0)

    result = SSLService.obtain_certificate(['a.example.com', 'www.a.example.com'], 'ops@example.com',
                                           cert_name='a.example.com')

    argv = fake_subprocess.argv_for(['certbot'])
    assert argv[argv.index('--cert-name') + 1] == 'a.example.com'
    assert argv.count('-d') == 2
    assert result['certificate_path'].endswith('/live/a.example.com/fullchain.pem')


def test_enable_route_uses_the_app_wide_action(app, client, auth_headers, monkeypatch):
    from app import db
    from app.models.domain import Domain
    from app.services.site_domain_service import SiteDomainService
    a = _app_with_domains(db)
    alias = Domain.query_active().filter_by(name='www.shop.example.com').first()
    seen = []
    monkeypatch.setattr(SiteDomainService, 'enable_app_https',
                        classmethod(lambda cls, app, email: seen.append(app.id) or
                                    {'success': True, 'domains': ['shop.example.com', 'www.shop.example.com'],
                                     'skipped': [], 'certificate': {}, 'warning': None}))

    resp = client.post(f'/api/v1/domains/{alias.id}/ssl/enable', headers=auth_headers,
                       json={'email': 'ops@example.com'})

    assert resp.status_code == 200, resp.get_json()
    assert seen == [a.id]
    assert resp.get_json()['domains_covered'] == ['shop.example.com', 'www.shop.example.com']


def test_disable_route_rerenders_nginx(app, client, auth_headers, monkeypatch):
    from app import db
    from app.api import domains as domains_api
    from app.models.domain import Domain
    a = _app_with_domains(db, names=('shop.example.com',))
    d = Domain.query_active().filter_by(application_id=a.id).first()
    d.ssl_enabled = True
    db.session.commit()
    rendered = []
    monkeypatch.setattr(domains_api, '_publish_app_vhost', lambda app: rendered.append(app.id) or {'success': True})

    resp = client.post(f'/api/v1/domains/{d.id}/ssl/disable', headers=auth_headers)

    assert resp.status_code == 200
    assert rendered == [a.id]


def test_renew_targets_the_shared_lineage(app, client, auth_headers, monkeypatch):
    from app import db
    from app.models.domain import Domain
    from app.services.ssl_service import SSLService
    a = _app_with_domains(db)
    alias = Domain.query_active().filter_by(name='www.shop.example.com').first()
    alias.ssl_enabled = True
    alias.ssl_certificate_path = '/etc/letsencrypt/live/shop.example.com/fullchain.pem'
    db.session.commit()
    renewed = []
    monkeypatch.setattr(SSLService, 'renew_certificate',
                        classmethod(lambda cls, name=None: renewed.append(name) or {'success': True}))

    resp = client.post(f'/api/v1/domains/{alias.id}/ssl/renew', headers=auth_headers)

    assert resp.status_code == 200
    assert renewed == ['shop.example.com']  # not the alias's own name
