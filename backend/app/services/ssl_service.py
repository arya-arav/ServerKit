import os
import shutil
import subprocess
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from app.utils.system import (PackageManager, ServiceControl, is_command_available,
                             run_privileged, write_privileged_file)


# Candidate locations for the certbot binary when it isn't on $PATH. Snap is
# certbot's recommended install method and lands in /snap/bin (often missing
# from a service's $PATH); apt/dnf land in /usr/bin; pip installs land in
# /usr/local/bin.
_CERTBOT_CANDIDATES = ('/usr/bin/certbot', '/snap/bin/certbot', '/usr/local/bin/certbot')

_certbot_bin_cache: Optional[str] = None


def reset_certbot_resolution_cache() -> None:
    """Drop the cached certbot path (tests, and right after installing certbot)."""
    global _certbot_bin_cache
    _certbot_bin_cache = None


def resolve_certbot_bin() -> Optional[str]:
    """Resolve the certbot binary path.

    Resolution order: the ``CERTBOT_BIN`` environment override (honored
    unconditionally), then ``shutil.which('certbot')``, then the well-known
    candidate paths including the snap location.  Returns ``None`` when
    certbot cannot be found.

    The result is cached — certbot's install location does not change
    mid-process.  Negative results are intentionally not cached so a later
    install is picked up without a restart.
    """
    global _certbot_bin_cache
    if _certbot_bin_cache is not None:
        return _certbot_bin_cache

    override = os.environ.get('CERTBOT_BIN')
    if override:
        _certbot_bin_cache = override
        return _certbot_bin_cache

    found = shutil.which('certbot')
    if not found:
        for candidate in _CERTBOT_CANDIDATES:
            if os.path.isfile(candidate) and \
                    (os.name == 'nt' or os.access(candidate, os.X_OK)):
                found = candidate
                break

    _certbot_bin_cache = found
    return found


# Panel-wide ACME contact, stored in system settings.
ACME_EMAIL_SETTING = 'ssl.acme_email'


def get_acme_contact(provided: Optional[str] = None) -> Optional[str]:
    """The contact address to register with Let's Encrypt.

    An address supplied with the request wins; otherwise the panel-wide
    contact stored in settings. Returns ``None`` when neither exists, leaving
    it to the caller to decide whether that is fatal — the domain SSL modal
    treats it as a hard requirement, while the automatic issuance paths would
    rather fall back than refuse.
    """
    provided = (provided or '').strip()
    if provided:
        return provided
    try:
        from app.services.settings_service import SettingsService
        stored = (SettingsService.get(ACME_EMAIL_SETTING) or '').strip()
    except Exception:  # noqa: BLE001 — never let settings break issuance
        return None
    return stored or None


def remember_acme_contact(email: Optional[str], user_id=None) -> None:
    """Store *email* as the panel-wide ACME contact.

    Called whenever a certificate request carries an explicit address, so the
    next request anywhere in the panel can prefill instead of asking again.
    Best-effort by design: failing to remember an address must never turn a
    successful certificate issuance into an error.
    """
    email = (email or '').strip()
    if not email:
        return
    try:
        from app.services.settings_service import SettingsService
        if (SettingsService.get(ACME_EMAIL_SETTING) or '').strip() == email:
            return
        SettingsService.set(ACME_EMAIL_SETTING, email, user_id=user_id)
    except Exception:  # noqa: BLE001
        pass


