"""Move a domain to another service (POST /api/v1/domains/<id>/move), and keep
a primary domain when the old one is deleted or moved. Fork: arya-arav/ServerKit.
"""
import pytest


@pytest.fixture
def published(monkeypatch):
    """Record vhost re-renders instead of touching nginx."""
    from app.api import domains as domains_api
    seen = []
    monkeypatch.setattr(domains_api, '_publish_app_vhost', lambda app: seen.append(app.name) or {'success': True})
    monkeypatch.setattr(domains_api.NginxService, 'disable_site', classmethod(lambda cls, name: None))
    monkeypatch.setattr(domains_api.NginxService, 'delete_site',
                        classmethod(lambda cls, name: seen.append(f'deleted:{name}') or {'success': True}))
    return seen


def _apps(db, owner_id=None):
    from app.models.domain import Domain
    from factories import make_application
    extra = {'user_id': owner_id} if owner_id else {}
    shop = make_application(db, name='shop', port=8071, **extra)
    blog = make_application(db, name='blog', port=8072, **extra)
    db.session.add(Domain(name='shop.example.com', is_primary=True, application_id=shop.id,
                          ssl_enabled=True, ssl_certificate_path='/etc/letsencrypt/live/shop.example.com/fullchain.pem',
                          ssl_key_path='/etc/letsencrypt/live/shop.example.com/privkey.pem'))
    db.session.add(Domain(name='old.example.com', is_primary=False, application_id=shop.id))
    db.session.commit()
    return shop, blog


def _domain(name):
    from app.models.domain import Domain
    return Domain.query_active().filter_by(name=name).first()


def test_move_to_an_empty_service_makes_it_primary_and_republishes_both(app, client, auth_headers, published):
    from app import db
    shop, blog = _apps(db)
    old = _domain('old.example.com')

    resp = client.post(f'/api/v1/domains/{old.id}/move', headers=auth_headers, json={'application_id': blog.id})

    assert resp.status_code == 200, resp.get_json()
    moved = _domain('old.example.com')
    assert moved.application_id == blog.id and moved.is_primary is True
    assert published == ['shop', 'blog']  # source re-rendered without it, target with it
    assert 'ssl_notice' not in resp.get_json()


def test_moving_the_primary_promotes_another_and_drops_its_https(app, client, auth_headers, published):
    from app import db
    from app.models.domain import Domain
    shop, blog = _apps(db)
    db.session.add(Domain(name='blog.example.com', is_primary=True, application_id=blog.id))
    db.session.commit()
    primary = _domain('shop.example.com')

    resp = client.post(f'/api/v1/domains/{primary.id}/move', headers=auth_headers, json={'application_id': blog.id})

    assert resp.status_code == 200
    moved = _domain('shop.example.com')
    # Target already had a primary; its certificate belonged to the old service.
    assert moved.is_primary is False
    assert moved.ssl_enabled is False and moved.ssl_certificate_path is None
    assert 'Enable SSL' in resp.get_json()['ssl_notice']
    assert _domain('old.example.com').is_primary is True  # source keeps a primary


def test_moving_the_last_domain_takes_the_source_site_down(app, client, auth_headers, published):
    from app import db
    from app.models.domain import Domain
    from factories import make_application
    solo = make_application(db, name='solo', port=8073)
    other = make_application(db, name='other', port=8074)
    db.session.add(Domain(name='solo.example.com', is_primary=True, application_id=solo.id))
    db.session.commit()

    resp = client.post(f'/api/v1/domains/{_domain("solo.example.com").id}/move',
                       headers=auth_headers, json={'application_id': other.id})

    assert resp.status_code == 200
    assert published == ['deleted:solo', 'other']


@pytest.mark.parametrize('body', [{}, {'application_id': 'two'}, {'application_id': True}])
def test_move_needs_a_service_id(app, client, auth_headers, published, body):
    from app import db
    _apps(db)
    resp = client.post(f'/api/v1/domains/{_domain("old.example.com").id}/move', headers=auth_headers, json=body)
    assert resp.status_code == 400
    assert published == []


def test_move_to_the_same_service_is_refused(app, client, auth_headers, published):
    from app import db
    shop, _ = _apps(db)
    resp = client.post(f'/api/v1/domains/{_domain("old.example.com").id}/move',
                       headers=auth_headers, json={'application_id': shop.id})
    assert resp.status_code == 400


def test_move_to_an_unknown_service_is_404(app, client, auth_headers, published):
    from app import db
    _apps(db)
    resp = client.post(f'/api/v1/domains/{_domain("old.example.com").id}/move',
                       headers=auth_headers, json={'application_id': 999999})
    assert resp.status_code == 404
    assert _domain('old.example.com').application_id != 999999


def test_viewers_cannot_move(app, client, viewer_headers, published):
    from app import db
    shop, blog = _apps(db)
    resp = client.post(f'/api/v1/domains/{_domain("old.example.com").id}/move',
                       headers=viewer_headers, json={'application_id': blog.id})
    assert resp.status_code == 403
    assert _domain('old.example.com').application_id == shop.id


def test_deleting_the_primary_promotes_another(app, client, auth_headers, published):
    from app import db
    _apps(db)
    resp = client.delete(f'/api/v1/domains/{_domain("shop.example.com").id}', headers=auth_headers)
    assert resp.status_code == 200
    assert _domain('old.example.com').is_primary is True
