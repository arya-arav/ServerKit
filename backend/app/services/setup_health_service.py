"""Setup Health registry — "how set up is this panel".

A small, cheap registry of setup-health *items* the operator should complete to
get a fully-working panel. Each item is a doctor-style check
(``key``/``title``/``status``/``detail``/``repairable``/``repair_ref``) plus the
setup extras ``section``/``severity``/``scope``/``why``/``fix`` so the same items
render in the Setup card, in Monitoring → Doctor's ``setup.*`` section, and in the
personal "secure your account" nudge.

Severity is graded by Decision 2's *silent-breakage* rule: an item is
``critical`` only when leaving it undone silently breaks something the operator
already set in motion (e.g. per-site DNS mode with no provider/IP — sites won't
resolve; no host firewall — every container port a deploy maps onto the host is
world-reachable and nothing in the UI says so). Everything else is
``recommended``. **HTTPS/TLS items are never critical** — SSL is optional by
decree.

Scope is ``panel`` (a whole-instance setting, admin-only) or ``user`` (a personal
item like account 2FA). Panel snoozes live in a SettingsService map; personal
snoozes live on ``users.setup_snoozes``. A snoozed item still renders, just muted
— it drops out of the open counts and never triggers the nag.

The weekly nag (Phase 6) fires ``setup.incomplete`` to admins only while
non-snoozed critical items exist AND the critical set changed since the last
notice (fingerprint throttle); the marker resets once the criticals clear.
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Phase-6 constants (imported by the API + the nag test) ───────────────────
NAG_JOB_KIND = 'setup.health.nag'
NAG_SCHEDULE_NAME = 'setup-health-nag'
NAG_MARKER_KEY = 'setup_health_nag_marker'
PANEL_SNOOZE_KEY = 'setup_health_panel_snoozes'

# Weekly cadence (seconds) for the nag schedule.
NAG_INTERVAL_SECONDS = 604800

# Item severities.
CRITICAL = 'critical'
RECOMMENDED = 'recommended'

# Every setup item key mapped to its scope. Drives _item_scope() (snooze RBAC)
# and lets the API reject an unknown key before touching storage.
_ITEM_SCOPES = {
    'setup.public_ip': 'panel',
    'setup.base_domain': 'panel',
    'setup.dns_provider': 'panel',
    'setup.email_delivery': 'panel',
    'setup.backup_policy': 'panel',
    'setup.backup_offsite': 'panel',
    'setup.firewall': 'panel',
    'setup.fail2ban': 'panel',
    'setup.canonical_domain': 'panel',
    'setup.wildcard_cert': 'panel',
    'setup.account_security': 'user',
}

# Where install.sh puts this host's panel config (install.sh CONFIG_DIR). Its
# presence is the marker for "ServerKit was installed onto THIS host", which is
# what makes the host-firewall items applicable at all — see
# SetupHealthService._manages_host_firewall(). Module-level so tests can point
# it somewhere harmless.
HOST_CONFIG_DIR = '/etc/serverkit'


def _link(to):
    """A deep-link fix descriptor pointing the operator at where to finish."""
    return {'kind': 'link', 'to': to}


def _item(key, title, status, detail, severity, fix, scope='panel', why=None,
          repairable=False, repair_ref=None):
    """Build a normalized setup-health item (doctor ``_check`` shape + extras)."""
    return {
        # doctor _check shape
        'key': key,
        'title': title,
        'status': status,          # 'ok' | 'warn' | 'fail'
        'detail': detail,
        'repairable': bool(repairable),
        'repair_ref': repair_ref,
        # setup extras
        'section': 'setup',
        'severity': severity,      # 'critical' | 'recommended'
        'scope': scope,            # 'panel' | 'user'
        'why': why or detail,
        'fix': fix,
    }


class SetupHealthService:
    """Evaluate the setup-health registry, snooze/unsnooze items, run the nag."""

    # ------------------------------------------------------------------ #
    # Small settings helpers (read raw settings, no dev fallbacks — an item
    # like base_domain must reflect what the operator actually set, not the
    # SITES_BASE_DOMAIN dev default).
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get(key, default=None):
        from app.models.system_settings import SystemSettings
        val = SystemSettings.get(key, default)
        return val if val is not None else default

    @staticmethod
    def _default_base_row():
        """The default row of the base-domain registry, or None.

        Settings → Site settings writes base domains to the registry, not to
        the legacy ``sites_*`` settings, so reading only the settings reported
        "no base domain" on a configured panel. Fork: arya-arav/ServerKit.
        """
        try:
            from app.services.site_base_domain_service import SiteBaseDomainService
            return SiteBaseDomainService.default()
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _base_domain(cls):
        row = cls._default_base_row()
        if row:
            return row.domain.strip().lower()
        return (cls._get('sites_base_domain', '') or '').strip().lower()

    @classmethod
    def _dns_mode(cls):
        row = cls._default_base_row()
        if row:
            return (row.dns_mode or '').strip().lower()
        return (cls._get('sites_dns_mode', '') or '').strip().lower()

    @classmethod
    def _server_ip(cls):
        return (cls._get('server_public_ip', '') or '').strip()

    @classmethod
    def _https_enabled(cls):
        row = cls._default_base_row()
        if row:
            return bool(row.https_enabled)
        return bool(cls._get('sites_https_enabled', False))

    # ------------------------------------------------------------------ #
    # Item scope + snooze storage
    # ------------------------------------------------------------------ #

    @classmethod
    def _item_scope(cls, key):
        """'panel' | 'user' for a known item key, else None."""
        return _ITEM_SCOPES.get(key)

    @staticmethod
    def _panel_snoozes():
        """The panel snooze map ``{key: iso_expiry}`` from the settings map."""
        from app.services.settings_service import SettingsService
        raw = SettingsService.get(PANEL_SNOOZE_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _user_snoozes(user):
        if not user or not getattr(user, 'setup_snoozes', None):
            return {}
        try:
            data = json.loads(user.setup_snoozes)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _active_until(snooze_map, key, now):
        """The (non-expired) snooze expiry for ``key``, or None."""
        iso = (snooze_map or {}).get(key)
        if not iso:
            return None
        try:
            expiry = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None
        return iso if expiry > now else None

    # ------------------------------------------------------------------ #
    # The probe matrix — one builder per item
    # ------------------------------------------------------------------ #

    @classmethod
    def _panel_items(cls):
        base = cls._base_domain()
        mode = cls._dns_mode()
        ip = cls._server_ip()
        per_site = bool(base) and mode == 'per-site'
        items = []

        # --- server public IP ---
        if ip:
            items.append(_item(
                'setup.public_ip', 'Server public IP', 'ok',
                f'Server public IP is set ({ip}).', RECOMMENDED,
                _link('/settings')))
        elif per_site:
            items.append(_item(
                'setup.public_ip', 'Server public IP', 'fail',
                'DNS mode is per-site but no server public IP is set, so each '
                "managed site's A record can't be auto-created — new sites won't "
                'resolve.', CRITICAL, _link('/settings')))
        else:
            items.append(_item(
                'setup.public_ip', 'Server public IP', 'warn',
                'No server public IP set. It is used to auto-create A records for '
                'managed domains.', RECOMMENDED, _link('/settings')))

        # --- managed-sites base domain ---
        if base:
            items.append(_item(
                'setup.base_domain', 'Managed-sites base domain', 'ok',
                f'Managed sites are published under {base}.', RECOMMENDED,
                _link('/settings')))
        else:
            items.append(_item(
                'setup.base_domain', 'Managed-sites base domain', 'warn',
                'No base domain set — new sites are only reachable at '
                'localhost:<port>. Set one so each site publishes at '
                '<name>.<domain>.', RECOMMENDED, _link('/settings')))

        # --- DNS provider ---
        has_provider = cls._has_dns_provider()
        if has_provider:
            items.append(_item(
                'setup.dns_provider', 'DNS provider connected', 'ok',
                'A DNS provider is connected for auto-managing records.',
                RECOMMENDED, _link('/settings/connections')))
        elif per_site:
            items.append(_item(
                'setup.dns_provider', 'DNS provider connected', 'fail',
                'DNS mode is per-site but no DNS provider is connected, so each '
                "site's A record can't be auto-created — new sites won't resolve.",
                CRITICAL, _link('/settings/connections')))
        else:
            items.append(_item(
                'setup.dns_provider', 'DNS provider connected', 'warn',
                'No DNS provider connected. Connect one to auto-create and heal '
                'DNS records for managed domains.', RECOMMENDED,
                _link('/settings/connections')))

        # --- email delivery ---
        items.append(cls._email_delivery_item())

        # --- backup policy ---
        items.append(cls._backup_policy_item())

        # --- offsite backup ---
        items.append(cls._backup_offsite_item())

        # --- host hardening (only on a host this panel actually owns) ---
        for probe in (cls._firewall_item, cls._fail2ban_item):
            probed = probe()
            if probed is not None:
                items.append(probed)

        # --- canonical panel domain ---
        canonical = (cls._get('canonical_domain', '') or '').strip()
        if canonical:
            items.append(_item(
                'setup.canonical_domain', 'Canonical panel domain', 'ok',
                f'Panel canonical domain is set ({canonical}).', RECOMMENDED,
                _link('/settings')))
        else:
            items.append(_item(
                'setup.canonical_domain', 'Canonical panel domain', 'warn',
                'No canonical panel domain set. Setting one gives the panel a '
                'stable public URL for agent installs and CORS.', RECOMMENDED,
                _link('/settings')))

        # --- wildcard certificate (only once wildcard HTTPS is switched on;
        #     never critical — SSL is optional by decree) ---
        cert_item = cls._wildcard_cert_item(base, mode)
        if cert_item is not None:
            items.append(cert_item)

        return items

    @classmethod
    def _has_dns_provider(cls):
        try:
            from app.models.email import DNSProviderConfig
            return DNSProviderConfig.query.count() > 0
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def _email_delivery_item(cls):
        try:
            from app.models.email_provider import EmailProviderConnection
            providers = EmailProviderConnection.query.filter_by(
                is_active=True, uses_notifications=True).all()
        except Exception:  # noqa: BLE001
            providers = []
        tested = [p for p in providers if p.last_test_ok]
        if tested:
            return _item(
                'setup.email_delivery', 'Email delivery', 'ok',
                'A verified email delivery path is configured.', RECOMMENDED,
                _link('/settings/connections'))
        if providers:
            return _item(
                'setup.email_delivery', 'Email delivery', 'warn',
                'An email provider is configured but its test send has not '
                'passed — notifications may not be delivered.', RECOMMENDED,
                _link('/settings/connections'))
        # The alert email channel in Settings → Notifications has its own SMTP
        # settings and sends without any provider connection. Fork: arya-arav/ServerKit.
        try:
            from app.services.notification_service import NotificationService
            channel = NotificationService.get_config().get('email') or {}
        except Exception:  # noqa: BLE001
            channel = {}
        if (channel.get('enabled') and channel.get('smtp_host')
                and channel.get('from_email') and channel.get('to_emails')):
            return _item(
                'setup.email_delivery', 'Email delivery', 'ok',
                f"Alert emails go out through {channel['smtp_host']} "
                '(Settings → Notifications).', RECOMMENDED,
                _link('/settings/notifications'))
        return _item(
            'setup.email_delivery', 'Email delivery', 'warn',
            'No email delivery path configured — the panel cannot send '
            'notifications or invitations.', RECOMMENDED,
            _link('/settings/connections'))

    @classmethod
    def _backup_policy_item(cls):
        try:
            from app.models.backup_policy import BackupPolicy
            enabled = BackupPolicy.query.filter_by(enabled=True).count() > 0
        except Exception:  # noqa: BLE001
            enabled = False
        if enabled:
            return _item(
                'setup.backup_policy', 'Backups scheduled', 'ok',
                'At least one enabled backup policy is protecting a target.',
                RECOMMENDED, _link('/backups'))
        # Schedules made on Backups → Schedules live in backups.json, not in
        # BackupPolicy; they back things up just the same. Fork: arya-arav/ServerKit.
        try:
            from app.services.backup_service import BackupService
            config = BackupService.get_config()
            schedules = [s for s in config.get('schedules', []) if s.get('enabled', True)]
            switched_on = bool(config.get('enabled'))
        except Exception:  # noqa: BLE001
            schedules, switched_on = [], False
        # The global switch gates every schedule (and the nightly off-site
        # cleanup): with it off, schedules exist but nothing runs.
        if schedules and not switched_on:
            return _item(
                'setup.backup_policy', 'Backups scheduled', 'warn',
                f'{len(schedules)} backup schedule(s) exist, but scheduled backups '
                'are switched off (Backups → Settings → Enable scheduled backups), '
                'so none of them run.', RECOMMENDED, _link('/backups/settings'))
        if schedules:
            return _item(
                'setup.backup_policy', 'Backups scheduled', 'ok',
                f'{len(schedules)} backup schedule(s) enabled.',
                RECOMMENDED, _link('/backups'))
        return _item(
            'setup.backup_policy', 'Backups scheduled', 'warn',
            'No enabled backup policy — nothing is being backed up on a '
            'schedule.', RECOMMENDED, _link('/backups'))

    @classmethod
    def _backup_offsite_item(cls):
        provider = None
        try:
            from app.services.storage_provider_service import StorageProviderService
            cfg = StorageProviderService.get_config() or {}
            provider = cfg.get('provider')
        except Exception:  # noqa: BLE001
            provider = None
        if provider and provider != 'local':
            return _item(
                'setup.backup_offsite', 'Offsite backup storage', 'ok',
                f'Offsite backup storage is configured ({provider}).',
                RECOMMENDED, _link('/settings/storage'))
        return _item(
            'setup.backup_offsite', 'Offsite backup storage', 'warn',
            'Backups are kept on this server only. Configure S3/B2 offsite '
            'storage so a backup survives losing the box.', RECOMMENDED,
            _link('/settings/storage'))

    # ------------------------------------------------------------------ #
    # Host hardening (firewall + fail2ban)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _in_container():
        """True when the panel is running inside a container.

        Mirrors install.sh's ``detect_container``. Inside a container the HOST
        owns netfilter, not this guest, so "no firewall here" says nothing
        about the box's exposure — reporting it would be a false alarm on every
        docker-compose deployment.
        """
        if os.path.exists('/.dockerenv'):
            return True
        probes = (
            ('/proc/1/cgroup', ('docker', 'containerd', 'lxc', 'kubepods')),
            ('/proc/1/environ', ('container=',)),
        )
        for path, needles in probes:
            try:
                with open(path, 'rb') as fh:
                    blob = fh.read().decode('utf-8', 'replace').lower()
            except OSError:
                continue
            if any(n in blob for n in needles):
                return True
        return False

    @classmethod
    def _manages_host_firewall(cls):
        """Whether the firewall / fail2ban items apply to this install at all.

        They only mean something on a Linux box that ServerKit was *installed
        onto* and whose inbound traffic this panel is responsible for:

        * **non-Linux** — a dev checkout on Windows/macOS. There is no host to
          judge (the wildcard-cert item guards itself the same way).
        * **containerised** — the host, not this guest, owns the firewall.
        * **no /etc/serverkit** — install.sh never ran here (dev checkout, CI
          runner, an unrelated machine), so a missing UFW is not this install's
          omission and the operator has nothing to act on.

        Failing the test drops the items entirely rather than reporting them
        ``ok``: an item nobody can act on is noise, and a fake ``ok`` would
        inflate the setup score.
        """
        if not sys.platform.startswith('linux'):
            return False
        if not os.path.isdir(HOST_CONFIG_DIR):
            return False
        return not cls._in_container()

    @classmethod
    def _firewall_item(cls):
        """Host firewall — the one setup item that is critical for *security*.

        Critical under the module's silent-breakage rule: ServerKit publishes
        managed apps by mapping container ports onto the host (8001-8999) and
        will happily stand up database containers on their stock ports. With no
        firewall those are reachable from the internet the moment they start.
        The operator set that in motion by deploying, nothing in the UI says the
        port is public, and the failure mode is a compromise rather than an
        error message — silent breakage, exactly what ``critical`` means here.

        (Not an HTTPS/TLS item, so the SSL-optional decree does not apply.)
        """
        if not cls._manages_host_firewall():
            return None
        try:
            from app.services.firewall_service import FirewallService
            status = FirewallService.get_status() or {}
        except Exception:  # noqa: BLE001 — a probe must never break the sweep
            return None

        if status.get('any_active'):
            name = status.get('active_firewall') or 'a firewall'
            return _item(
                'setup.firewall', 'Host firewall', 'ok',
                f'Inbound traffic is filtered by {name}.', RECOMMENDED,
                _link('/security/firewall'))
        return _item(
            'setup.firewall', 'Host firewall', 'fail',
            'No firewall is active on this server, so every port a managed app '
            'or database container maps onto the host is reachable from the '
            'internet. Turn on UFW (or firewalld) with default-deny inbound, '
            'allowing only SSH, 80 and 443. Snooze this item if your firewall '
            'lives at the provider edge instead.',
            CRITICAL, _link('/security/firewall'))

    @classmethod
    def _fail2ban_item(cls):
        """Brute-force protection.

        Recommended, not critical: the panel already throttles its own logins
        per-IP (``AUTH_IP_MAX_ATTEMPTS``), so a missing fail2ban thins out
        defence in depth rather than silently breaking something the operator
        started — which is the bar ``critical`` has to clear in this registry.
        """
        if not cls._manages_host_firewall():
            return None
        try:
            from app.services.fail2ban_jail_service import Fail2banJailService
            status = Fail2banJailService.get_fail2ban_status() or {}
        except Exception:  # noqa: BLE001
            return None

        if not status.get('installed'):
            return _item(
                'setup.fail2ban', 'Brute-force protection', 'warn',
                'fail2ban is not installed, so repeated SSH login failures are '
                'never banned at the host level and the panel\'s jail '
                'management stays inert.', RECOMMENDED,
                _link('/security/fail2ban'))
        if not status.get('service_running'):
            return _item(
                'setup.fail2ban', 'Brute-force protection', 'warn',
                'fail2ban is installed but not running, so none of its jails '
                'are banning anything.', RECOMMENDED, _link('/security/fail2ban'))

        jails = status.get('jails') or []
        if not any('ssh' in str(j).lower() for j in jails):
            return _item(
                'setup.fail2ban', 'Brute-force protection', 'warn',
                'fail2ban is running but no SSH jail is enabled — password '
                'spraying against sshd goes unthrottled.', RECOMMENDED,
                _link('/security/fail2ban'))
        return _item(
            'setup.fail2ban', 'Brute-force protection', 'ok',
            'fail2ban is running with an SSH jail.', RECOMMENDED,
            _link('/security/fail2ban'))

    @classmethod
    def _wildcard_cert_item(cls, base, mode):
        """The wildcard-cert item — only applicable once wildcard HTTPS is on for
        a wildcard base. Never critical (HTTPS is optional)."""
        if not base or mode != 'wildcard' or not cls._https_enabled():
            return None
        exists = True
        if sys.platform.startswith('linux'):
            try:
                from app.services.site_domain_service import SiteDomainService
                cert, _key = SiteDomainService.wildcard_cert_paths(base)
                exists = bool(cert and os.path.exists(cert))
            except Exception:  # noqa: BLE001
                exists = True
        if exists:
            return _item(
                'setup.wildcard_cert', 'Wildcard HTTPS certificate', 'ok',
                f'A wildcard certificate for *.{base} is present.', RECOMMENDED,
                _link('/settings'))
        return _item(
            'setup.wildcard_cert', 'Wildcard HTTPS certificate', 'warn',
            f'Wildcard HTTPS is enabled for *.{base} but its certificate is not '
            'present yet — managed subdomains fall back to HTTP until it issues.',
            RECOMMENDED, _link('/settings'))

    @classmethod
    def _user_items(cls, user):
        if not user:
            return []
        has_factor = bool(getattr(user, 'totp_enabled', False))
        if not has_factor:
            try:
                has_factor = user.passkeys.filter_by(is_active=True).count() > 0
            except Exception:  # noqa: BLE001
                has_factor = False
        if has_factor:
            item = _item(
                'setup.account_security', 'Secure your account', 'ok',
                'Your account has a second factor (passkey or authenticator).',
                RECOMMENDED, _link('/settings/security'), scope='user')
        else:
            item = _item(
                'setup.account_security', 'Secure your account', 'warn',
                'Add a passkey or authenticator app so a stolen password alone '
                "can't sign in as you.", RECOMMENDED,
                _link('/settings/security'), scope='user')
        return [item]

    # ------------------------------------------------------------------ #
    # Evaluate
    # ------------------------------------------------------------------ #

    @classmethod
    def evaluate(cls, scope='panel', user=None):
        """Evaluate the registry for ``scope`` ('panel' | 'user'), applying
        snoozes, and return ``{'items': [...], 'summary': {...}}``."""
        now = datetime.utcnow()
        if scope == 'user':
            items = cls._user_items(user)
            snooze_map = cls._user_snoozes(user)
        else:
            items = cls._panel_items()
            snooze_map = cls._panel_snoozes()

        for c in items:
            until = cls._active_until(snooze_map, c['key'], now)
            if until:
                c['snoozed'] = True
                c['snoozed_until'] = until
            else:
                c['snoozed'] = False
                c['snoozed_until'] = None

        return {'items': items, 'summary': cls._summary(items)}

    @staticmethod
    def _summary(items):
        weights = {CRITICAL: 3, RECOMMENDED: 1}
        ok = critical_open = recommended_open = snoozed = 0
        earned = total_weight = 0
        for c in items:
            w = weights.get(c['severity'], 1)
            total_weight += w
            is_ok = c['status'] == 'ok'
            is_snoozed = bool(c.get('snoozed'))
            if is_ok or is_snoozed:
                earned += w
            if is_ok:
                ok += 1
            elif is_snoozed:
                snoozed += 1
            elif c['severity'] == CRITICAL:
                critical_open += 1
            else:
                recommended_open += 1
        score = round(100 * earned / total_weight) if total_weight else 100
        return {
            'total': len(items),
            'ok': ok,
            'critical_open': critical_open,
            'recommended_open': recommended_open,
            'snoozed': snoozed,
            'score': max(0, min(100, score)),
        }

    # ------------------------------------------------------------------ #
    # Snooze / unsnooze
    # ------------------------------------------------------------------ #

    @classmethod
    def snooze(cls, key, days=30, user=None):
        """Snooze a setup item for ``days`` (mutes it). Panel items store into the
        settings map; personal items store on the user row. Returns a descriptor
        dict, or ``{'error': ...}`` for an unknown item."""
        scope = cls._item_scope(key)
        if scope is None:
            return {'error': f'Unknown setup item: {key}'}
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 30
        until = (datetime.utcnow() + timedelta(days=days)).isoformat()

        if scope == 'user':
            if user is None:
                return {'error': 'A user is required to snooze a personal item.'}
            from app import db
            snoozes = cls._user_snoozes(user)
            snoozes[key] = until
            user.setup_snoozes = json.dumps(snoozes)
            db.session.commit()
        else:
            from app.services.settings_service import SettingsService
            snoozes = cls._panel_snoozes()
            snoozes[key] = until
            SettingsService.set(PANEL_SNOOZE_KEY, json.dumps(snoozes))
        return {'success': True, 'key': key, 'scope': scope, 'snoozed_until': until}

    @classmethod
    def unsnooze(cls, key, user=None):
        """Clear a snooze so the item is active again."""
        scope = cls._item_scope(key)
        if scope is None:
            return {'error': f'Unknown setup item: {key}'}
        if scope == 'user':
            if user is None:
                return {'error': 'A user is required to unsnooze a personal item.'}
            from app import db
            snoozes = cls._user_snoozes(user)
            snoozes.pop(key, None)
            user.setup_snoozes = json.dumps(snoozes)
            db.session.commit()
        else:
            from app.services.settings_service import SettingsService
            snoozes = cls._panel_snoozes()
            snoozes.pop(key, None)
            SettingsService.set(PANEL_SNOOZE_KEY, json.dumps(snoozes))
        return {'success': True, 'key': key, 'scope': scope}

    # ------------------------------------------------------------------ #
    # Fingerprint + nag
    # ------------------------------------------------------------------ #

    @staticmethod
    def _critical_fingerprint(result):
        """A stable, human-readable fingerprint of the non-snoozed critical open
        items (the comma-joined sorted keys). Empty string when none."""
        keys = sorted(
            c['key'] for c in result['items']
            if c['severity'] == CRITICAL and c['status'] != 'ok'
            and not c.get('snoozed'))
        return ','.join(keys)

    @classmethod
    def fingerprint(cls):
        """The current non-snoozed-critical fingerprint (panel scope)."""
        return cls._critical_fingerprint(cls.evaluate(scope='panel'))

    @classmethod
    def run_nag_job(cls, job):
        """Weekly nag handler: notify admins once per critical-set change while
        non-snoozed critical items exist; reset the marker once they clear."""
        from app.services.settings_service import SettingsService
        result = cls.evaluate(scope='panel')
        critical_open = result['summary']['critical_open']
        marker = SettingsService.get(NAG_MARKER_KEY) or ''

        if critical_open == 0:
            if marker:
                SettingsService.set(NAG_MARKER_KEY, '')
            return {'notified': False, 'critical_open': 0}

        fingerprint = cls._critical_fingerprint(result)
        if fingerprint == marker:
            return {'notified': False, 'reason': 'unchanged',
                    'critical_open': critical_open}

        try:
            from app.plugins_sdk import notify
            notify.send(
                'setup.incomplete', to='admins',
                data={
                    'count': critical_open,
                    'summary': f'{critical_open} critical setup item(s) need '
                               'attention.',
                    'message': f'{critical_open} critical setup item(s) are '
                               'incomplete. Finish them in Monitoring → Doctor '
                               '(Setup section).',
                })
        except Exception as e:  # noqa: BLE001 — a nag must never break the job
            logger.warning('setup-health nag notify failed: %s', e)

        SettingsService.set(NAG_MARKER_KEY, fingerprint)
        return {'notified': True, 'critical_open': critical_open}

    # ------------------------------------------------------------------ #
    # Doctor integration + job plumbing
    # ------------------------------------------------------------------ #

    @classmethod
    def doctor_checks(cls):
        """The panel setup items as doctor ``setup.*`` checks, for inclusion in
        the doctor report's Setup section (Monitoring → Doctor)."""
        return cls.evaluate(scope='panel')['items']

    @classmethod
    def register_jobs(cls):
        """Register the nag handler with the job registry. Called once at app
        startup (see app/__init__.py)."""
        from app.jobs import registry
        registry.register(NAG_JOB_KIND, cls.run_nag_job, replace=True)


def _register_catalog_events():
    """Register the ``setup.incomplete`` event so the nag renders through the same
    pipeline as any core notification. Safe + idempotent at import time."""
    try:
        from app.notifications import catalog
        catalog.register(
            'setup.incomplete',
            title='Finish setting up ServerKit',
            template='generic',
            severity='warning',
            category='system',
        )
    except Exception as e:  # noqa: BLE001
        logger.debug('could not register setup.incomplete catalog event: %s', e)


_register_catalog_events()
