"""Email provider service + transports for the Notification Bus.

Manages ``EmailProviderConnection`` rows (add / list / test / set-default /
delete) and sends a rendered email through the configured transport:

    SMTP (any) · SendGrid · Postmark · Amazon SES · Mailgun

Secrets are encrypted at rest (see the model). The email channel adapter calls
``EmailProviderService.default_provider()`` then ``.send(...)``; if no provider
is configured it falls back to the legacy notifications.json SMTP config.
"""
import json
import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from app import db
from app.models.email_provider import EmailProviderConnection
from app.utils.crypto import encrypt_secret, is_encrypted

logger = logging.getLogger(__name__)

# Per-provider field set + which fields are secret (encrypted at rest).
SUPPORTED = {
    'smtp': {
        'name': 'Custom SMTP',
        'fields': ['host', 'port', 'username', 'password', 'use_tls'],
        'secrets': ['password'],
    },
    'sendgrid': {
        'name': 'SendGrid',
        'fields': ['api_key'],
        'secrets': ['api_key'],
    },
    'postmark': {
        'name': 'Postmark',
        'fields': ['server_token'],
        'secrets': ['server_token'],
    },
    'ses': {
        'name': 'Amazon SES',
        'fields': ['access_key', 'secret_key', 'region'],
        'secrets': ['secret_key'],
    },
    'mailgun': {
        'name': 'Mailgun',
        'fields': ['api_key', 'domain'],
        'secrets': ['api_key'],
    },
}

_TIMEOUT = 20


def _is_masked(value):
    """A form echoing back the stored-secret placeholder, not a new value."""
    return isinstance(value, str) and bool(value) and set(value) <= {'*', '•'}


