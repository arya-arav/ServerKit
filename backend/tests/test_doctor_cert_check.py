"""Tests for the doctor's certificate-expiry check: certificates read from disk
(not only Domain.ssl_expires_at) and the certbot auto-renew status
(doctor_service._cert_check / _certs_on_disk / _renewal_status)."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.services import doctor_service
from app.services.doctor_service import CERT_WARN_DAYS, DoctorService


def write_cert(path, days_left):
    """Self-signed PEM at path that expires days_left days from now."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test.local')])
    now = datetime.now(timezone.utc)
    not_after = now + timedelta(days=days_left)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(min(now, not_after) - timedelta(days=1))
            .not_valid_after(not_after)
            .sign(key, hashes.SHA256()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def live_dir(tmp_path, monkeypatch):
    """Point the disk scan at a temp Let's Encrypt-style live/ directory."""
    monkeypatch.setattr(doctor_service, 'CERT_DIR_GLOBS', (str(tmp_path / '*' / 'cert.pem'),))
    return tmp_path


def run_check(app, monkeypatch, renewal):
    monkeypatch.setattr(DoctorService, '_renewal_status', staticmethod(lambda: renewal))
    with app.app_context():
        return DoctorService._cert_check()


def test_no_certificates_still_warns_not_monitored(app, live_dir, monkeypatch):
    result = run_check(app, monkeypatch, renewal=True)
    assert result['status'] == 'warn'
    assert 'not being monitored' in result['detail']


def test_certificate_on_disk_with_auto_renew_is_ok(app, live_dir, monkeypatch):
    write_cert(live_dir / 'cp.example.com' / 'cert.pem', days_left=80)
    result = run_check(app, monkeypatch, renewal=True)
    assert result['status'] == 'ok'
    assert 'cp.example.com' in result['detail']
    assert 'Automatic renewal is on' in result['detail']


def test_auto_renew_off_warns(app, live_dir, monkeypatch):
    write_cert(live_dir / 'cp.example.com' / 'cert.pem', days_left=80)
    result = run_check(app, monkeypatch, renewal=False)
    assert result['status'] == 'warn'
    assert 'renewal is off' in result['detail']


def test_unknown_renewal_state_stays_ok_without_claim(app, live_dir, monkeypatch):
    write_cert(live_dir / 'cp.example.com' / 'cert.pem', days_left=80)
    result = run_check(app, monkeypatch, renewal=None)
    assert result['status'] == 'ok'
    assert 'renewal' not in result['detail']


def test_expiring_soon_warns_with_nearest(app, live_dir, monkeypatch):
    write_cert(live_dir / 'far.example.com' / 'cert.pem', days_left=80)
    write_cert(live_dir / 'near.example.com' / 'cert.pem', days_left=CERT_WARN_DAYS - 4)
    result = run_check(app, monkeypatch, renewal=True)
    assert result['status'] == 'warn'
    assert 'near.example.com' in result['detail']


def test_expired_fails(app, live_dir, monkeypatch):
    write_cert(live_dir / 'old.example.com' / 'cert.pem', days_left=-2)
    result = run_check(app, monkeypatch, renewal=True)
    assert result['status'] == 'fail'
    assert 'old.example.com' in result['detail']


def test_unreadable_file_is_skipped(app, live_dir, monkeypatch):
    bad = live_dir / 'broken.example.com' / 'cert.pem'
    bad.parent.mkdir(parents=True)
    bad.write_text('not a certificate')
    write_cert(live_dir / 'good.example.com' / 'cert.pem', days_left=60)
    result = run_check(app, monkeypatch, renewal=True)
    assert result['status'] == 'ok'
    assert 'good.example.com' in result['detail']
