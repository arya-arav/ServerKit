"""GET /api/v1/domains/<id>/verify checks access to the domain's service like
every other per-domain route. Fork: arya-arav/ServerKit."""
import pytest


@pytest.fixture
def domain(app, monkeypatch):
    """A domain on a service owned by a separate admin, DNS stubbed out."""
    from app import db
    from app.models.domain import Domain
    from app.services import doctor_service
    from factories import make_application
    monkeypatch.setattr(doctor_service, '_resolve_host_ips', lambda name: [])
    svc = make_application(db, name='shop', port=8071)
    row = Domain(name='shop.example.com', is_primary=True, application_id=svc.id)
    db.session.add(row)
    db.session.commit()
    return row


def test_an_unrelated_user_cannot_verify_someone_elses_domain(client, developer_headers, domain):
    resp = client.get(f'/api/v1/domains/{domain.id}/verify', headers=developer_headers)
    assert resp.status_code == 403


def test_an_admin_still_can(client, auth_headers, domain):
    resp = client.get(f'/api/v1/domains/{domain.id}/verify', headers=auth_headers)
    assert resp.status_code == 200, resp.get_json()


def test_a_deleted_services_domain_is_not_found(app, client, auth_headers, domain):
    from app import db
    from app.models import Application
    svc = Application.query.get(domain.application_id)
    svc.soft_delete()
    db.session.commit()
    resp = client.get(f'/api/v1/domains/{domain.id}/verify', headers=auth_headers)
    assert resp.status_code == 404
