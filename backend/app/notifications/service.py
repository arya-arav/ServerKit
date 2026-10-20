"""NotificationBusService — the producer side of the Notification Bus.

``send()`` is the one entry point. It is non-blocking: it records the event,
plans per-recipient deliveries (honoring each user's preferences), persists the
delivery rows, and enqueues the non-instant ones on the Queue Bus. A background
``NotificationConsumer`` then renders + transmits each delivery.

    from app.notifications.service import NotificationBusService
    NotificationBusService.send('backup.completed', to='admins',
                                data={'app': 'blog', 'size': '2.3 GB'})
"""
import logging
from datetime import datetime, time

from app import db
from app.models.notification_preferences import NotificationPreferences
from app.notifications import catalog
from app.notifications.models import Notification, NotificationDelivery
from app.queue_bus.service import QueueBusService
from app.services.telemetry_service import TelemetryService, generate_correlation_id

logger = logging.getLogger(__name__)

GROUP_SLUG = 'serverkit-system'
QUEUE_SLUG = 'notifications'
QUEUE_CONFIG = {'max_attempts': 4, 'visibility_timeout_ms': 60000}


class NotificationBusService:

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @classmethod
    def send(cls, event, to, data=None, channels=None, severity=None, title=None,
             category=None, action_path=None, action_label=None):
        """Emit a notification.

        event    — catalog key, e.g. 'backup.completed' (unknown keys render
                   via the generic template).
        to        — User | user-id | email | 'admins' | 'all' | list of those.
        data      — context dict for templates (and the in-app body).
        channels  — None = preference-driven (honor each user's prefs/quiet
                    hours); an explicit list = directed transactional send
                    (e.g. ['email']) that bypasses preference gating.
        severity  — override the catalog default.
        title     — override the computed title/subject.
        category  — override the catalog category (system/security/backups/apps).
        action_path  — deep link (relative path) for this notification; when
                    omitted, resolved from the catalog link builder. Persisted
                    on the row so a later route move never breaks old rows.
        action_label — button label for the deep link (falls back to the
                    catalog default when a path is resolved).

        Returns {'notification_id', 'deliveries'} immediately.
        """
        data = data or {}
        meta = catalog.resolve(event, data, severity=severity, title=title)
        severity = meta['severity']
        category = category or meta['category']

        # Compute + persist the deep link at send time (producer override wins).
        if action_path is None:
            action_path, link_label = catalog.link_for(event, data)
            if action_label is None:
                action_label = link_label

        notification = Notification(
            event_key=event,
            category=category,
            severity=severity,
            title=meta['title'],
            body=data.get('summary') or data.get('message'),
            audience=cls._describe(to),
            action_path=action_path,
            action_label=action_label,
            correlation_id=generate_correlation_id(),
        )
        notification.set_data(data)
        db.session.add(notification)
        db.session.flush()  # assign notification.id

        # Emit telemetry event for the notification lifecycle start.
        TelemetryService.emit(
            source='notification',
            event_type='notification.queued',
            message=f'Notification queued: {meta["title"]}',
            severity=severity if severity in ('warning', 'error', 'critical') else 'info',
            actor_user_id=None,
            correlation_id=notification.correlation_id,
            payload={
                'notification_id': notification.id,
                'event_key': event,
                'category': category,
                'audience': notification.audience,
            },
            commit=False,
        )

        seen = set()
        to_enqueue = []
        inapp_deliveries = []
        now = datetime.utcnow()
        for recipient in cls._resolve_recipients(to):
            for channel, target in cls._plan(recipient, channels, severity, category):
                user = recipient['user']
                key = (user.id if user else None, channel, target)
                if key in seen:
                    continue
                seen.add(key)
                delivery = NotificationDelivery(
                    notification_id=notification.id,
                    recipient_user_id=user.id if user else None,
                    channel=channel,
                    target=target,
                )
                # In-app has nothing to transmit — the row IS the notification.
                # Mark it sent now so the bell is instant; skip the queue.
                if channel == NotificationDelivery.CHANNEL_INAPP:
                    delivery.status = NotificationDelivery.STATUS_SENT
                    delivery.sent_at = now
                    inapp_deliveries.append((user.id, delivery))
                else:
                    delivery.status = NotificationDelivery.STATUS_PENDING
                    to_enqueue.append(delivery)
                db.session.add(delivery)

        # Org-level chat/webhook connections. A preference-
        # driven notification (channels is None) fans out ONCE per active,
        # category-matched connection as an org-level delivery (no recipient
        # user). Directed transactional sends (explicit channels) are
        # per-recipient only and never trigger org chat.
        if channels is None:
            from app.services.chat_webhook_service import ChatWebhookService
            for conn in ChatWebhookService.active_for_category(category):
                target = f'conn:{conn.id}'
                key = (None, conn.kind, target)
                if key in seen:
                    continue
                seen.add(key)
                delivery = NotificationDelivery(
                    notification_id=notification.id,
                    recipient_user_id=None,
                    channel=conn.kind,
                    target=target,
                    status=NotificationDelivery.STATUS_PENDING,
                )
                to_enqueue.append(delivery)
                db.session.add(delivery)

        db.session.commit()

        priority = 10 if severity == 'critical' else 0
        for delivery in to_enqueue:
            try:
                cls._enqueue(delivery.id, priority=priority)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error('Failed to enqueue notification delivery %s: %s', delivery.id, exc)

        # Push the in-app deliveries to any open tabs in real time.
        for user_id, delivery in inapp_deliveries:
            cls._emit_inapp(user_id, notification, delivery.id)

        return {'notification_id': notification.id, 'deliveries': len(seen)}

    @staticmethod
    def _emit_inapp(user_id, notification, delivery_id):
        """Best-effort Socket.IO push of a new in-app notification."""
        try:
            from app import socketio
            payload = notification.to_dict()
            payload['delivery_id'] = delivery_id
            payload['read'] = False
            from app.sockets_rooms import user_room
            socketio.emit('notification', payload, room=user_room(user_id))
        except Exception as exc:  # pragma: no cover - socket is best-effort
            logger.debug('Socket emit for notification skipped: %s', exc)

    # ------------------------------------------------------------------
    # Legacy system broadcast (what NotificationService.send_all delegates to)
    # ------------------------------------------------------------------
    @classmethod
    def broadcast_system(cls, alerts):
        """Route a legacy system alert to the configured *system* channels (the
        global notifications.json webhooks/email) through the bus — non-blocking,
        recorded, retried. Recipients are unchanged; only the delivery path is."""
        alerts = alerts or []
        from app.services.notification_service import NotificationService
        config = NotificationService.get_config()
        hostname = NotificationService.get_hostname()

        sevs = [a.get('severity', 'info') for a in alerts]
        if 'critical' in sevs:
            severity = Notification.SEVERITY_CRITICAL
        elif 'warning' in sevs:
            severity = Notification.SEVERITY_WARNING
        elif 'test' in sevs:
            severity = Notification.SEVERITY_TEST
        else:
            severity = Notification.SEVERITY_INFO
        title = f"[{severity.upper()}] ServerKit: {len(alerts)} alert(s) on {hostname}"

        notification = Notification(
            event_key='monitoring.alert', category='system', severity=severity,
            title=title, body=(alerts[0].get('message') if alerts else None),
            audience='system channels',
        )
        notification.set_data({'alerts': alerts, 'hostname': hostname})
        db.session.add(notification)
        db.session.flush()

        targets = []
        email = config.get('email', {})
        if email.get('enabled') and email.get('smtp_host') and cls._sys_allows(email, severity):
            for addr in email.get('to_emails', []) or []:
                targets.append((NotificationDelivery.CHANNEL_EMAIL, addr))
        discord = config.get('discord', {})
        if discord.get('enabled') and discord.get('webhook_url') and cls._sys_allows(discord, severity):
            targets.append((NotificationDelivery.CHANNEL_DISCORD, discord['webhook_url']))
        slack = config.get('slack', {})
        if slack.get('enabled') and slack.get('webhook_url') and cls._sys_allows(slack, severity):
            targets.append((NotificationDelivery.CHANNEL_SLACK, slack['webhook_url']))
        telegram = config.get('telegram', {})
        if telegram.get('enabled') and telegram.get('chat_id') and cls._sys_allows(telegram, severity):
            targets.append((NotificationDelivery.CHANNEL_TELEGRAM, telegram['chat_id']))
        webhook = config.get('generic_webhook', {})
        if webhook.get('enabled') and webhook.get('url') and cls._sys_allows(webhook, severity):
            targets.append((NotificationDelivery.CHANNEL_WEBHOOK, webhook['url']))

        rows = []
        for channel, target in targets:
            delivery = NotificationDelivery(
                notification_id=notification.id, recipient_user_id=None,
                channel=channel, target=target, status=NotificationDelivery.STATUS_PENDING,
            )
            db.session.add(delivery)
            rows.append(delivery)
        db.session.commit()

        priority = 10 if severity == Notification.SEVERITY_CRITICAL else 0
        for delivery in rows:
            try:
                cls._enqueue(delivery.id, priority=priority)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error('Failed to enqueue system delivery %s: %s', delivery.id, exc)

        return {'success': True, 'results': {}, 'queued': len(rows),
                'notification_id': notification.id}

    @staticmethod
    def _sys_allows(channel_cfg, severity):
        notify_on = channel_cfg.get('notify_on', ['critical', 'warning'])
        return severity in notify_on or severity == Notification.SEVERITY_TEST

    # ------------------------------------------------------------------
    # Queue plumbing
    # ------------------------------------------------------------------
    @classmethod
    def _enqueue(cls, delivery_id, priority=0):
        QueueBusService.ensure_queue(GROUP_SLUG, QUEUE_SLUG, config=QUEUE_CONFIG)
        return QueueBusService.send(
            GROUP_SLUG, QUEUE_SLUG, {'delivery_id': delivery_id}, priority=priority,
        )

    # ------------------------------------------------------------------
    # Recipient resolution
    # ------------------------------------------------------------------
    @classmethod
    def _resolve_recipients(cls, to):
        """Normalize ``to`` into a list of {'user': User|None, 'email': str|None}."""
        from app.models.user import User

        if to is None:
            return []
        if isinstance(to, (list, tuple, set)):
            out = []
            for item in to:
                out.extend(cls._resolve_recipients(item))
            return out
        if isinstance(to, User):
            return [{'user': to, 'email': None}]
        if isinstance(to, bool):
            return []
        if isinstance(to, int):
            user = User.query.get(to)
            return [{'user': user, 'email': None}] if user else []
        if isinstance(to, str):
            value = to.strip()
            low = value.lower()
            if low == 'admins':
                return [{'user': u, 'email': None}
                        for u in User.query.filter_by(role='admin').all()]
            if low in ('all', 'everyone'):
                return [{'user': u, 'email': None} for u in User.query.all()]
            if '@' in value:
                return [{'user': None, 'email': value}]
            user = User.query.filter_by(username=value).first()
            return [{'user': user, 'email': None}] if user else []
        return []

    # ------------------------------------------------------------------
    # Delivery planning
    # ------------------------------------------------------------------
    @classmethod
    def _plan(cls, recipient, channels, severity, category):
        """Return a list of (channel, target) for one recipient."""
        user = recipient['user']
        email = recipient['email']

        # Bare email recipient — only the email channel applies.
        if user is None:
            if email and (channels is None or 'email' in channels):
                return [(NotificationDelivery.CHANNEL_EMAIL, email)]
            return []

        prefs = NotificationPreferences.get_or_create(user.id)
        directed = channels is not None

        if directed:
            wanted = list(channels)
        else:
            if not prefs.enabled:
                return []
            if not cls._severity_allowed(prefs, severity):
                return []
            if not cls._category_allowed(prefs, category):
                return []
            if cls._suppressed_by_quiet_hours(prefs, severity):
                return []
            # The in-app bell is always on for an enabled user.
            wanted = list(set(prefs.get_channels()) | {NotificationDelivery.CHANNEL_INAPP})

        plan = []
        for channel in wanted:
            target = cls._target_for(channel, user, prefs)
            if channel != NotificationDelivery.CHANNEL_INAPP and not target:
                continue  # no destination for this channel/user
            plan.append((channel, target))
        return plan

    @staticmethod
    def _target_for(channel, user, prefs):
        if channel == NotificationDelivery.CHANNEL_INAPP:
            return None
        if channel == NotificationDelivery.CHANNEL_EMAIL:
            return (prefs.email if prefs else None) or user.email
        if channel == NotificationDelivery.CHANNEL_DISCORD:
            return prefs.discord_webhook if prefs else None
        if channel == NotificationDelivery.CHANNEL_TELEGRAM:
            return prefs.telegram_chat_id if prefs else None
        # slack / generic webhook are system-wide today — no per-user target.
        return None

    # ------------------------------------------------------------------
    # Preference gating
    # ------------------------------------------------------------------
    @staticmethod
    def _severity_allowed(prefs, severity):
        if severity == Notification.SEVERITY_CRITICAL:
            return True  # critical always reaches the user
        return severity in prefs.get_severities()

    @staticmethod
    def _category_allowed(prefs, category):
        return prefs.get_categories().get(category, True)

    @staticmethod
    def _suppressed_by_quiet_hours(prefs, severity):
        if not prefs.quiet_hours_enabled or severity == Notification.SEVERITY_CRITICAL:
            return False
        try:
            sh, sm = (int(x) for x in (prefs.quiet_hours_start or '22:00').split(':'))
            eh, em = (int(x) for x in (prefs.quiet_hours_end or '08:00').split(':'))
        except (ValueError, AttributeError):
            return False
        now = datetime.now().time()
        start, end = time(sh, sm), time(eh, em)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # window wraps midnight

    # ------------------------------------------------------------------
    @staticmethod
    def _describe(to):
        if isinstance(to, str):
            return to[:255]
        if isinstance(to, int):
            return f'user:{to}'
        if isinstance(to, (list, tuple, set)):
            return f'{len(to)} recipients'
        user_id = getattr(to, 'id', None)
        return f'user:{user_id}' if user_id is not None else 'recipients'

    # ------------------------------------------------------------------
    # In-app notification center (the bell + history)
    # ------------------------------------------------------------------
    @staticmethod
    def _inbox_query(user_id):
        return (
            NotificationDelivery.query
            .filter_by(recipient_user_id=user_id,
                       channel=NotificationDelivery.CHANNEL_INAPP)
            .filter(NotificationDelivery.dismissed_at.is_(None))
        )

    @classmethod
    def dismiss(cls, user_id, delivery_id):
        """Remove one in-app notification from the user's bell and inbox.

        The delivery row stays for the admin delivery log. Returns True if it
        was found. Fork: arya-arav/ServerKit.
        """
        delivery = cls._inbox_query(user_id).filter_by(id=delivery_id).first()
        if not delivery:
            return False
        now = datetime.utcnow()
        delivery.dismissed_at = now
        if delivery.read_at is None:
            delivery.read_at = now
        db.session.commit()
        return True

    @classmethod
    def clear(cls, user_id, include_unread=False):
        """Remove the user's read notifications (all of them with
        ``include_unread``). Returns the count. Fork: arya-arav/ServerKit."""
        now = datetime.utcnow()
        query = cls._inbox_query(user_id)
        if not include_unread:
            query = query.filter(NotificationDelivery.read_at.isnot(None))
        cleared = query.update(
            {NotificationDelivery.dismissed_at: now,
             NotificationDelivery.read_at: db.func.coalesce(NotificationDelivery.read_at, now)},
            synchronize_session=False)
        db.session.commit()
        return cleared

    @classmethod
    def inbox(cls, user_id, limit=20, offset=0, unread_only=False,
              category=None, severity=None):
        """Return the current user's in-app notifications, newest first.

        Optional ``category`` / ``severity`` filters narrow the history to one
        notification bucket (joined against the parent :class:`Notification`).
        """
        query = cls._inbox_query(user_id)
        if unread_only:
            query = query.filter(NotificationDelivery.read_at.is_(None))
        query = cls._apply_notification_filters(query, category, severity)
        rows = (query.order_by(NotificationDelivery.created_at.desc())
                .limit(limit).offset(offset).all())
        items = []
        for delivery in rows:
            notification = delivery.notification
            item = notification.to_dict() if notification else {}
            item.update({
                'delivery_id': delivery.id,
                'read': delivery.read_at is not None,
                'created_at': (delivery.created_at.isoformat()
                               if delivery.created_at else item.get('created_at')),
            })
            items.append(item)
        return items

    @classmethod
    def unread_count(cls, user_id):
        return cls._inbox_query(user_id).filter(
            NotificationDelivery.read_at.is_(None)
        ).count()

    @classmethod
    def mark_read(cls, user_id, delivery_id):
        """Mark one in-app notification read. Returns True if it changed."""
        delivery = cls._inbox_query(user_id).filter_by(id=delivery_id).first()
        if not delivery:
            return False
        if delivery.read_at is None:
            delivery.read_at = datetime.utcnow()
            db.session.commit()
        return True

    @classmethod
    def mark_all_read(cls, user_id, category=None, severity=None):
        """Mark every unread in-app notification read. With ``category`` /
        ``severity``, only that group is touched. Returns the count."""
        now = datetime.utcnow()
        query = (cls._inbox_query(user_id)
                 .filter(NotificationDelivery.read_at.is_(None)))
        if category or severity:
            # A join is needed to filter on the parent notification, and a bulk
            # UPDATE with a join isn't portable — resolve ids, then update those.
            query = cls._apply_notification_filters(query, category, severity)
            ids = [row.id for row in query.with_entities(NotificationDelivery.id).all()]
            if not ids:
                return 0
            NotificationDelivery.query.filter(NotificationDelivery.id.in_(ids)).update(
                {NotificationDelivery.read_at: now}, synchronize_session=False)
            db.session.commit()
            return len(ids)
        updated = query.update({NotificationDelivery.read_at: now},
                               synchronize_session=False)
        db.session.commit()
        return updated

    @staticmethod
    def _apply_notification_filters(query, category=None, severity=None):
        """Join the parent :class:`Notification` and filter by category/severity
        when either is given."""
        if not category and not severity:
            return query
        query = query.join(
            Notification, NotificationDelivery.notification_id == Notification.id)
        if category:
            query = query.filter(Notification.category == category)
        if severity:
            query = query.filter(Notification.severity == severity)
        return query

    # ------------------------------------------------------------------
    # Delivery log / ops (admin)
    # ------------------------------------------------------------------
    @classmethod
    def delivery_log(cls, status=None, channel=None, limit=50, offset=0):
        """Admin view: recent deliveries across all users, newest first."""
        query = NotificationDelivery.query
        if status:
            query = query.filter_by(status=status)
        if channel:
            query = query.filter_by(channel=channel)
        rows = (query.order_by(NotificationDelivery.created_at.desc())
                .limit(limit).offset(offset).all())
        out = []
        for delivery in rows:
            notification = delivery.notification
            item = delivery.to_dict()
            item['event_key'] = notification.event_key if notification else None
            item['title'] = notification.title if notification else None
            item['notif_severity'] = notification.severity if notification else None
            out.append(item)
        return out

    @classmethod
    def delivery_stats(cls):
        from sqlalchemy import func
        by_status = dict(
            db.session.query(NotificationDelivery.status, func.count())
            .group_by(NotificationDelivery.status).all()
        )
        by_channel = dict(
            db.session.query(NotificationDelivery.channel, func.count())
            .group_by(NotificationDelivery.channel).all()
        )
        return {
            'by_status': by_status,
            'by_channel': by_channel,
            'total': sum(by_status.values()),
        }

    @classmethod
    def retry_delivery(cls, delivery_id):
        """Re-queue a failed/skipped delivery. Returns the dict, or None."""
        delivery = NotificationDelivery.query.get(delivery_id)
        if not delivery:
            return None
        if delivery.channel == NotificationDelivery.CHANNEL_INAPP:
            delivery.status = NotificationDelivery.STATUS_SENT
            delivery.sent_at = datetime.utcnow()
            delivery.error = None
            db.session.commit()
            return delivery.to_dict()
        delivery.status = NotificationDelivery.STATUS_PENDING
        delivery.error = None
        db.session.commit()
        cls._enqueue(delivery.id, priority=5)
        return delivery.to_dict()
