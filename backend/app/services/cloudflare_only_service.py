"""Cloudflare-only web ports. Fork: arya-arav/ServerKit.

When every site on the server is proxied through Cloudflare, ports 80 and 443
only ever need to accept connections from Cloudflare's edge. Anything else
reaching them is someone who found the origin IP and is going around
Cloudflare's WAF, rate limits and Access policies.

The switch manages up to three places, each only when the host has it:

* **Host iptables** — Oracle Cloud images ship ``/etc/iptables/rules.v4`` whose
  INPUT chain ends in ``-j REJECT`` *before* ufw's chains, so for IPv4 ufw never
  sees a packet. Rules go into INPUT ahead of that reject, live and in
  ``rules.v4`` (edited as text, never ``netfilter-persistent save``, which would
  freeze ufw's, Docker's and CrowdSec's chains into the file).
* **ufw** — one rule per range, IPv4 and IPv6, for 80 and 443.
* **nginx real IP** — a conf.d file trusting ``CF-Connecting-IP`` from the same
  ranges, so logs, rate limits and fail2ban see visitors instead of Cloudflare.

Cloudflare's ranges are fetched from cloudflare.com/ips-v4 and /ips-v6 and
re-checked weekly; the bundled list in :mod:`app.utils.cloudflare_ips` is the
fallback. Port 22 is never touched.
"""

import ipaddress
import json
import logging
import os
import re
import shlex
import tempfile
import urllib.request
from datetime import datetime, timezone

from app import paths
from app.utils.cloudflare_ips import BUNDLED_V4, BUNDLED_V6
from app.utils.system import (
    PROBE_TIMEOUT,
    is_command_available,
    run_privileged,
    write_privileged_file,
)

logger = logging.getLogger(__name__)

SETTING_KEY = 'security_cloudflare_only'
JOB_KIND = 'security.cloudflare_only.refresh'
SCHEDULE_NAME = 'cloudflare-ips-refresh'

#: Comment on every rule this service owns.
TAG = 'serverkit-cloudflare'
#: Comment on the open-to-everyone rule written when the switch is turned off.
OPEN_TAG = 'serverkit-web'
#: Comments of hand-made Cloudflare rules, adopted (replaced) on enable.
LEGACY_TAGS = ('cloudflare-web',)

WEB_PORTS = (80, 443)
RANGE_URLS = {
    'v4': 'https://www.cloudflare.com/ips-v4',
    'v6': 'https://www.cloudflare.com/ips-v6',
}

RANGES_FILE = os.path.join(paths.SERVERKIT_CONFIG_DIR, 'cloudflare-ips.json')
RULES_V4 = '/etc/iptables/rules.v4'
NGINX_CONF_DIR = '/etc/nginx/conf.d'
REALIP_CONF = os.path.join(NGINX_CONF_DIR, 'serverkit-cloudflare-realip.conf')

# A fetched list is only trusted when it looks like Cloudflare's: enough
# networks, and none so wide it would open the ports to a large slice of the
# internet (a captive portal or a mangled response must never become a rule).
_MIN_V4_NETWORKS = 5
_MIN_PREFIX = {4: 8, 6: 16}

_CATCH_ALL_RE = re.compile(r'^-A INPUT -j (REJECT|DROP)\b')
_UFW_JUMP_RE = re.compile(r'^-A INPUT -j ufw6?-')


def _web_rule(source=None, tag=TAG):
    """iptables argv (after the chain) for an ACCEPT on 80,443."""
    spec = ['-s', source] if source else []
    return spec + ['-p', 'tcp', '-m', 'multiport', '--dports',
                   ','.join(str(p) for p in WEB_PORTS),
                   '-m', 'comment', '--comment', tag, '-j', 'ACCEPT']


def _rule_tag(tokens):
    if '--comment' in tokens:
        i = tokens.index('--comment')
        if i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _rule_ports(tokens):
    ports = set()
    for flag in ('--dport', '--dports'):
        if flag in tokens:
            i = tokens.index(flag)
            if i + 1 < len(tokens):
                for part in tokens[i + 1].split(','):
                    lo, _, hi = part.partition(':')
                    try:
                        lo_n = int(lo)
                        hi_n = int(hi) if hi else lo_n
                    except ValueError:
                        continue
                    ports.update(p for p in WEB_PORTS if lo_n <= p <= hi_n)
    return ports


