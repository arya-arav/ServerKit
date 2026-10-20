"""A custom domain's own certificate survives a vhost re-render
(SiteDomainService.app_vhost_kwargs / own_domain_cert_paths). Fork: arya-arav/ServerKit.

Before this, doctor/drift repair rewrote such a vhost as HTTP-only and HTTPS
requests fell through to the panel's default server."""


def _mk_app_with_domain(db, cert=None, key=None, ssl_enabled=True):
    from app.models.domain import Domain
    from factories import make_application
    a = make_application(db, name='shop', root_path='/srv/shop', port=8071)
    db.session.add(Domain(name='shop.example.com', is_primary=True, application_id=a.id,
                          ssl_enabled=ssl_enabled, ssl_certificate_path=cert, ssl_key_path=key))
    db.session.commit()
    return a


def _cert_files(tmp_path, with_key=True):
    live = tmp_path / 'live' / 'shop.example.com'
    live.mkdir(parents=True)
    (live / 'fullchain.pem').write_text('cert')
    if with_key:
        (live / 'privkey.pem').write_text('key')
    return str(live / 'fullchain.pem'), str(live / 'privkey.pem')


def test_render_keeps_the_domains_own_certificate(app, tmp_path):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    cert, key = _cert_files(tmp_path)
    a = _mk_app_with_domain(db, cert=cert, key=key)
    kwargs, warning = SiteDomainService.app_vhost_kwargs(a)
    assert warning is None
    assert kwargs['ssl_cert'] == cert
    assert kwargs['ssl_key'] == key


def test_missing_key_path_falls_back_to_privkey_next_to_cert(app, tmp_path):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    cert, key = _cert_files(tmp_path)
    a = _mk_app_with_domain(db, cert=cert, key=None)
    kwargs, _ = SiteDomainService.app_vhost_kwargs(a)
    assert (kwargs['ssl_cert'], kwargs['ssl_key']) == (cert, key)


def test_missing_files_render_http_only(app, tmp_path):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    cert, _ = _cert_files(tmp_path, with_key=False)
    a = _mk_app_with_domain(db, cert=cert, key=None)
    kwargs, _ = SiteDomainService.app_vhost_kwargs(a)
    assert kwargs['ssl_cert'] is None and kwargs['ssl_key'] is None


def test_ssl_disabled_domain_renders_http_only(app, tmp_path):
    from app import db
    from app.services.site_domain_service import SiteDomainService
    cert, key = _cert_files(tmp_path)
    a = _mk_app_with_domain(db, cert=cert, key=key, ssl_enabled=False)
    kwargs, _ = SiteDomainService.app_vhost_kwargs(a)
    assert kwargs['ssl_cert'] is None
