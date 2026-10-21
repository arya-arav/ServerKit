"""Fork: edit-in-place and test-before-save for core email providers.

The Connections SMTP card falls back to the core provider API when the Email
extension isn't installed, so it needs to test unsaved settings and edit an
existing SMTP connection without re-entering (or exposing) its password.
"""
from unittest.mock import patch

from app.notifications.providers import EmailProviderService
from app.utils.crypto import decrypt_secret_safe


def _smtp(**extra):
    data = {'provider': 'smtp', 'host': 'smtp.example.com', 'port': 587,
            'username': 'u', 'password': 'stored-pw', 'use_tls': True}
    data.update(extra)
    return EmailProviderService.add_provider(data)


def test_to_dict_exposes_settings_but_never_secrets(app):
    with app.app_context():
        d = _smtp().to_dict()
        assert d['settings']['host'] == 'smtp.example.com'
        assert d['settings']['username'] == 'u'
        assert 'password' not in d['settings']
        assert 'password' in d['configured_fields']


def test_update_settings_keeps_blank_or_masked_password(app):
    with app.app_context():
        row = _smtp()
        EmailProviderService.update_settings(row.id, {
            'host': 'smtp.other.com', 'port': 2525, 'password': '', 'is_active': False,
        })
        EmailProviderService.update_settings(row.id, {'password': '••••••••'})
        creds = row.credentials()
        assert creds['host'] == 'smtp.other.com'
        assert creds['port'] == 2525
        assert creds['password'] == 'stored-pw'
        assert row.is_active is False


def test_update_settings_encrypts_a_new_password(app):
    with app.app_context():
        row = _smtp()
        EmailProviderService.update_settings(row.id, {'password': 'new-pw'})
        raw = row.raw_credentials()['password']
        assert raw != 'new-pw' and decrypt_secret_safe(raw) == 'new-pw'


def test_test_config_uses_form_values_and_stored_password(app):
    with app.app_context():
        row = _smtp()
        with patch.object(EmailProviderService, '_test',
                          return_value={'success': True}) as fake:
            result = EmailProviderService.test_config({
                'provider_id': row.id, 'host': 'smtp.new.com', 'password': '',
            })
        assert result['success'] is True
        provider, creds = fake.call_args.args
        assert provider == 'smtp'
        assert creds['host'] == 'smtp.new.com'
        assert creds['password'] == 'stored-pw'
        # Testing never writes.
        assert row.credentials()['host'] == 'smtp.example.com'


def test_test_config_requires_host_and_known_provider(app):
    with app.app_context():
        assert EmailProviderService.test_config({'provider': 'smtp'})['success'] is False
        assert EmailProviderService.test_config({'provider': 'nope'})['success'] is False


def test_test_config_reports_connection_errors(app):
    with app.app_context():
        with patch.object(EmailProviderService, '_test', side_effect=OSError('refused')):
            result = EmailProviderService.test_config({'provider': 'smtp', 'host': 'x'})
        assert result == {'success': False, 'error': 'refused'}


def test_test_route_is_admin_only(client, auth_headers):
    resp = client.post('/api/v1/notifications/admin/email-providers/test',
                       json={'provider': 'smtp'}, headers=auth_headers)
    # auth_headers is an admin: a missing host is a 400, not a 404/405.
    assert resp.status_code == 400
    assert client.post('/api/v1/notifications/admin/email-providers/test',
                       json={'provider': 'smtp'}).status_code == 401
