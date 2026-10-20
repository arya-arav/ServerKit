"""Everything about one service in one read: how it was installed (template,
version, the options chosen), what is running (containers, images, ports, the
PHP version inside), where it lives on disk and which domains serve it.

Fork: arya-arav/ServerKit. The service page had no place showing, for example,
which PHP version a template install picked: it lives only in the app's
``.serverkit-template.json`` and ``.env``. Read-only, best-effort: a probe
that fails leaves its field empty instead of failing the whole response.
"""
import json
import os
import re
from datetime import datetime

# Option names whose values are secrets: shown masked, never sent to the browser.
SECRET_NAME = re.compile(r'PASS|SECRET|TOKEN|KEY|SALT|PRIVATE|CREDENTIAL', re.IGNORECASE)
MASK = '••••••'
# Only containers that look like PHP get a `php -r` probe, so the page stays fast.
PHP_HINT = re.compile(r'php|wordpress|laravel|drupal|joomla|nextcloud', re.IGNORECASE)
MAX_CONTAINERS = 12
PROBE_TIMEOUT = 5


class AppDetailsService:

    @staticmethod
    def find_app(app_id):
        """The live (not soft-deleted) Application, or None."""
        from app.models.application import Application
        return Application.query_active().filter_by(id=app_id).first()

    @classmethod
    def collect(cls, app):
        return {
            'service': cls._service(app),
            'template': cls._template(app),
            'containers': cls._containers(app),
            'domains': cls._domains(app),
        }

    @staticmethod
    def _service(app):
        return {
            'name': app.name,
            'type': app.app_type,
            'source': getattr(app, 'source', None),
            'managed_by': getattr(app, 'managed_by', None),
            'root_path': app.root_path,
            'compose_file': getattr(app, 'compose_file', None),
            'port': app.port,
            'status': app.status,
            'created_at': app.created_at.isoformat() if app.created_at else None,
        }

    @staticmethod
    def mask_variables(variables):
        """Options as a sorted list, with secret-looking values masked."""
        out = []
        for name in sorted(variables or {}):
            value = variables[name]
            secret = bool(SECRET_NAME.search(name))
            out.append({'name': name, 'value': MASK if secret else str(value), 'secret': secret})
        return out

    @classmethod
    def _template(cls, app):
        if not app.root_path:
            return None
        path = os.path.join(app.root_path, '.serverkit-template.json')
        try:
            with open(path) as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            return None
        return {
            'id': info.get('template_id'),
            'name': info.get('template_name'),
            'version': info.get('template_version'),
            'installed_at': info.get('installed_at'),
            'variables': cls.mask_variables(info.get('variables')),
        }

    @classmethod
    def _containers(cls, app):
        try:
            from app.services.docker_service import DockerService
            listed = DockerService.get_all_app_containers(app) or []
        except Exception:  # noqa: BLE001
            return []
        result = []
        for c in listed[:MAX_CONTAINERS]:
            ref = c.get('id') or c.get('name')
            item = {
                'name': c.get('name'),
                'service': c.get('service'),
                'state': c.get('state'),
                'image': None,
                'ports': [],
                'started_at': None,
                'php_version': None,
            }
            if ref:
                cls._inspect(DockerService, ref, item)
            result.append(item)
        return result

    @staticmethod
    def parse_ports(port_map):
        """``NetworkSettings.Ports`` → ["127.0.0.1:8071 → 80/tcp", "9000/tcp"]."""
        out = []
        for container_port, bindings in sorted((port_map or {}).items()):
            if not bindings:
                out.append(container_port)
                continue
            seen = set()
            for b in bindings:
                host = f"{b.get('HostIp') or '0.0.0.0'}:{b.get('HostPort')}"
                if host not in seen:
                    seen.add(host)
                    out.append(f'{host} → {container_port}')
        return out

    @classmethod
    def _inspect(cls, docker, ref, item):
        inspected = docker.run(['inspect', ref, '--format', '{{json .}}'], timeout=PROBE_TIMEOUT)
        if not inspected.get('success'):
            return
        try:
            data = json.loads(inspected.get('output') or '{}')
        except ValueError:
            return
        item['image'] = (data.get('Config') or {}).get('Image')
        state = data.get('State') or {}
        item['state'] = state.get('Status') or item['state']
        item['started_at'] = state.get('StartedAt')
        item['ports'] = cls.parse_ports((data.get('NetworkSettings') or {}).get('Ports'))
        looks_php = PHP_HINT.search(f"{item['image'] or ''} {item['name'] or ''}")
        if state.get('Running') and looks_php:
            probe = docker.run(['exec', ref, 'php', '-r', 'echo PHP_VERSION;'], timeout=PROBE_TIMEOUT)
            version = (probe.get('output') or '').strip()
            if probe.get('success') and re.fullmatch(r'\d+\.\d+\.\d+\S*', version):
                item['php_version'] = version

    @classmethod
    def _domains(cls, app):
        try:
            from app.models.domain import Domain
            rows = (Domain.query_active().filter_by(application_id=app.id)
                    .order_by(Domain.is_primary.desc(), Domain.id.asc()).all())
        except Exception:  # noqa: BLE001
            return []
        return [{
            'name': d.name,
            'primary': bool(d.is_primary),
            'ssl': bool(d.ssl_enabled),
            'ssl_expires_at': cls._cert_expiry(d.ssl_certificate_path) if d.ssl_enabled else None,
        } for d in rows]

    @staticmethod
    def _cert_expiry(cert_path):
        if not cert_path:
            return None
        try:
            from cryptography import x509
            with open(cert_path, 'rb') as fh:
                cert = x509.load_pem_x509_certificate(fh.read())
            expires = cert.not_valid_after_utc.replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            return None
        return expires.isoformat() if isinstance(expires, datetime) else None
