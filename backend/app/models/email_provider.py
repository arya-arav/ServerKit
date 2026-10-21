"""Email provider connection for the Notification Bus.

A configured outbound transport (SMTP or a native API: SendGrid / Postmark /
Amazon SES / Mailgun) the email channel uses to actually send. Credentials are
stored Fernet-encrypted (per-field) in ``credentials_json``; the public
``to_dict`` never exposes them. Follows the Connections pattern used by DNS /
cloud / registrar providers.
"""
from datetime import datetime

from app import db
from app.models.mixins import TimestampMixin
from app.models.json_column_mixin import JsonColumnMixin
from app.utils.crypto import decrypt_secret_safe


class EmailProviderConnection(TimestampMixin, JsonColumnMixin, db.Model):
    __tablename__ = 'email_provider_connections'

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), nullable=False)   # smtp|sendgrid|postmark|ses|mailgun
    name = db.Column(db.String(120), nullable=False)

    # JSON map of credential fields; secret fields are Fernet-encrypted at rest.
    credentials_json = db.Column(db.Text)

    from_address = db.Column(db.String(255))
    from_name = db.Column(db.String(120), default='ServerKit')

    is_default = db.Column(db.Boolean, default=False, index=True)
    is_active = db.Column(db.Boolean, default=True)

    # Usage flags: one connection can power the Notification Bus
    # and/or the Postfix outbound relay. API providers (SendGrid/Postmark/SES/
    # Mailgun) can't be a Postfix smarthost, so only 'smtp' rows set uses_relay.
    uses_notifications = db.Column(db.Boolean, default=True, nullable=False)
    uses_relay = db.Column(db.Boolean, default=False, nullable=False)
    relay_priority = db.Column(db.Integer, default=0, nullable=False)

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    last_tested_at = db.Column(db.DateTime)
    last_test_ok = db.Column(db.Boolean)

    def raw_credentials(self):
        return self._json_read('credentials_json')

    def credentials(self):
        """Decrypted credentials for use at send/test time (never serialized)."""
        out = {}
        for key, value in self.raw_credentials().items():
            out[key] = decrypt_secret_safe(value) if isinstance(value, str) else value
        return out

    def public_settings(self):
        """Non-secret credential fields (host, port, username...) for edit
        forms. Secret fields are left out whatever their storage state."""
        from app.notifications.providers import SUPPORTED
        secrets = set(SUPPORTED.get(self.provider, {}).get('secrets', []))
        return {k: v for k, v in self.raw_credentials().items() if k not in secrets}

    def to_dict(self):
        creds = self.raw_credentials()
        # Surface which non-secret fields are set / the masked tail — never secrets.
        return {
            'id': self.id,
            'provider': self.provider,
            'name': self.name,
            'from_address': self.from_address,
            'from_name': self.from_name,
            'is_default': self.is_default,
            'is_active': self.is_active,
            'uses_notifications': self.uses_notifications,
            'uses_relay': self.uses_relay,
            'relay_priority': self.relay_priority,
            'configured_fields': sorted(creds.keys()),
            'settings': self.public_settings(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_tested_at': self.last_tested_at.isoformat() if self.last_tested_at else None,
            'last_test_ok': self.last_test_ok,
        }

    def __repr__(self):
        return f'<EmailProviderConnection {self.id} {self.provider} default={self.is_default}>'
