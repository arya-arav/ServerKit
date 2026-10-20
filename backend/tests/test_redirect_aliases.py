"""Redirect a service's other domains to its primary domain (redirect_aliases).
Fork: arya-arav/ServerKit.

All of a service's domains used to serve the same site, so every page existed
at several addresses. With the switch on, the site answers on the primary
domain only and every other domain 301s to the same path there.
"""
import re

from app.services.nginx_service import NginxService

DOMAINS = ['shop.example.com', 'www.shop.example.com', 'shop.example.net']


def _server_blocks(config):
    return re.findall(r'server \{.*?\n\}', config, re.S)


def _render(**kw):
    result = NginxService.render_site_config('shop', 'docker', kw.pop('domains', DOMAINS), port=8071, **kw)
    assert result['success'], result
    return result['config']


def test_without_canonical_every_domain_serves_the_site():
    config = _render()
    assert 'server_name shop.example.com www.shop.example.com shop.example.net;' in config
    assert 'return 301' not in config


def test_http_only_aliases_redirect_to_the_primary():
    config = _render(canonical_domain='shop.example.com')
    blocks = _server_blocks(config)
    site = next(b for b in blocks if 'proxy_pass' in b)
    alias = next(b for b in blocks if 'return 301' in b)
    assert 'server_name shop.example.com;' in site
    assert 'server_name www.shop.example.com shop.example.net;' in alias
    assert 'return 301 http://shop.example.com$request_uri;' in alias


def test_https_aliases_redirect_to_the_primary_on_both_ports():
    config = _render(canonical_domain='shop.example.com',
                     ssl_cert='/etc/letsencrypt/live/shop.example.com/fullchain.pem',
                     ssl_key='/etc/letsencrypt/live/shop.example.com/privkey.pem')
    blocks = _server_blocks(config)
    # :80 sends every name to https://$server_name, which must be the primary.
    http = next(b for b in blocks if 'listen 80;' in b)
    assert 'server_name shop.example.com www.shop.example.com shop.example.net;' in http
    assert 'return 301 https://$server_name$request_uri;' in http
    site = next(b for b in blocks if 'proxy_pass' in b)
    assert 'server_name shop.example.com;' in site and 'listen 443 ssl' in site
    alias = next(b for b in blocks if 'return 301 https://shop.example.com$request_uri;' in b)
    assert 'listen 443 ssl' in alias
    assert 'server_name www.shop.example.com shop.example.net;' in alias
    assert 'ssl_certificate /etc/letsencrypt/live/shop.example.com/fullchain.pem;' in alias


def test_canonical_is_moved_first_even_if_listed_later():
    config = _render(domains=['www.shop.example.com', 'shop.example.com'],
                     canonical_domain='shop.example.com',
                     ssl_cert='/c.pem', ssl_key='/k.pem')
    http = next(b for b in _server_blocks(config) if 'listen 80;' in b)
    assert 'server_name shop.example.com www.shop.example.com;' in http


def test_single_domain_ignores_the_switch():
    config = _render(domains=['shop.example.com'], canonical_domain='shop.example.com')
    assert 'return 301' not in config


def _app(db, redirect=False, names=DOMAINS):
    from app.models.domain import Domain
    from factories import make_application
    a = make_application(db, name='shop', root_path='/srv/shop', port=8071, redirect_aliases=redirect)
    # Primary inserted LAST: ordering must not depend on insertion order.
    for name in reversed(names):
        db.session.add(Domain(name=name, is_primary=(name == names[0]), application_id=a.id))
    db.session.commit()
    return a


def test_vhost_kwargs_put_the_primary_first(app):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    kwargs, _ = SiteDomainService.app_vhost_kwargs(_app(db))
    assert kwargs['domains'][0] == 'shop.example.com'
    assert 'canonical_domain' not in kwargs


def test_vhost_kwargs_pass_the_primary_when_switched_on(app):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    kwargs, _ = SiteDomainService.app_vhost_kwargs(_app(db, redirect=True))
    assert kwargs['canonical_domain'] == 'shop.example.com'


def test_route_saves_the_flag_and_rerenders(app, client, auth_headers, monkeypatch):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    a = _app(db)
    writes = []
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, app, force_type=None: writes.append(app.redirect_aliases) or
                                    {'nginx': {'success': True}, 'warning': None}))

    resp = client.put(f'/api/v1/apps/{a.id}/redirect-aliases', headers=auth_headers, json={'enabled': True})

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['redirect_aliases'] is True
    assert writes == [True]


def test_route_rejects_a_non_boolean(app, client, auth_headers):
    from app import db
    a = _app(db)
    resp = client.put(f'/api/v1/apps/{a.id}/redirect-aliases', headers=auth_headers, json={'enabled': 'yes'})
    assert resp.status_code == 400


def test_changing_the_primary_rerenders_nginx(app, client, auth_headers, monkeypatch):
    from app import db
    from app.api import domains as domains_api
    from app.models.domain import Domain
    a = _app(db)
    other = Domain.query_active().filter_by(name='shop.example.net').first()
    rendered = []
    monkeypatch.setattr(domains_api, '_publish_app_vhost', lambda app: rendered.append(app.id) or {'success': True})

    resp = client.put(f'/api/v1/domains/{other.id}', headers=auth_headers, json={'is_primary': True})

    assert resp.status_code == 200
    assert rendered == [a.id]