class EmailProviderService:

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @classmethod
    def list_providers(cls):
        return EmailProviderConnection.query.order_by(
            EmailProviderConnection.is_default.desc(),
            EmailProviderConnection.created_at.asc(),
        ).all()

    @staticmethod
    def get_provider(provider_id):
        return EmailProviderConnection.query.get(provider_id)

    @classmethod
    def default_provider(cls):
        """The provider the Notification Bus should send through: the active
        default, else the first active one — both restricted to providers
        flagged ``uses_notifications`` so relay-only rows are never picked."""
        row = EmailProviderConnection.query.filter_by(
            is_default=True, is_active=True, uses_notifications=True).first()
        if row:
            return row
        return EmailProviderConnection.query.filter_by(
            is_active=True, uses_notifications=True).order_by(
            EmailProviderConnection.created_at.asc()
        ).first()

    @classmethod
    def relay_provider(cls):
        """The active SMTP connection that should drive the Postfix relay:
        highest relay_priority first, then oldest. None if no relay is flagged."""
        return EmailProviderConnection.query.filter_by(
            uses_relay=True, is_active=True).order_by(
            EmailProviderConnection.relay_priority.desc(),
            EmailProviderConnection.created_at.asc(),
        ).first()

    @classmethod
    def add_provider(cls, data, user_id=None):
        provider = (data.get('provider') or '').lower().strip()
        if provider not in SUPPORTED:
            raise ValueError(f'Unsupported email provider: {provider}')

        spec = SUPPORTED[provider]
        creds = {}
        for field in spec['fields']:
            value = data.get(field)
            if value in (None, ''):
                continue
            if field in spec['secrets'] and isinstance(value, str) and not is_encrypted(value):
                value = encrypt_secret(value)
            creds[field] = value

        make_default = bool(data.get('is_default'))
        # First provider is implicitly the default.
        if EmailProviderConnection.query.count() == 0:
            make_default = True
        if make_default:
            EmailProviderConnection.query.update({EmailProviderConnection.is_default: False})

        # Usage flags. API providers cannot be a Postfix smarthost, so
        # uses_relay is only honored for SMTP.
        uses_relay = bool(data.get('uses_relay')) and provider == 'smtp'
        row = EmailProviderConnection(
            provider=provider,
            name=data.get('name') or spec['name'],
            credentials_json=json.dumps(creds),
            from_address=(data.get('from_address') or '').strip() or None,
            from_name=(data.get('from_name') or 'ServerKit').strip(),
            is_default=make_default,
            is_active=data.get('is_active', True),
            uses_notifications=bool(data.get('uses_notifications', True)),
            uses_relay=uses_relay,
            relay_priority=int(data.get('relay_priority') or 0),
            created_by=user_id,
        )
        db.session.add(row)
        db.session.commit()
        return row

    @classmethod
    def update_usage(cls, provider_id, fields):
        """Toggle a provider's usage flags: uses_notifications, uses_relay,
        relay_priority. uses_relay is only honored for SMTP connections."""
        row = cls.get_provider(provider_id)
        if not row:
            return None
        if 'uses_notifications' in fields:
            row.uses_notifications = bool(fields['uses_notifications'])
        if 'uses_relay' in fields:
            row.uses_relay = bool(fields['uses_relay']) and row.provider == 'smtp'
        if 'relay_priority' in fields and fields['relay_priority'] is not None:
            try:
                row.relay_priority = int(fields['relay_priority'])
            except (TypeError, ValueError):
                pass
        db.session.commit()
        return row

    @classmethod
    def update_settings(cls, provider_id, data):
        """Edit a provider's credentials/sender in place. A blank or masked
        secret keeps the stored one; ``is_active`` toggles the connection."""
        row = cls.get_provider(provider_id)
        if not row:
            return None
        spec = SUPPORTED.get(row.provider, {'fields': [], 'secrets': []})
        creds = row.raw_credentials()
        for field in spec['fields']:
            if field not in data:
                continue
            value = data.get(field)
            if field in spec['secrets']:
                if not value or _is_masked(value):
                    continue
                if isinstance(value, str) and not is_encrypted(value):
                    value = encrypt_secret(value)
            if value in (None, ''):
                creds.pop(field, None)
            else:
                creds[field] = value
        row.credentials_json = json.dumps(creds)
        if 'from_address' in data:
            row.from_address = (data.get('from_address') or '').strip() or None
        if data.get('from_name'):
            row.from_name = data['from_name'].strip()
        if 'is_active' in data:
            row.is_active = bool(data['is_active'])
        db.session.commit()
        return row

    @classmethod
    def set_default(cls, provider_id):
        row = cls.get_provider(provider_id)
        if not row:
            return None
        EmailProviderConnection.query.update({EmailProviderConnection.is_default: False})
        row.is_default = True
        row.is_active = True
        db.session.commit()
        return row

    @classmethod
    def delete_provider(cls, provider_id):
        row = cls.get_provider(provider_id)
        if not row:
            return False
        was_default = row.is_default
        db.session.delete(row)
        db.session.commit()
        # Promote another provider to default if we removed the default one.
        if was_default:
            nxt = EmailProviderConnection.query.order_by(
                EmailProviderConnection.created_at.asc()).first()
            if nxt:
                nxt.is_default = True
                db.session.commit()
        return True

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    @classmethod
    def send(cls, provider, to_addr, subject, html, text):
        """Send one email via ``provider``. Returns
        {'success', 'message_id', 'error'}."""
        creds = provider.credentials()
        sender = provider.from_address or creds.get('username') or 'serverkit@localhost'
        from_name = provider.from_name or 'ServerKit'
        try:
            if provider.provider == 'smtp':
                return cls._send_smtp(creds, sender, from_name, to_addr, subject, html, text)
            if provider.provider == 'sendgrid':
                return cls._send_sendgrid(creds, sender, from_name, to_addr, subject, html, text)
            if provider.provider == 'postmark':
                return cls._send_postmark(creds, sender, from_name, to_addr, subject, html, text)
            if provider.provider == 'ses':
                return cls._send_ses(creds, sender, to_addr, subject, html, text)
            if provider.provider == 'mailgun':
                return cls._send_mailgun(creds, sender, from_name, to_addr, subject, html, text)
        except Exception as exc:  # transport-level failure
            return {'success': False, 'message_id': None, 'error': str(exc)[:500]}
        return {'success': False, 'message_id': None, 'error': 'unknown provider'}

    @staticmethod
    def _mime(sender, from_name, to_addr, subject, html, text):
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'{from_name} <{sender}>'
        msg['To'] = to_addr
        msg.attach(MIMEText(text, 'plain', 'utf-8'))
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        return msg

    @classmethod
    def _send_smtp(cls, creds, sender, from_name, to_addr, subject, html, text):
        host = creds.get('host')
        port = int(creds.get('port') or 587)
        use_tls = creds.get('use_tls', True)
        if isinstance(use_tls, str):
            use_tls = use_tls.lower() not in ('0', 'false', 'no')
        msg = cls._mime(sender, from_name, to_addr, subject, html, text)
        if use_tls:
            server = smtplib.SMTP(host, port, timeout=_TIMEOUT)
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT, context=ssl.create_default_context())
        with server:
            if creds.get('username') and creds.get('password'):
                server.login(creds['username'], creds['password'])
            server.send_message(msg)
        return {'success': True, 'message_id': None, 'error': None}

    @classmethod
    def _send_sendgrid(cls, creds, sender, from_name, to_addr, subject, html, text):
        resp = requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={'Authorization': f"Bearer {creds.get('api_key')}",
                     'Content-Type': 'application/json'},
            json={
                'personalizations': [{'to': [{'email': to_addr}]}],
                'from': {'email': sender, 'name': from_name},
                'subject': subject,
                'content': [
                    {'type': 'text/plain', 'value': text},
                    {'type': 'text/html', 'value': html},
                ],
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code in (200, 201, 202):
            return {'success': True, 'message_id': resp.headers.get('X-Message-Id'), 'error': None}
        return {'success': False, 'message_id': None, 'error': f'SendGrid HTTP {resp.status_code}: {resp.text[:200]}'}

    @classmethod
    def _send_postmark(cls, creds, sender, from_name, to_addr, subject, html, text):
        resp = requests.post(
            'https://api.postmarkapp.com/email',
            headers={'X-Postmark-Server-Token': creds.get('server_token') or '',
                     'Accept': 'application/json', 'Content-Type': 'application/json'},
            json={
                'From': f'{from_name} <{sender}>',
                'To': to_addr,
                'Subject': subject,
                'HtmlBody': html,
                'TextBody': text,
                'MessageStream': 'outbound',
            },
            timeout=_TIMEOUT,
        )
        body = resp.json() if resp.content else {}
        if resp.status_code == 200 and body.get('ErrorCode', 0) == 0:
            return {'success': True, 'message_id': body.get('MessageID'), 'error': None}
        return {'success': False, 'message_id': None,
                'error': f"Postmark: {body.get('Message') or resp.status_code}"}

    @classmethod
    def _send_mailgun(cls, creds, sender, from_name, to_addr, subject, html, text):
        domain = creds.get('domain')
        resp = requests.post(
            f'https://api.mailgun.net/v3/{domain}/messages',
            auth=('api', creds.get('api_key') or ''),
            data={
                'from': f'{from_name} <{sender}>',
                'to': to_addr,
                'subject': subject,
                'text': text,
                'html': html,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            body = resp.json() if resp.content else {}
            return {'success': True, 'message_id': body.get('id'), 'error': None}
        return {'success': False, 'message_id': None, 'error': f'Mailgun HTTP {resp.status_code}: {resp.text[:200]}'}

    @classmethod
    def _send_ses(cls, creds, sender, to_addr, subject, html, text):
        client = cls._ses_client(creds)
        resp = client.send_email(
            Source=sender,
            Destination={'ToAddresses': [to_addr]},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {
                    'Text': {'Data': text, 'Charset': 'UTF-8'},
                    'Html': {'Data': html, 'Charset': 'UTF-8'},
                },
            },
        )
        return {'success': True, 'message_id': resp.get('MessageId'), 'error': None}

    @staticmethod
    def _ses_client(creds):
        import boto3
        return boto3.client(
            'ses',
            aws_access_key_id=creds.get('access_key'),
            aws_secret_access_key=creds.get('secret_key'),
            region_name=creds.get('region') or 'us-east-1',
        )

    # ------------------------------------------------------------------
    # Testing (validate credentials without sending)
    # ------------------------------------------------------------------
    @classmethod
    def test_provider(cls, provider_id):
        provider = cls.get_provider(provider_id)
        if not provider:
            return {'success': False, 'error': 'Provider not found'}
        creds = provider.credentials()
        try:
            result = cls._test(provider.provider, creds)
        except Exception as exc:
            result = {'success': False, 'error': str(exc)[:300]}
        provider.last_tested_at = datetime.utcnow()
        provider.last_test_ok = bool(result.get('success'))
        db.session.commit()
        return result

    @classmethod
    def test_config(cls, data):
        """Validate unsaved settings without storing them. With ``provider_id``,
        blank or masked secrets fall back to that provider's stored ones, so
        an edit form can test without re-entering the password."""
        provider = (data.get('provider') or '').lower().strip()
        stored = cls.get_provider(data['provider_id']) if data.get('provider_id') else None
        if stored:
            provider = stored.provider
        if provider not in SUPPORTED:
            return {'success': False, 'error': f'Unsupported email provider: {provider}'}
        spec = SUPPORTED[provider]
        creds = stored.credentials() if stored else {}
        for field in spec['fields']:
            value = data.get(field)
            if field in spec['secrets'] and (not value or _is_masked(value)):
                continue
            if value not in (None, ''):
                creds[field] = value
        if provider == 'smtp' and not str(creds.get('host') or '').strip():
            return {'success': False, 'error': 'An SMTP host is required'}
        try:
            return cls._test(provider, creds)
        except Exception as exc:
            return {'success': False, 'error': str(exc)[:300]}

    @classmethod
    def _test(cls, provider, creds):
        if provider == 'smtp':
            host = creds.get('host')
            port = int(creds.get('port') or 587)
            use_tls = creds.get('use_tls', True)
            if isinstance(use_tls, str):
                use_tls = use_tls.lower() not in ('0', 'false', 'no')
            if use_tls:
                server = smtplib.SMTP(host, port, timeout=_TIMEOUT)
                server.starttls(context=ssl.create_default_context())
            else:
                server = smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT, context=ssl.create_default_context())
            with server:
                if creds.get('username') and creds.get('password'):
                    server.login(creds['username'], creds['password'])
            return {'success': True, 'message': 'SMTP connection OK'}

        if provider == 'sendgrid':
            resp = requests.get('https://api.sendgrid.com/v3/scopes',
                                headers={'Authorization': f"Bearer {creds.get('api_key')}"},
                                timeout=_TIMEOUT)
            ok = resp.status_code == 200
            return {'success': ok, 'message': 'SendGrid key OK' if ok else None,
                    'error': None if ok else f'HTTP {resp.status_code}'}

        if provider == 'postmark':
            resp = requests.get('https://api.postmarkapp.com/server',
                                headers={'X-Postmark-Server-Token': creds.get('server_token') or '',
                                         'Accept': 'application/json'},
                                timeout=_TIMEOUT)
            ok = resp.status_code == 200
            return {'success': ok, 'message': 'Postmark token OK' if ok else None,
                    'error': None if ok else f'HTTP {resp.status_code}'}

        if provider == 'mailgun':
            domain = creds.get('domain')
            resp = requests.get(f'https://api.mailgun.net/v3/{domain}',
                                auth=('api', creds.get('api_key') or ''),
                                timeout=_TIMEOUT)
            ok = resp.status_code == 200
            return {'success': ok, 'message': 'Mailgun key OK' if ok else None,
                    'error': None if ok else f'HTTP {resp.status_code}'}

        if provider == 'ses':
            client = cls._ses_client(creds)
            client.get_send_quota()
            return {'success': True, 'message': 'SES credentials OK'}

        return {'success': False, 'error': 'unknown provider'}