def _is_open_web_rule(tokens):
    """An ACCEPT for 80 or 443 from anywhere that this service did not tag."""
    return ('-j' in tokens and tokens[tokens.index('-j') + 1] == 'ACCEPT'
            and '-s' not in tokens and bool(_rule_ports(tokens))
            and _rule_tag(tokens) not in (TAG, *LEGACY_TAGS))


class CloudflareOnlyService:
    """Restrict ports 80/443 to Cloudflare's edge, and keep it current."""

    # ------------------------------------------------------------------ #
    # Setting
    # ------------------------------------------------------------------ #

    @staticmethod
    def setting():
        """True / False once the operator has used the switch, else None.

        None means the panel has never managed this host's web ports, so
        nothing (boot, the weekly job) may touch them.
        """
        from .settings_service import SettingsService
        value = SettingsService.get(SETTING_KEY, None)
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip().lower() not in ('false', '0', 'no', 'off', '')
        return bool(value)

    # ------------------------------------------------------------------ #
    # Cloudflare ranges
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_ranges(text, version):
        nets = []
        for line in (text or '').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            net = ipaddress.ip_network(line, strict=True)
            if net.version != version:
                raise ValueError(f'{line} is not an IPv{version} network')
            if net.prefixlen < _MIN_PREFIX[version]:
                raise ValueError(f'{line} is wider than any Cloudflare range')
            nets.append(str(net))
        return nets

    @classmethod
    def fetch_ranges(cls):
        """Download the current lists; raises on anything that looks wrong."""
        ranges = {}
        for key, url in RANGE_URLS.items():
            req = urllib.request.Request(url, headers={'User-Agent': 'ServerKit'})
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URL
                text = resp.read(64 * 1024).decode('utf-8', 'replace')
            ranges[key] = cls._parse_ranges(text, 4 if key == 'v4' else 6)
        if len(ranges['v4']) < _MIN_V4_NETWORKS:
            raise ValueError('Cloudflare returned too few IPv4 ranges')
        return ranges

    @staticmethod
    def ranges():
        """The ranges in use: the last good download, else the bundled list."""
        try:
            with open(RANGES_FILE) as f:
                data = json.load(f)
            if data.get('v4'):
                return {'v4': list(data['v4']), 'v6': list(data.get('v6') or []),
                        'fetched_at': data.get('fetched_at'), 'source': 'cloudflare.com'}
        except (OSError, ValueError):
            pass
        return {'v4': list(BUNDLED_V4), 'v6': list(BUNDLED_V6),
                'fetched_at': None, 'source': 'bundled'}

    @classmethod
    def refresh_ranges(cls):
        """Fetch and store the lists. Returns ``{'success', 'changed', ...}``."""
        before = cls.ranges()
        try:
            fresh = cls.fetch_ranges()
        except Exception as exc:  # noqa: BLE001 - reported, the old list stays
            logger.warning('cloudflare range refresh failed: %s', exc)
            return {'success': False, 'changed': False,
                    'error': f'Could not download Cloudflare IP ranges: {exc}'}
        record = {**fresh, 'fetched_at': datetime.now(timezone.utc).isoformat()}
        result = write_privileged_file(RANGES_FILE, json.dumps(record, indent=2) + '\n',
                                       mode='644')
        if not result['success']:
            return {'success': False, 'changed': False, 'error': result['error']}
        changed = (sorted(fresh['v4']) != sorted(before['v4'])
                   or sorted(fresh['v6']) != sorted(before['v6']))
        return {'success': True, 'changed': changed}

    # ------------------------------------------------------------------ #
    # Domains
    # ------------------------------------------------------------------ #

    @staticmethod
    def _served_hosts():
        """Every public hostname this server answers for (sites + panel)."""
        from .doctor_service import DoctorService, _is_public_site_host
        hosts = list(DoctorService._site_domains() or [])
        try:
            with open(os.path.join(paths.SERVERKIT_CONFIG_DIR, 'panel-domain')) as f:
                panel = f.read().strip()
            if panel:
                hosts.append(panel)
        except OSError:
            pass
        seen = []
        for h in hosts:
            h = h.strip().lower().rstrip('.')
            if _is_public_site_host(h) and h not in seen:
                seen.append(h)
        return seen

    @classmethod
    def domain_report(cls):
        """Which served hostnames reach the server through Cloudflare.

        Only ``not_proxied`` blocks the switch: those sites would stop loading.
        A name that does not resolve reaches nobody either way.
        """
        import socket
        from .doctor_service import _EAI_UNRESOLVED, _all_cloudflare, _resolve_host_ips
        report = []
        for host in cls._served_hosts():
            try:
                ips = _resolve_host_ips(host)
            except socket.gaierror as exc:
                state = 'unresolved' if exc.errno in _EAI_UNRESOLVED else 'unknown'
                report.append({'host': host, 'state': state, 'ips': []})
                continue
            except Exception:  # noqa: BLE001
                report.append({'host': host, 'state': 'unknown', 'ips': []})
                continue
            state = 'proxied' if _all_cloudflare(ips) else 'not_proxied'
            report.append({'host': host, 'state': state, 'ips': ips})
        return report

    # ------------------------------------------------------------------ #
    # Host iptables layer (Oracle Cloud images)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _input_rules():
        """``iptables -S INPUT`` rules as token lists, in order (no -P line)."""
        result = run_privileged(['iptables', '-S', 'INPUT'], timeout=PROBE_TIMEOUT)
        if result.returncode != 0:
            return None
        return [shlex.split(line) for line in (result.stdout or '').splitlines()
                if line.startswith('-A INPUT')]

    @classmethod
    def _host_layer_applies(cls, rules=None):
        """True when INPUT rejects everything before ufw's chains run."""
        if os.name == 'nt' or not is_command_available('iptables'):
            return False
        if not os.path.exists(RULES_V4):
            return False
        rules = cls._input_rules() if rules is None else rules
        if not rules:
            return False
        for tokens in rules:
            line = ' '.join(tokens)
            if _UFW_JUMP_RE.match(line):
                return False
            if _CATCH_ALL_RE.match(line):
                return True
        return False

    @staticmethod
    def _catch_all_position(rules):
        """1-based position of the first catch-all reject in INPUT."""
        for i, tokens in enumerate(rules, start=1):
            if _CATCH_ALL_RE.match(' '.join(tokens)):
                return i
        return None

    @classmethod
    def _host_layer_state(cls, rules):
        managed = [t for t in rules if _rule_tag(t) == TAG]
        legacy = [t for t in rules if _rule_tag(t) in LEGACY_TAGS]
        stop = cls._catch_all_position(rules) or len(rules) + 1
        open_rules = [t for t in rules[:stop - 1] if _is_open_web_rule(t)]
        sources = sorted({t[t.index('-s') + 1] for t in managed + legacy if '-s' in t})
        return {'managed': len(managed), 'legacy': len(legacy),
                'open_to_all': len(open_rules), 'sources': sources}

    @classmethod
    def _apply_host_layer(cls, enabled, v4):
        """Converge the live INPUT chain, then rewrite rules.v4 to match."""
        rules = cls._input_rules()
        if rules is None:
            return {'success': False, 'error': 'Could not read the iptables INPUT chain'}

        def delete(tokens):
            res = run_privileged(['iptables', '-D'] + tokens[1:])
            if res.returncode != 0:
                raise RuntimeError((res.stderr or 'iptables -D failed').strip())

        def insert(spec):
            current = cls._input_rules() or []
            pos = cls._catch_all_position(current) or len(current) + 1
            res = run_privileged(['iptables', '-I', 'INPUT', str(pos)] + spec)
            if res.returncode != 0:
                raise RuntimeError((res.stderr or 'iptables -I failed').strip())

        try:
            if enabled:
                have = {t[t.index('-s') + 1] for t in rules
                        if _rule_tag(t) == TAG and '-s' in t}
                # Add first, remove after: the ports are never closed to
                # Cloudflare while the switch is moving.
                for cidr in v4:
                    if cidr not in have:
                        insert(_web_rule(cidr))
                rules = cls._input_rules() or []
                stop = cls._catch_all_position(rules) or len(rules) + 1
                for i, tokens in enumerate(rules, start=1):
                    tag = _rule_tag(tokens)
                    stale = tag == TAG and '-s' in tokens and tokens[tokens.index('-s') + 1] not in v4
                    if (tag in LEGACY_TAGS or tag == OPEN_TAG or stale
                            or (i < stop and _is_open_web_rule(tokens))):
                        delete(tokens)
            else:
                if not any(_rule_tag(t) == OPEN_TAG for t in rules):
                    insert(_web_rule(tag=OPEN_TAG))
                for tokens in cls._input_rules() or []:
                    if _rule_tag(tokens) in (TAG, *LEGACY_TAGS):
                        delete(tokens)
        except RuntimeError as exc:
            return {'success': False, 'error': f'iptables: {exc}'}
        return cls._persist_host_layer(enabled, v4)

    @classmethod
    def _rewrite_rules_v4(cls, text, enabled, v4):
        """``rules.v4`` with this service's INPUT lines replaced."""
        out, wanted = [], []
        if enabled:
            wanted = ['-A INPUT ' + ' '.join(_web_rule(c)) for c in v4]
        else:
            wanted = ['-A INPUT ' + ' '.join(_web_rule(tag=OPEN_TAG))]
        in_filter = inserted = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('*'):
                in_filter = stripped == '*filter'
            if in_filter and stripped.startswith('-A INPUT'):
                try:
                    tokens = shlex.split(stripped)
                except ValueError:
                    tokens = []
                tag = _rule_tag(tokens)
                if tag in (TAG, OPEN_TAG, *LEGACY_TAGS):
                    continue
                if enabled and not inserted and _is_open_web_rule(tokens):
                    continue
                if not inserted and _CATCH_ALL_RE.match(stripped):
                    out.extend(wanted)
                    inserted = True
            if in_filter and stripped == 'COMMIT' and not inserted:
                out.extend(wanted)
                inserted = True
            out.append(line)
        return '\n'.join(out) + '\n'

    @classmethod
    def _persist_host_layer(cls, enabled, v4):
        try:
            res = run_privileged(['cat', RULES_V4], timeout=PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return {'success': False, 'error': f'could not read {RULES_V4}: {exc}'}
        if res.returncode != 0:
            return {'success': False, 'error': f'could not read {RULES_V4}'}
        new_text = cls._rewrite_rules_v4(res.stdout or '', enabled, v4)
        if new_text == (res.stdout or ''):
            return {'success': True}
        # Never write a file iptables-restore would refuse at boot.
        fd, tmp = tempfile.mkstemp(prefix='serverkit-rules-v4-')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(new_text)
            check = run_privileged(['iptables-restore', '--test', tmp])
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if check.returncode != 0:
            return {'success': False,
                    'error': f'refused to save {RULES_V4}: '
                             f'{(check.stderr or "iptables-restore --test failed").strip()}'}
        written = write_privileged_file(RULES_V4, new_text)
        if not written['success']:
            return {'success': False, 'error': written['error']}
        return {'success': True}

    # ------------------------------------------------------------------ #
    # ufw layer
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ufw_active():
        if os.name == 'nt' or not is_command_available('ufw'):
            return False
        res = run_privileged(['ufw', 'status'], timeout=PROBE_TIMEOUT)
        return res.returncode == 0 and 'Status: active' in (res.stdout or '')

    @staticmethod
    def _ufw_rules():
        """``ufw show added`` lines, e.g. ``ufw allow 80/tcp``."""
        res = run_privileged(['ufw', 'show', 'added'], timeout=PROBE_TIMEOUT)
        if res.returncode != 0:
            return []
        return [line.strip() for line in (res.stdout or '').splitlines()
                if line.strip().startswith('ufw ')]

    @staticmethod
    def _ufw_rule_kind(line):
        """'managed' | 'legacy' | 'open' | None for one ``ufw show added`` line."""
        try:
            tokens = shlex.split(line)
        except ValueError:
            return None
        tag = tokens[tokens.index('comment') + 1] if 'comment' in tokens[:-1] else None
        if tag == TAG:
            return 'managed'
        if tag in LEGACY_TAGS:
            return 'legacy'
        if len(tokens) < 3 or tokens[1] != 'allow' or 'from' in tokens:
            return None
        if tag == OPEN_TAG:
            return 'open'
        # `ufw allow 80/tcp`, `ufw allow 443`, `ufw allow 80,443/tcp`,
        # `ufw allow proto tcp to any port 80`, `ufw allow 'Nginx Full'`.
        target = tokens[2]
        if 'port' in tokens:
            target = tokens[tokens.index('port') + 1]
        if target in ('Nginx Full', 'Nginx HTTP', 'Nginx HTTPS', 'Apache Full', 'WWW Full'):
            return 'open'
        ports = target.split('/')[0].split(',')
        return 'open' if any(p in ('80', '443') for p in ports) else None

    @classmethod
    def _ufw_state(cls):
        kinds = [cls._ufw_rule_kind(line) for line in cls._ufw_rules()]
        return {'managed': kinds.count('managed'), 'legacy': kinds.count('legacy'),
                'open_to_all': kinds.count('open')}

    @classmethod
    def _apply_ufw(cls, enabled, ranges):
        def run(argv):
            res = run_privileged(argv)
            if res.returncode != 0:
                raise RuntimeError((res.stderr or res.stdout or 'ufw failed').strip())

        def delete(line):
            run(['ufw', 'delete'] + shlex.split(line)[1:])

        port_spec = ','.join(str(p) for p in WEB_PORTS)
        try:
            lines = cls._ufw_rules()
            if enabled:
                wanted = list(ranges['v4']) + list(ranges['v6'])
                have = set()
                for line in lines:
                    if cls._ufw_rule_kind(line) == 'managed':
                        tokens = shlex.split(line)
                        if 'from' in tokens:
                            have.add(tokens[tokens.index('from') + 1])
                for cidr in wanted:
                    if cidr not in have:
                        run(['ufw', 'allow', 'proto', 'tcp', 'from', cidr, 'to', 'any',
                             'port', port_spec, 'comment', TAG])
                for line in cls._ufw_rules():
                    kind = cls._ufw_rule_kind(line)
                    tokens = shlex.split(line)
                    source = tokens[tokens.index('from') + 1] if 'from' in tokens else None
                    if (kind in ('legacy', 'open')
                            or (kind == 'managed' and source not in wanted)):
                        delete(line)
            else:
                if not any(cls._ufw_rule_kind(line) == 'open' for line in lines):
                    run(['ufw', 'allow', 'proto', 'tcp', 'to', 'any', 'port', port_spec,
                         'comment', OPEN_TAG])
                for line in cls._ufw_rules():
                    if cls._ufw_rule_kind(line) in ('managed', 'legacy'):
                        delete(line)
        except RuntimeError as exc:
            return {'success': False, 'error': f'ufw: {exc}'}
        return {'success': True}

    # ------------------------------------------------------------------ #
    # nginx real IP
    # ------------------------------------------------------------------ #

    @staticmethod
    def _realip_conf(ranges):
        lines = ['# Managed by ServerKit (Cloudflare-only web traffic). Do not edit;',
                 '# the list is refreshed from cloudflare.com every week.']
        lines += [f'set_real_ip_from {cidr};' for cidr in ranges['v4'] + ranges['v6']]
        lines += ['real_ip_header CF-Connecting-IP;', '']
        return '\n'.join(lines)

    @staticmethod
    def _foreign_realip_file():
        """Another conf.d file already setting real_ip_header (two would clash)."""
        try:
            names = sorted(os.listdir(NGINX_CONF_DIR))
        except OSError:
            return None
        for name in names:
            path = os.path.join(NGINX_CONF_DIR, name)
            if path == REALIP_CONF or not name.endswith('.conf'):
                continue
            try:
                with open(path) as f:
                    if re.search(r'^\s*real_ip_header\b', f.read(), re.M):
                        return path
            except OSError:
                continue
        return None

    @classmethod
    def _apply_realip(cls, ranges):
        if os.name == 'nt' or not os.path.isdir(NGINX_CONF_DIR):
            return {'success': True, 'skipped': 'nginx not installed'}
        foreign = cls._foreign_realip_file()
        if foreign:
            return {'success': True, 'skipped': f'real IP already set in {foreign}'}
        content = cls._realip_conf(ranges)
        try:
            with open(REALIP_CONF) as f:
                previous = f.read()
        except OSError:
            previous = None
        if previous == content:
            return {'success': True}
        written = write_privileged_file(REALIP_CONF, content, mode='644')
        if not written['success']:
            return {'success': False, 'error': written['error']}
        test = run_privileged(['nginx', '-t'])
        if test.returncode != 0:
            if previous is None:
                run_privileged(['rm', '-f', REALIP_CONF])
            else:
                write_privileged_file(REALIP_CONF, previous, mode='644')
            return {'success': False,
                    'error': f'nginx rejected the real IP config: {(test.stderr or "").strip()}'}
        run_privileged(['systemctl', 'reload', 'nginx'])
        return {'success': True}

    # ------------------------------------------------------------------ #
    # Public operations
    # ------------------------------------------------------------------ #

    @classmethod
    def status(cls, include_domains=True):
        ranges = cls.ranges()
        info = {
            'supported': os.name != 'nt',
            'setting': cls.setting(),
            'ranges': {'v4': len(ranges['v4']), 'v6': len(ranges['v6']),
                       'fetched_at': ranges['fetched_at'], 'source': ranges['source']},
            'layers': {},
        }
        if not info['supported']:
            info['mode'] = 'unsupported'
            return info
        try:
            rules = cls._input_rules() if is_command_available('iptables') else None
            if rules is not None and cls._host_layer_applies(rules):
                info['layers']['host_iptables'] = cls._host_layer_state(rules)
            if cls._ufw_active():
                info['layers']['ufw'] = cls._ufw_state()
        except Exception as exc:  # noqa: BLE001 - status must always answer
            logger.warning('cloudflare-only status failed: %s', exc)
            info['error'] = str(exc)
        info['realip'] = os.path.exists(REALIP_CONF) or bool(cls._foreign_realip_file())

        layers = info['layers'].values()
        if not info['layers']:
            info['mode'] = 'unsupported'
        elif all((l['managed'] or l['legacy']) and not l['open_to_all'] for l in layers):
            info['mode'] = 'on'
        elif any(l['managed'] or l['legacy'] for l in layers):
            info['mode'] = 'partial'
        else:
            info['mode'] = 'off'
        if include_domains:
            info['domains'] = cls.domain_report()
        return info

    @classmethod
    def apply(cls, enabled, force=False):
        """Turn the switch on or off now; persists the setting on success."""
        if os.name == 'nt':
            return {'success': False, 'error': 'Not supported on this platform'}
        if enabled and not force:
            blocking = [d for d in cls.domain_report() if d['state'] == 'not_proxied']
            if blocking:
                return {'success': False, 'blocked_by': 'unproxied_domains',
                        'domains': blocking,
                        'error': 'These domains reach the server without Cloudflare '
                                 'and would stop loading: '
                                 + ', '.join(d['host'] for d in blocking)}
        if enabled:
            cls.refresh_ranges()  # best effort; the stored or bundled list stays valid
        result = cls._converge(enabled, cls.ranges())
        if result['success']:
            from .settings_service import SettingsService
            SettingsService.set(SETTING_KEY, bool(enabled))
        return result

    @classmethod
    def _converge(cls, enabled, ranges):
        steps, errors = {}, []
        rules = cls._input_rules() if is_command_available('iptables') else None
        layers = 0
        if rules is not None and cls._host_layer_applies(rules):
            layers += 1
            steps['host_iptables'] = cls._apply_host_layer(enabled, ranges['v4'])
        if cls._ufw_active():
            layers += 1
            steps['ufw'] = cls._apply_ufw(enabled, ranges)
        if not layers:
            return {'success': False,
                    'error': 'No firewall to manage: ufw is not active and the '
                             'host has no iptables reject rule ahead of it.'}
        if enabled:
            steps['realip'] = cls._apply_realip(ranges)
        for name, step in steps.items():
            if not step.get('success'):
                errors.append(step.get('error') or f'{name} failed')
        if errors:
            return {'success': False, 'steps': steps, 'error': '; '.join(errors)}
        return {'success': True, 'steps': steps}

    @classmethod
    def refresh(cls):
        """Weekly: re-download the ranges and, when the switch is on, re-apply."""
        fetched = cls.refresh_ranges()
        if cls.setting() is not True:
            return fetched
        result = cls._converge(True, cls.ranges())
        return {**result, 'changed': fetched.get('changed', False),
                'fetch_error': fetched.get('error')}

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #

    @classmethod
    def register_jobs(cls):
        from app.jobs import registry
        registry.register(JOB_KIND, cls._run_refresh_job, replace=True)

    @classmethod
    def _run_refresh_job(cls, job):  # noqa: ARG003 - job payload unused
        if os.name == 'nt' or cls.setting() is not True:
            return {'skipped': True}
        result = cls.refresh()
        if not result.get('success'):
            raise RuntimeError(result.get('error') or 'Cloudflare-only refresh failed')
        return result
