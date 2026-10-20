"""Tests for the Notification Bus (queue-backed, multi-channel).

Proves the end-to-end spine: notify.send() records the event, plans per-recipient
deliveries honoring preferences, enqueues them, and the consumer renders + marks
them — plus the preference/quiet-hours/severity gating.
"""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

from app import db
from app.models import User
from app.models.notification_preferences import NotificationPreferences
from app.notifications.channels import get_adapter, register_adapter
from app.notifications.channels.base import ChannelAdapter, DeliveryResult
from app.notifications.consumer import process_message
from app.notifications.models import Notification, NotificationDelivery
from app.notifications.service import GROUP_SLUG, QUEUE_SLUG, NotificationBusService
from app.queue_bus.service import QueueBusService


@pytest.fixture(autouse=True)
def reset_broker(app):
    QueueBusService.reset_broker()


class _CaptureEmail(ChannelAdapter):
    """Stand-in email adapter: renders the real template (proving rendering
    integrates) and records the result instead of opening SMTP."""

    key = 'email'

    def __init__(self):
        self.sent = []

    def deliver(self, delivery, notification):
        from app.notifications import catalog, rendering
        meta = catalog.resolve(
            notification.event_key, notification.get_data(),
            severity=notification.severity, title=notification.title,
        )
        rendered = rendering.render_email(
            meta['template'], notification.title,
            severity=notification.severity, data=notification.get_data(),
        )
        self.sent.append({
            'to': delivery.target,
            'subject': rendered['subject'],
            'html': rendered['html'],
            'text': rendered['text'],
        })
        return DeliveryResult.sent('captured-msg-id')


@pytest.fixture
def captured_email():
    original = get_adapter('email')
    cap = _CaptureEmail()
    register_adapter(cap)
    yield cap
    register_adapter(original)  # restore real adapter for other tests