class SSLService:
    """Service for SSL certificate management with Let's Encrypt."""

    # Fallback used when resolution finds nothing, preserving the historical
    # default; commands built with it fail cleanly with FileNotFoundError.
    CERTBOT_FALLBACK = '/usr/bin/certbot'
    CERTS_DIR = '/etc/letsencrypt/live'
    RENEWAL_DIR = '/etc/letsencrypt/renewal'
    # Custom-cert install location (e.g. issued Cloudflare Origin CA certs, written
    # by AdvancedSSLService.upload_custom_cert). certbot doesn't know about these.
    SERVERKIT_CERTS_DIR = '/etc/ssl/serverkit'

    @classmethod
    def certbot_bin(cls) -> str:
        """Resolved certbot binary path (never ``None`` — see CERTBOT_FALLBACK)."""
        return resolve_certbot_bin() or cls.CERTBOT_FALLBACK

    @classmethod
    def is_certbot_installed(cls) -> bool:
        """Check if certbot is installed."""
        return is_command_available('certbot')

    @classmethod
    def install_certbot(cls) -> Dict:
        """Install certbot if not present."""
        if not PackageManager.is_available():
            return {'success': False, 'error': 'No supported package manager found'}

        try:
            result = PackageManager.install(
                ['certbot', 'python3-certbot-nginx'],
                timeout=300,
            )
            if result.returncode != 0:
                return {'success': False, 'error': result.stderr}

            reset_certbot_resolution_cache()
            return {'success': True, 'message': 'Certbot installed successfully'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def _has_nginx_plugin(cls, certbot: str) -> bool:
        """Check certbot's plugin list for an enabled nginx plugin.

        Parses ``certbot plugins`` output (authoritative for both apt and snap
        installs; enabled plugins are listed as ``* nginx``, disabled as
        ``- nginx``).  Falls back to the distro package check when the probe
        itself fails, so a broken ``plugins`` subcommand doesn't block issuance
        on a host where the plugin package is clearly present.
        """
        try:
            result = run_privileged([certbot, 'plugins'], timeout=60)
            if result.returncode == 0:
                return any(
                    line.strip().startswith('* nginx')
                    for line in (result.stdout or '').split('\n')
                )
        except Exception:
            pass
        return PackageManager.is_installed('python3-certbot-nginx')

    @classmethod
    def _preflight_nginx_plugin(cls, certbot: str) -> Optional[Dict]:
        """Verify certbot's nginx plugin before invoking ``certbot --nginx``.

        Without this, a host that has certbot but not the plugin (or a certbot
        that can't see the nginx binary) fails deep inside certbot with a
        confusing "nginx plugin is not working / binary doesn't exist" message
        that we would pass through raw.  Returns an actionable error dict, or
        ``None`` when everything is in place.
        """
        if not is_command_available('nginx'):
            return {
                'success': False,
                'error': 'nginx binary not found — install nginx before '
                         'requesting a certificate with the nginx plugin',
            }
        if not cls._has_nginx_plugin(certbot):
            return {
                'success': False,
                'error': 'certbot nginx plugin not installed — install '
                         'python3-certbot-nginx (apt) or use the certbot snap, '
                         'which bundles the nginx plugin',
            }
        return None

    @classmethod
    def obtain_certificate(cls, domains: List[str], email: str,
                           webroot_path: str = None, use_nginx: bool = True,
                           cert_name: str = None) -> Dict:
        """Obtain a new SSL certificate from Let's Encrypt.

        *cert_name* pins certbot's lineage name, so one certificate covering
        several names (a service's primary domain plus its aliases) keeps a
        predictable path and is expanded in place when names are added.
        """
        if not cls.is_certbot_installed():
            install_result = cls.install_certbot()
            if not install_result['success']:
                return install_result

        try:
            # Build certbot command
            cmd = [cls.certbot_bin(), 'certonly']

            if use_nginx:
                preflight_error = cls._preflight_nginx_plugin(cmd[0])
                if preflight_error is not None:
                    return preflight_error
                cmd.append('--nginx')
            elif webroot_path:
                cmd.extend(['--webroot', '-w', webroot_path])
            else:
                return {'success': False, 'error': 'Either use_nginx or webroot_path is required'}

            # Add domains
            for domain in domains:
                cmd.extend(['-d', domain])

            # Add email and agree to TOS
            cmd.extend([
                '--email', email,
                '--agree-tos',
                '--non-interactive',
                '--expand'
            ])
            if cert_name:
                cmd.extend(['--cert-name', cert_name])

            result = run_privileged(cmd, timeout=300)

            if result.returncode == 0:
                primary_domain = domains[0]
                lineage = cert_name or primary_domain
                cert_path = f'{cls.CERTS_DIR}/{lineage}/fullchain.pem'
                key_path = f'{cls.CERTS_DIR}/{lineage}/privkey.pem'

                response = {
                    'success': True,
                    'message': 'Certificate obtained successfully',
                    'certificate_path': cert_path,
                    'private_key_path': key_path,
                    'domains': domains
                }

                # Best-effort: authorize Let's Encrypt via a CAA record on whichever
                # connected DNS provider manages the domain. This satisfies CAA
                # security scanners and pins issuance to our CA. Never let a CAA
                # hiccup fail an otherwise-successful certificate.
                try:
                    from app.services.dns_provider_service import DNSProviderService
                    response['caa'] = DNSProviderService.ensure_caa_record(primary_domain)
                except Exception as e:
                    response['caa'] = {'created': False, 'reason': 'error', 'error': str(e)}

                return response
            else:
                return {'success': False, 'error': result.stderr}

        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'Certificate request timed out'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def renew_certificate(cls, domain: str = None) -> Dict:
        """Renew SSL certificate(s)."""
        try:
            cmd = [cls.certbot_bin(), 'renew', '--non-interactive']

            if domain:
                cmd.extend(['--cert-name', domain])

            result = run_privileged(cmd, timeout=300)

            return {
                'success': result.returncode == 0,
                'message': result.stdout if result.returncode == 0 else result.stderr
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def revoke_certificate(cls, domain: str) -> Dict:
        """Revoke an SSL certificate."""
        cert_path = f'{cls.CERTS_DIR}/{domain}/fullchain.pem'

        try:
            cmd = [
                cls.certbot_bin(), 'revoke',
                '--cert-path', cert_path,
                '--non-interactive'
            ]

            result = run_privileged(cmd, timeout=120)

            if result.returncode == 0:
                # Also delete the certificate
                delete_cmd = [
                    cls.certbot_bin(), 'delete',
                    '--cert-name', domain,
                    '--non-interactive'
                ]
                run_privileged(delete_cmd)

                return {'success': True, 'message': f'Certificate for {domain} revoked and deleted'}

            return {'success': False, 'error': result.stderr}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def list_certificates(cls) -> List[Dict]:
        """List all installed certificates.

        Inventory probe failures are dropped here for backward compatibility;
        callers that need to distinguish "no certificates" from "inventory
        unavailable" should use :meth:`list_certificates_report`.
        """
        certificates, _errors = cls.list_certificates_report()
        return certificates

    @classmethod
    def list_certificates_report(cls) -> Tuple[List[Dict], List[str]]:
        """List all installed certificates, plus any inventory probe failures.

        Returns ``(certificates, errors)``. certbot missing/erroring (or an
        unreadable custom cert) used to vanish into an empty list, so the
        status endpoint reported "0 certificates, nothing expiring" with
        HTTP 200 when the inventory probe itself had failed.
        """
        certificates: List[Dict] = []
        errors: List[str] = []

        try:
            result = run_privileged([cls.certbot_bin(), 'certificates'], timeout=60)

            # Parse certbot output (skip on a non-zero exit, but still surface
            # the custom-installed certs below — and record the failure).
            if result.returncode == 0:
                current_cert = None
                for line in result.stdout.split('\n'):
                    line = line.strip()

                    if line.startswith('Certificate Name:'):
                        if current_cert:
                            certificates.append(current_cert)
                        current_cert = {'name': line.split(':', 1)[1].strip()}

                    elif current_cert:
                        if line.startswith('Domains:'):
                            current_cert['domains'] = line.split(':', 1)[1].strip().split()
                        elif line.startswith('Expiry Date:'):
                            expiry_str = line.split(':', 1)[1].strip()
                            # Parse expiry date
                            try:
                                # Format: 2024-03-15 12:00:00+00:00
                                expiry_part = expiry_str.split(' (')[0]
                                current_cert['expiry'] = expiry_part
                                current_cert['expiry_valid'] = 'VALID' in expiry_str
                            except Exception:
                                current_cert['expiry'] = expiry_str
                        elif line.startswith('Certificate Path:'):
                            current_cert['cert_path'] = line.split(':', 1)[1].strip()
                        elif line.startswith('Private Key Path:'):
                            current_cert['key_path'] = line.split(':', 1)[1].strip()

                if current_cert:
                    certificates.append(current_cert)
            else:
                detail = (result.stderr or result.stdout or '').strip()
                errors.append('certbot inventory failed'
                              + (f': {detail[:200]}' if detail else ''))

        except Exception as e:
            errors.append(f'certbot inventory failed: {e}')

        # Also surface custom-installed certs (e.g. Cloudflare Origin CA), which
        # certbot doesn't track. Parse the x509 issuer to badge them.
        certificates.extend(cls._list_serverkit_certificates(errors))

        return certificates, errors

    @classmethod
    def _list_serverkit_certificates(cls, errors: Optional[List[str]] = None) -> List[Dict]:
        """Walk the custom-cert install dir (``/etc/ssl/serverkit/<domain>/cert.pem``)
        and describe each cert, badging Cloudflare Origin CA certs as proxy-only."""
        import glob
        out: List[Dict] = []
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
        except Exception:
            return out

        for cert_path in sorted(glob.glob(f'{cls.SERVERKIT_CERTS_DIR}/*/cert.pem')):
            try:
                domain = os.path.basename(os.path.dirname(cert_path))
                with open(cert_path, 'rb') as fh:
                    cert = x509.load_pem_x509_certificate(fh.read(), default_backend())
                issuer = cert.issuer.rfc4514_string()
                is_origin_ca = 'origin' in issuer.lower() and 'cloudflare' in issuer.lower()
                try:
                    expiry = cert.not_valid_after_utc
                except AttributeError:  # cryptography < 42
                    expiry = cert.not_valid_after
                out.append({
                    'name': domain,
                    'domains': [domain],
                    'cert_path': cert_path,
                    'key_path': os.path.join(os.path.dirname(cert_path), 'key.pem'),
                    'issuer': issuer,
                    'source': 'custom',
                    'badge': 'Origin CA (proxy-only)' if is_origin_ca else None,
                    'expiry': expiry.strftime('%Y-%m-%d %H:%M:%S%z') if expiry else None,
                })
            except Exception as e:
                if errors is not None:
                    errors.append(f'unreadable certificate {cert_path}: {e}')
                continue
        return out

    @classmethod
    def get_certificate_info(cls, domain: str) -> Optional[Dict]:
        """Get detailed information about a specific certificate."""
        cert_path = f'{cls.CERTS_DIR}/{domain}/fullchain.pem'

        try:
            # Use openssl to get certificate details
            result = run_privileged(
                ['openssl', 'x509', '-in', cert_path, '-noout',
                 '-subject', '-issuer', '-dates', '-serial'],
            )

            if result.returncode != 0:
                return None

            info = {'domain': domain, 'cert_path': cert_path}

            for line in result.stdout.split('\n'):
                if line.startswith('subject='):
                    info['subject'] = line.split('=', 1)[1].strip()
                elif line.startswith('issuer='):
                    info['issuer'] = line.split('=', 1)[1].strip()
                elif line.startswith('notBefore='):
                    info['valid_from'] = line.split('=', 1)[1].strip()
                elif line.startswith('notAfter='):
                    info['valid_until'] = line.split('=', 1)[1].strip()
                elif line.startswith('serial='):
                    info['serial'] = line.split('=', 1)[1].strip()

            return info

        except Exception:
            return None

    @classmethod
    def check_expiry(cls, domain: str) -> Dict:
        """Check if a certificate is expiring soon."""
        try:
            cert_path = f'{cls.CERTS_DIR}/{domain}/fullchain.pem'

            # Check expiry with openssl
            result = run_privileged(
                ['openssl', 'x509', '-in', cert_path, '-checkend', '2592000'],
            )

            expiring_soon = result.returncode != 0

            # Get actual expiry date
            date_result = run_privileged(
                ['openssl', 'x509', '-in', cert_path, '-noout', '-enddate'],
            )

            expiry_date = None
            if date_result.returncode == 0:
                expiry_date = date_result.stdout.replace('notAfter=', '').strip()

            # `-checkend` exits non-zero BOTH when the cert expires within the
            # window AND when it cannot be read at all (missing/corrupt PEM,
            # permission denied). With no parsed expiry date the failure is
            # "unreadable", not "expiring" — report unknown instead of a
            # phantom "needs renewal".
            if expiring_soon and not expiry_date:
                return {
                    'domain': domain,
                    'error': (result.stderr or 'certificate unreadable').strip(),
                }

            return {
                'domain': domain,
                'expiring_soon': expiring_soon,
                'expiry_date': expiry_date,
                'needs_renewal': expiring_soon
            }

        except Exception as e:
            return {'domain': domain, 'error': str(e)}

    @classmethod
    def setup_auto_renewal(cls) -> Dict:
        """Set up automatic certificate renewal via cron/systemd timer."""
        try:
            # Check if systemd timer exists
            if ServiceControl.is_enabled('certbot.timer'):
                return {'success': True, 'message': 'Auto-renewal already configured via systemd'}

            # Enable systemd timer
            enable_result = run_privileged(
                ['systemctl', 'enable', '--now', 'certbot.timer'],
            )

            if enable_result.returncode == 0:
                return {'success': True, 'message': 'Auto-renewal enabled via systemd timer'}

            # Fall back to cron
            cron_job = '0 0,12 * * * root certbot renew --quiet --post-hook "systemctl reload nginx"'
            cron_file = '/etc/cron.d/certbot-renewal'

            write_privileged_file(cron_file, cron_job + '\n')

            return {'success': True, 'message': 'Auto-renewal configured via cron'}

        except Exception as e:
            return {'success': False, 'error': str(e)}
