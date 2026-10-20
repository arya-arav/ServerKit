"""ORM models for the ServerKit Notification Bus.

``Notification``         — the durable record of *what happened* (one row per
                           emitted event). Powers the in-app notification
                           center / bell and the delivery history.

``NotificationDelivery`` — one attempt to tell *one recipient* on *one
                           channel*. This is the per-channel tracking that the
                           legacy fire-and-forget sender never had: status,
                           attempts, error, provider message-id, sent/read time.
"""
import json
from datetime import datetime

from app import db


class Notification(db.Model):
    __tablename__ = 'notifications'

    # Severity levels (shared vocabulary with NotificationPreferences/monitoring)
    SEVERITY_CRITICAL = 'critical'
    SEVERITY_WARNING = 'warning'
    SEVERITY_INFO = 'info'
    SEVERITY_SUCCESS = 'success'
    SEVERITY_TEST = 'test'

    id = db.Column(db.Integer, primary_key=True)

    # Event identity — e.g. 'backup.completed', 'security.alert'. Selects the
    # template via the catalog.
    event_key = db.Column(db.String(120), nullable=False, index=True)

    # Coarse bucket used for per-user opt-out: system | security | backups | apps
    category = db.Column(db.String(40), default='system', index=True)
    severity = db.Column(db.String(20), default='info', index=True)

    # Rendered, channel-agnostic summary (the subject line / bell headline).
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text)  # short plain-text summary for the in-app list

    # The context dict passed to notify.send(), used by templates. JSON-encoded.
    data_json = db.Column(db.Text)

    # Human-readable description of the intended audience, for debugging/audit
    # (e.g. 'admins', 'user:42', 'ops@example.com'). Not used for routing.
    audience = db.Column(db.String(255))

    # Deep link to this notification's subject. Stored RELATIVE
    # (e.g. '/domains') and computed at send time — from the catalog link builder
    # or a producer override — so a later route move never breaks old rows. The
    # bell/history navigate here; email/chat resolve it to an absolute URL.
    action_path = db.Column(db.String(512))
    action_label = db.Column(db.String(120))

    # Correlation ID for grouping this notification with related telemetry events.
    correlation_id = db.Column(db.String(64), nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    deliveries = db.relationship(
        'NotificationDelivery',
        backref='notification',
        cascade='all, delete-orphan',
        lazy='dynamic',
    )

    def set_data(self, data):
        self.data_json = json.dumps(data or {})

    def get_data(self):
        try:
            return json.loads(self.data_json) if self.data_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def to_dict(self, include_data=False):
        out = {
            'id': self.id,
            'event_key': self.event_key,
            'category': self.category,
            'severity': self.severity,
            'title': self.title,
            'body': self.body,
            'audience': self.audience,
            'action_path': self.action_path,
            'action_label': self.action_label,
            'correlation_id': self.correlation_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_data:
            out['data'] = self.get_data()
        return out

    def __repr__(self):
        return f'<Notification {self.id} {self.event_key} {self.severity}>'


class NotificationDelivery(db.Model):
    __tablename__ = 'notification_deliveries'

    # Outcome of the delivery attempt (NOT read-state — that's read_at).
    STATUS_PENDING = 'pending'    # queued, not yet attempted
    STATUS_SENT = 'sent'          # handed to the channel successfully
    STATUS_FAILED = 'failed'      # exhausted retries (dead-letter on the queue)
    STATUS_SKIPPED = 'skipped'    # intentionally not sent (e.g. no target)
    # Held for the periodic digest: a digestable email or a
    # quiet-hours catch-up. Never enqueued; the digest job groups these, sends
    # ONE branded email, then flips them to STATUS_SENT.
    STATUS_QUEUED_DIGEST = 'queued_digest'

    # Channels
    CHANNEL_INAPP = 'inapp'
    CHANNEL_EMAIL = 'email'
    CHANNEL_SLACK = 'slack'
    CHANNEL_DISCORD = 'discord'
    CHANNEL_TELEGRAM = 'telegram'
    CHANNEL_WEBHOOK = 'webhook'

    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(
        db.Integer, db.ForeignKey('notifications.id'), nullable=False, index=True
    )

    # Recipient user (nullable: bare-email / global-webhook recipients have no user).
    recipient_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True, index=True
    )

    channel = db.Column(db.String(40), nullable=False, index=True)
    # The concrete destination for this channel: email address, telegram chat id,
    # webhook url, etc. Null for in-app (the user_id is the destination).
    target = db.Column(db.String(512))

    status = db.Column(db.String(20), default=STATUS_PENDING, index=True)
    attempts = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)
    # Provider-assigned id (SendGrid/Postmark/etc.) for later bounce correlation.
    provider_message_id = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    sent_at = db.Column(db.DateTime)
    # In-app read-state. Null = unread (the bell's unread count counts these).
    read_at = db.Column(db.DateTime)
    # Removed from the recipient's bell and inbox; the delivery log keeps it.
    # Fork: arya-arav/ServerKit.
    dismissed_at = db.Column(db.DateTime)

    recipient = db.relationship('User', foreign_keys=[recipient_user_id])

    def to_dict(self):
        return {
            'id': self.id,
            'notification_id': self.notification_id,
            'recipient_user_id': self.recipient_user_id,
            'channel': self.channel,
            'target': self.target,
            'status': self.status,
            'attempts': self.attempts,
            'error': self.error,
            'provider_message_id': self.provider_message_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'read_at': self.read_at.isoformat() if self.read_at else None,
        }

    def __repr__(self):
        return f'<NotificationDelivery {self.id} {self.channel} {self.status}>'