def _make_user(username='alice', email='alice@example.com', role='developer'):
    user = User(
        email=email, username=username,
        password_hash=generate_password_hash('x'),
        role=role, is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _prefs(user, **fields):
    prefs = NotificationPreferences.get_or_create(user.id)
    for key, value in fields.items():
        if key == 'channels':
            prefs.set_channels(value)
        elif key == 'severities':
            prefs.set_severities(value)
        elif key == 'categories':
            prefs.set_categories(value)
        else:
            setattr(prefs, key, value)
    db.session.commit()
    return prefs


def _drain():
    msgs = QueueBusService.receive(GROUP_SLUG, QUEUE_SLUG, visibility_timeout_ms=60000, max_messages=100)
    for message in msgs:
        process_message(message)
    return len(msgs)


class TestSendAndDeliver:
    def test_send_creates_notification_and_deliveries(self, app):
        user = _make_user()
        _prefs(user, channels=['email'], severities=['critical', 'warning', 'success'])

        result = NotificationBusService.send(
            'backup.completed', to=user,
            data={'app': 'blog', 'size': '2.3 GB', 'duration': '41s'},
        )

        notif = Notification.query.get(result['notification_id'])
        assert notif is not None
        assert notif.event_key == 'backup.completed'
        assert notif.severity == 'success'
        assert notif.category == 'backups'
        assert notif.title == 'Backup completed: blog'

        deliveries = NotificationDelivery.query.filter_by(notification_id=notif.id).all()
        channels = {d.channel for d in deliveries}
        assert channels == {'inapp', 'email'}

    def test_inapp_is_sent_immediately_email_is_queued(self, app):
        user = _make_user()
        _prefs(user, channels=['email'], severities=['success'])

        NotificationBusService.send('backup.completed', to=user, data={'app': 'blog'})

        inapp = NotificationDelivery.query.filter_by(channel='inapp').one()
        email = NotificationDelivery.query.filter_by(channel='email').one()
        assert inapp.status == 'sent' and inapp.sent_at is not None
        assert email.status == 'pending'  # waits for the consumer

    def test_consumer_delivers_and_renders_email(self, app, captured_email):
        user = _make_user(email='ops@example.com')
        _prefs(user, channels=['email'], severities=['critical'])

        NotificationBusService.send(
            'security.alert', to=user,
            data={'message': '5 failed admin logins', 'alert_type': 'auth_failure',
                  'source_ip': '203.0.113.7', 'identity': 'admin'},
        )
        drained = _drain()

        assert drained == 1  # only the email was queued (inapp is instant)
        assert len(captured_email.sent) == 1
        sent = captured_email.sent[0]
        assert sent['to'] == 'ops@example.com'
        assert sent['subject'] == 'Security alert: auth_failure'
        assert '203.0.113.7' in sent['html']      # rendered the fact
        assert '<!DOCTYPE html>' in sent['html']   # full branded layout
        assert '5 failed admin logins' in sent['text']

        email = NotificationDelivery.query.filter_by(channel='email').one()
        assert email.status == 'sent'
        assert email.provider_message_id == 'captured-msg-id'
        assert email.attempts == 1


class TestGating:
    def test_severity_below_preference_is_dropped(self, app):
        user = _make_user()
        _prefs(user, channels=['email'], severities=['critical', 'warning'])

        # 'info' is not in the user's severities -> nothing planned.
        result = NotificationBusService.send('app.deployed', to=user, severity='info',
                                             data={'app': 'blog'})
        assert result['deliveries'] == 0
        assert NotificationDelivery.query.count() == 0

    def test_category_opt_out_is_respected(self, app):
        user = _make_user()
        _prefs(user, channels=['email'], severities=['critical'],
               categories={'system': True, 'security': False, 'backups': True, 'apps': True})

        # critical bypasses severity gating, but the category opt-out still holds.
        result = NotificationBusService.send('security.alert', to=user,
                                             data={'message': 'x'})
        assert result['deliveries'] == 0

    def test_quiet_hours_suppress_non_critical_but_not_critical(self, app):
        user = _make_user()
        now = datetime.now()
        start = (now - timedelta(minutes=3)).strftime('%H:%M')
        end = (now + timedelta(minutes=3)).strftime('%H:%M')
        _prefs(user, channels=['email'], severities=['critical', 'warning'],
               quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)

        quiet = NotificationBusService.send('system.alert', to=user, severity='warning',
                                            data={'message': 'cpu high'})
        assert quiet['deliveries'] == 0  # suppressed during quiet hours

        loud = NotificationBusService.send('security.alert', to=user, severity='critical',
                                           data={'message': 'breach'})
        assert loud['deliveries'] >= 1  # critical pierces quiet hours

    def test_disabled_user_gets_nothing_unless_directed(self, app):
        user = _make_user()
        _prefs(user, enabled=False, channels=['email'])

        off = NotificationBusService.send('app.deployed', to=user, data={'app': 'blog'})
        assert off['deliveries'] == 0

        # A directed transactional send bypasses preference gating entirely.
        directed = NotificationBusService.send('user.welcome', to=user,
                                               channels=['email'], data={})
        assert directed['deliveries'] == 1


class TestRecipients:
    def test_bare_email_recipient(self, app, captured_email):
        NotificationBusService.send('user.invitation', to='invitee@example.com',
                                    channels=['email'],
                                    data={'summary': 'Join the team', 'action_url': 'https://x/y'})
        _drain()
        assert len(captured_email.sent) == 1
        assert captured_email.sent[0]['to'] == 'invitee@example.com'

        delivery = NotificationDelivery.query.filter_by(channel='email').one()
        assert delivery.recipient_user_id is None
        assert delivery.status == 'sent'

    def test_admins_audience_resolves_admin_users(self, app):
        _make_user('admin1', 'a1@example.com', role=User.ROLE_ADMIN)
        _make_user('admin2', 'a2@example.com', role=User.ROLE_ADMIN)
        _make_user('dev', 'd@example.com', role='developer')
        for u in User.query.all():
            _prefs(u, channels=['email'], severities=['critical'])

        result = NotificationBusService.send('security.alert', to='admins',
                                             data={'message': 'x'})
        # 2 admins x (inapp + email) = 4 deliveries; the developer is excluded.
        recipients = {d.recipient_user_id for d in NotificationDelivery.query.all()}
        assert len(recipients) == 2
        assert result['deliveries'] == 4

    def test_sdk_facade_sends(self, app):
        from app.plugins_sdk import notify
        user = _make_user()
        _prefs(user, channels=['email'], severities=['success'])
        result = notify.send('backup.completed', to=user, data={'app': 'blog'})
        assert result['notification_id'] is not None
        assert result['deliveries'] == 2  # inapp + email


class TestInbox:
    def test_inbox_lists_inapp_and_unread_count(self, app):
        user = _make_user()
        _prefs(user, channels=['email'], severities=['success'])
        NotificationBusService.send('backup.completed', to=user, data={'app': 'blog'})

        items = NotificationBusService.inbox(user.id)
        assert len(items) == 1
        assert items[0]['event_key'] == 'backup.completed'
        assert items[0]['read'] is False
        assert 'delivery_id' in items[0]
        assert NotificationBusService.unread_count(user.id) == 1

    def test_mark_read_and_mark_all(self, app):
        user = _make_user()
        _prefs(user, channels=[], severities=['critical'])  # inapp only
        NotificationBusService.send('security.alert', to=user, data={'message': 'x'})
        NotificationBusService.send('security.alert', to=user, data={'message': 'y'})
        assert NotificationBusService.unread_count(user.id) == 2

        items = NotificationBusService.inbox(user.id)
        assert NotificationBusService.mark_read(user.id, items[0]['delivery_id']) is True
        assert NotificationBusService.unread_count(user.id) == 1
        assert NotificationBusService.mark_all_read(user.id) == 1
        assert NotificationBusService.unread_count(user.id) == 0

    def test_inbox_api_roundtrip(self, app, client, auth_headers):
        admin = User.query.filter_by(username='testadmin').first()
        _prefs(admin, channels=[], severities=['critical'])
        NotificationBusService.send('security.alert', to=admin, data={'message': 'x'})

        resp = client.get('/api/v1/notifications/inbox', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['unread_count'] == 1
        assert len(body['items']) == 1

        delivery_id = body['items'][0]['delivery_id']
        resp2 = client.post(f'/api/v1/notifications/inbox/{delivery_id}/read', headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.get_json()['unread_count'] == 0

        resp3 = client.get('/api/v1/notifications/inbox/unread-count', headers=auth_headers)
        assert resp3.get_json()['count'] == 0

    # Fork: arya-arav/ServerKit — removing notifications from the bell.
    def test_dismiss_removes_one_from_the_inbox_but_keeps_the_delivery(self, app):
        user = _make_user()
        _prefs(user, channels=[], severities=['critical'])
        NotificationBusService.send('security.alert', to=user, data={'message': 'x'})
        NotificationBusService.send('security.alert', to=user, data={'message': 'y'})
        first = NotificationBusService.inbox(user.id)[0]['delivery_id']

        assert NotificationBusService.dismiss(user.id, first) is True

        assert [i['delivery_id'] for i in NotificationBusService.inbox(user.id)] != [first]
        assert len(NotificationBusService.inbox(user.id)) == 1
        assert NotificationBusService.unread_count(user.id) == 1
        assert NotificationDelivery.query.get(first) is not None   # admin log keeps it
        assert NotificationBusService.dismiss(user.id, first) is False

    def test_clear_removes_read_ones_and_all_with_the_flag(self, app):
        user = _make_user()
        _prefs(user, channels=[], severities=['critical'])
        for msg in ('a', 'b', 'c'):
            NotificationBusService.send('security.alert', to=user, data={'message': msg})
        NotificationBusService.mark_read(user.id, NotificationBusService.inbox(user.id)[0]['delivery_id'])

        assert NotificationBusService.clear(user.id) == 1
        assert len(NotificationBusService.inbox(user.id)) == 2
        assert NotificationBusService.clear(user.id, include_unread=True) == 2
        assert NotificationBusService.inbox(user.id) == []
        assert NotificationBusService.unread_count(user.id) == 0

    def test_a_user_cannot_dismiss_someone_elses_notification(self, app, client, auth_headers):
        other = _make_user()
        _prefs(other, channels=[], severities=['critical'])
        NotificationBusService.send('security.alert', to=other, data={'message': 'x'})
        delivery_id = NotificationBusService.inbox(other.id)[0]['delivery_id']

        resp = client.post(f'/api/v1/notifications/inbox/{delivery_id}/dismiss', headers=auth_headers)

        assert resp.status_code == 404
        assert len(NotificationBusService.inbox(other.id)) == 1

    def test_clear_api(self, app, client, auth_headers):
        admin = User.query.filter_by(username='testadmin').first()
        _prefs(admin, channels=[], severities=['critical'])
        NotificationBusService.send('security.alert', to=admin, data={'message': 'x'})

        resp = client.post('/api/v1/notifications/inbox/clear?all=1', headers=auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()['cleared'] == 1
        assert client.get('/api/v1/notifications/inbox', headers=auth_headers).get_json()['items'] == []


class TestDeliveryLog:
    def test_delivery_log_and_stats(self, app, captured_email):
        user = _make_user(email='ops@example.com')
        _prefs(user, channels=['email'], severities=['critical'])
        NotificationBusService.send('security.alert', to=user, data={'message': 'x'})
        _drain()

        log = NotificationBusService.delivery_log()
        assert len(log) == 2  # inapp + email
        assert all('event_key' in d for d in log)

        stats = NotificationBusService.delivery_stats()
        assert stats['total'] == 2
        assert stats['by_status'].get('sent') == 2
        assert set(stats['by_channel'].keys()) == {'inapp', 'email'}

    def test_retry_requeues_failed(self, app):
        user = _make_user()
        _prefs(user, channels=['email'], severities=['critical'])
        NotificationBusService.send('security.alert', to=user, data={'message': 'x'})

        email = NotificationDelivery.query.filter_by(channel='email').one()
        email.status = 'failed'
        email.error = 'boom'
        db.session.commit()

        result = NotificationBusService.retry_delivery(email.id)
        assert result['status'] == 'pending'
        db.session.refresh(email)
        assert email.status == 'pending'
        assert email.error is None

    def test_admin_delivery_log_api(self, app, client, auth_headers):
        admin = User.query.filter_by(username='testadmin').first()
        _prefs(admin, channels=[], severities=['critical'])
        NotificationBusService.send('security.alert', to=admin, data={'message': 'x'})

        resp = client.get('/api/v1/notifications/admin/deliveries', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['stats']['total'] >= 1
        assert any(d['channel'] == 'inapp' for d in body['deliveries'])


class TestEmailProviders:
    def test_add_encrypts_and_sets_default(self, app):
        import json as _json
        from app.notifications.providers import EmailProviderService

        provider = EmailProviderService.add_provider({
            'provider': 'sendgrid', 'name': 'SG',
            'api_key': 'SG.supersecret', 'from_address': 'no-reply@x.com',
        })
        assert provider.is_default is True
        # Encrypted at rest, but decrypts back, and never serialized.
        assert provider.raw_credentials()['api_key'] != 'SG.supersecret'
        assert provider.credentials()['api_key'] == 'SG.supersecret'
        assert 'SG.supersecret' not in _json.dumps(provider.to_dict())
        assert EmailProviderService.default_provider().id == provider.id

    def test_adapter_prefers_provider(self, app, monkeypatch):
        from app.notifications.providers import EmailProviderService

        EmailProviderService.add_provider({
            'provider': 'smtp', 'name': 'S', 'host': 'localhost', 'port': '25',
            'username': 'u', 'password': 'p', 'from_address': 'a@b.com',
        })
        captured = {}

        def fake_send(provider, to_addr, subject, html, text):
            captured.update({'to': to_addr, 'subject': subject})
            return {'success': True, 'message_id': 'mid-123', 'error': None}
        monkeypatch.setattr(EmailProviderService, 'send', staticmethod(fake_send))

        user = _make_user(email='ops@example.com')
        _prefs(user, channels=['email'], severities=['critical'])
        NotificationBusService.send('security.alert', to=user, data={'message': 'x'})
        _drain()

        email = NotificationDelivery.query.filter_by(channel='email').one()
        assert email.status == 'sent'
        assert email.provider_message_id == 'mid-123'
        assert captured['to'] == 'ops@example.com'

    def test_set_default_and_delete_promotes(self, app):
        from app.notifications.providers import EmailProviderService

        a = EmailProviderService.add_provider({'provider': 'sendgrid', 'name': 'A', 'api_key': 'k1'})
        b = EmailProviderService.add_provider({'provider': 'postmark', 'name': 'B', 'server_token': 't1'})
        assert a.is_default is True and b.is_default is False

        EmailProviderService.set_default(b.id)
        assert EmailProviderService.default_provider().id == b.id

        EmailProviderService.delete_provider(b.id)
        assert EmailProviderService.default_provider().id == a.id


class TestSystemBroadcast:
    def test_send_all_routes_through_bus(self, app, monkeypatch, captured_email):
        from app.services.notification_service import NotificationService

        cfg = {
            'email': {'enabled': True, 'smtp_host': 'smtp.x', 'from_email': 'a@x',
                      'to_emails': ['ops@x.com'], 'notify_on': ['critical', 'warning']},
            'discord': {'enabled': True, 'webhook_url': 'https://discord/hook',
                        'notify_on': ['critical', 'warning']},
            'slack': {'enabled': False}, 'telegram': {'enabled': False},
            'generic_webhook': {'enabled': False},
        }
        monkeypatch.setattr(NotificationService, 'get_config', classmethod(lambda cls: cfg))
        discord_seen = {}
        monkeypatch.setattr(NotificationService, 'send_discord',
                            classmethod(lambda cls, alerts, c: discord_seen.update({'alerts': alerts}) or {'success': True}))

        result = NotificationService.send_all([
            {'severity': 'critical', 'type': 'disk', 'message': 'Disk full', 'value': '95%', 'threshold': '90%'},
        ])
        assert result['success'] is True

        notif = Notification.query.filter_by(event_key='monitoring.alert').one()
        deliveries = NotificationDelivery.query.filter_by(notification_id=notif.id).all()
        assert {d.channel for d in deliveries} == {'email', 'discord'}
        assert all(d.recipient_user_id is None for d in deliveries)  # system, not per-user

        _drain()
        # Email rendered through the new monitoring template.
        assert len(captured_email.sent) == 1
        assert captured_email.sent[0]['to'] == 'ops@x.com'
        assert 'Disk full' in captured_email.sent[0]['html']
        # Chat kept the original per-alert list.
        assert discord_seen['alerts'][0]['type'] == 'disk'
        for d in deliveries:
            db.session.refresh(d)
            assert d.status == 'sent'
