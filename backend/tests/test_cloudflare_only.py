"""Cloudflare-only web ports (CloudflareOnlyService + /firewall/cloudflare-only).
Fork: arya-arav/ServerKit.

Proving points:
- on an Oracle-style host (INPUT rejects before ufw) the rules go into INPUT
  ahead of the reject, live and in rules.v4; ufw gets IPv4 and IPv6 rules
- hand-made `cloudflare-web` rules and open-to-everyone 80/443 rules are replaced
- a range Cloudflare drops is removed on refresh; turning off reopens 80/443
- a served domain that is not proxied blocks the switch (409) unless forced
- nothing is touched while the switch has never been used
"""
import shlex

import pytest

import app.services.cloudflare_only_service as cos
from app.services.cloudflare_only_service import (
    LEGACY_TAGS, OPEN_TAG, TAG, CloudflareOnlyService,
)
from subprocess_stub import FakeProc

V4 = ['173.245.48.0/20', '103.21.244.0/22']
V6 = ['2400:cb00::/32']

ORACLE_INPUT = [
    '-A INPUT -j CROWDSEC_CHAIN',
    '-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT',
    '-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT',
    '-A INPUT -s 173.245.48.0/20 -p tcp -m multiport --dports 80,443 -m comment --comment cloudflare-web -j ACCEPT',
    '-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT',
    '-A INPUT -j REJECT --reject-with icmp-host-prohibited',
    '-A INPUT -j ufw-before-logging-input',
    '-A INPUT -j ufw-before-input',
]

RULES_V4_TEXT = '\n'.join([
    '*filter',
    ':INPUT ACCEPT [0:0]',
    ':InstanceServices - [0:0]',
    '-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT',
    '-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT',
    '-A INPUT -s 173.245.48.0/20 -p tcp -m multiport --dports 80,443 -m comment --comment cloudflare-web -j ACCEPT',
    '-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT',
    '-A INPUT -j REJECT --reject-with icmp-host-prohibited',
    '-A InstanceServices -d 169.254.169.254/32 -p tcp -m tcp --dport 80 -j ACCEPT',
    'COMMIT',
]) + '\n'


class FakeHost:
    """iptables INPUT chain + ufw rule list + rules.v4 on disk."""

    def __init__(self, rules_v4, input_rules=None, ufw=None, ufw_active=True):
        self.rules_v4 = rules_v4
        self.input = list(ORACLE_INPUT if input_rules is None else input_rules)
        self.ufw = list(ufw or [
            'ufw allow 22/tcp',
            "ufw allow from 173.245.48.0/20 to any port 80,443 proto tcp comment 'cloudflare-web'",
        ])
        self.ufw_active = ufw_active
        self.commands = []

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd[:3] == ['iptables', '-S', 'INPUT']:
            return FakeProc(0, stdout='-P INPUT DROP\n' + '\n'.join(self.input) + '\n')
        if cmd[:3] == ['iptables', '-I', 'INPUT']:
            self.input.insert(int(cmd[3]) - 1, '-A INPUT ' + ' '.join(cmd[4:]))
            return FakeProc(0)
        if cmd[:3] == ['iptables', '-D', 'INPUT']:
            line = '-A INPUT ' + ' '.join(cmd[3:])
            if line not in self.input:
                return FakeProc(1, stderr='Bad rule (does a matching rule exist in that chain?).')
            self.input.remove(line)
            return FakeProc(0)
        if cmd[0] == 'iptables-restore':
            return FakeProc(0)
        if cmd == ['cat', str(self.rules_v4)]:
            return FakeProc(0, stdout=self.rules_v4.read_text())
        if cmd[:2] == ['ufw', 'status']:
            return FakeProc(0, stdout='Status: active\n' if self.ufw_active else 'Status: inactive\n')
        if cmd[:3] == ['ufw', 'show', 'added']:
            return FakeProc(0, stdout="Added user rules (see 'ufw status' for running firewall):\n"
                                       + '\n'.join(self.ufw) + '\n')
        if cmd[:2] == ['ufw', 'allow']:
            args = cmd[2:]
            tag = args[args.index('comment') + 1]
            if 'from' in args:
                src = args[args.index('from') + 1]
                self.ufw.append(f"ufw allow from {src} to any port 80,443 proto tcp comment '{tag}'")
            else:
                self.ufw.append(f"ufw allow 80,443/tcp comment '{tag}'")
            return FakeProc(0)
        if cmd[:2] == ['ufw', 'delete']:
            for line in self.ufw:
                if shlex.split(line)[1:] == cmd[2:]:
                    self.ufw.remove(line)
                    return FakeProc(0)
            return FakeProc(1, stderr='Could not delete non-existent rule')
        if cmd[:2] == ['nginx', '-t'] or cmd[:2] == ['systemctl', 'reload']:
            return FakeProc(0)
        if cmd[:2] == ['rm', '-f']:
            return FakeProc(0)
        return FakeProc(1, stderr=f'unscripted: {cmd}')


@pytest.fixture
def host(monkeypatch, tmp_path):
    rules_v4 = tmp_path / 'rules.v4'
    rules_v4.write_text(RULES_V4_TEXT)
    conf_d = tmp_path / 'conf.d'
    conf_d.mkdir()
    fake = FakeHost(rules_v4)

    def write(path, content, **kwargs):
        with open(path, 'w') as f:
            f.write(content)
        return {'success': True, 'path': path}

    monkeypatch.setattr(cos, 'run_privileged', fake.run)
    monkeypatch.setattr(cos, 'write_privileged_file', write)
    monkeypatch.setattr(cos, 'is_command_available', lambda c: c in ('iptables', 'ufw'))
    monkeypatch.setattr(cos, 'RULES_V4', str(rules_v4))
    monkeypatch.setattr(cos, 'RANGES_FILE', str(tmp_path / 'cloudflare-ips.json'))
    monkeypatch.setattr(cos, 'NGINX_CONF_DIR', str(conf_d))
    monkeypatch.setattr(cos, 'REALIP_CONF', str(conf_d / 'serverkit-cloudflare-realip.conf'))
    monkeypatch.setattr(CloudflareOnlyService, 'fetch_ranges',
                        classmethod(lambda cls: {'v4': list(V4), 'v6': list(V6)}))
    monkeypatch.setattr(CloudflareOnlyService, 'domain_report',
                        classmethod(lambda cls: [{'host': 'shop.example.com', 'state': 'proxied',
                                                  'ips': ['104.16.1.1']}]))
    fake.tmp = tmp_path
    return fake


def _host_web_rules(fake):
    """INPUT rules for 80/443 that sit ahead of the reject."""
    stop = next(i for i, r in enumerate(fake.input) if '-j REJECT' in r)
    return [r for r in fake.input[:stop] if '80' in r]


# ── turning it on ───────────────────────────────────────────────────────────

def test_enable_puts_cloudflare_rules_ahead_of_the_oracle_reject(app, host):
    result = CloudflareOnlyService.apply(True)

    assert result['success'] is True, result
    web = _host_web_rules(host)
    assert sorted(web) == sorted(
        f'-A INPUT -s {c} -p tcp -m multiport --dports 80,443 -m comment --comment {TAG} -j ACCEPT'
        for c in V4)
    assert not any('cloudflare-web' in r for r in host.input)          # hand-made rules adopted
    assert '-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT' not in host.input  # open rule gone
    assert '-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT' in host.input


def test_enable_saves_rules_v4_without_touching_other_chains(app, host):
    CloudflareOnlyService.apply(True)

    text = (host.tmp / 'rules.v4').read_text().splitlines()
    reject = text.index('-A INPUT -j REJECT --reject-with icmp-host-prohibited')
    managed = [i for i, line in enumerate(text) if f'--comment {TAG}' in line]
    assert len(managed) == len(V4) and max(managed) < reject
    assert not any('cloudflare-web' in line for line in text)
    assert '-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT' not in text
    assert '-A InstanceServices -d 169.254.169.254/32 -p tcp -m tcp --dport 80 -j ACCEPT' in text
    assert '-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT' in text


def test_enable_gives_ufw_ipv4_and_ipv6_rules(app, host):
    CloudflareOnlyService.apply(True)

    sources = sorted(shlex.split(line)[shlex.split(line).index('from') + 1]
                     for line in host.ufw if f"'{TAG}'" in line)
    assert sources == sorted(V4 + V6)
    assert 'ufw allow 22/tcp' in host.ufw
    assert not any('cloudflare-web' in line for line in host.ufw)


def test_enable_writes_real_ip_config_and_stores_the_setting(app, host):
    CloudflareOnlyService.apply(True)

    conf = (host.tmp / 'conf.d' / 'serverkit-cloudflare-realip.conf').read_text()
    for cidr in V4 + V6:
        assert f'set_real_ip_from {cidr};' in conf
    assert 'real_ip_header CF-Connecting-IP;' in conf
    assert CloudflareOnlyService.setting() is True
    assert CloudflareOnlyService.status(include_domains=False)['mode'] == 'on'


def test_real_ip_left_alone_when_another_file_sets_it(app, host):
    (host.tmp / 'conf.d' / 'cloudflare-realip.conf').write_text('real_ip_header CF-Connecting-IP;\n')

    result = CloudflareOnlyService.apply(True)

    assert result['success'] is True
    assert 'already set' in result['steps']['realip']['skipped']
    assert not (host.tmp / 'conf.d' / 'serverkit-cloudflare-realip.conf').exists()


def test_enabling_twice_changes_nothing(app, host):
    CloudflareOnlyService.apply(True)
    before = list(host.input), list(host.ufw)
    host.commands.clear()

    CloudflareOnlyService.apply(True)

    assert (list(host.input), list(host.ufw)) == before
    assert not [c for c in host.commands if c[:2] in (['iptables', '-I'], ['iptables', '-D'],
                                                     ['ufw', 'allow'], ['ufw', 'delete'])]


# ── safety ──────────────────────────────────────────────────────────────────

def test_an_unproxied_domain_blocks_the_switch(app, host, monkeypatch):
    monkeypatch.setattr(CloudflareOnlyService, 'domain_report', classmethod(
        lambda cls: [{'host': 'direct.example.com', 'state': 'not_proxied', 'ips': ['203.0.113.10']}]))

    result = CloudflareOnlyService.apply(True)

    assert result['success'] is False
    assert result['blocked_by'] == 'unproxied_domains'
    assert 'direct.example.com' in result['error']
    assert not [c for c in host.commands if c[0] in ('iptables', 'ufw') and c[1] not in ('-S', 'show', 'status')]


def test_force_overrides_the_domain_check(app, host, monkeypatch):
    monkeypatch.setattr(CloudflareOnlyService, 'domain_report', classmethod(
        lambda cls: [{'host': 'direct.example.com', 'state': 'not_proxied', 'ips': ['203.0.113.10']}]))

    assert CloudflareOnlyService.apply(True, force=True)['success'] is True


def test_a_range_cloudflare_drops_is_removed_on_refresh(app, host, monkeypatch):
    CloudflareOnlyService.apply(True)
    monkeypatch.setattr(CloudflareOnlyService, 'fetch_ranges',
                        classmethod(lambda cls: {'v4': ['173.245.48.0/20'], 'v6': list(V6)}))

    result = CloudflareOnlyService.refresh()

    assert result['success'] is True and result['changed'] is True
    assert not any('103.21.244.0/22' in r for r in host.input)
    assert not any('103.21.244.0/22' in line for line in host.ufw)
    assert '103.21.244.0/22' not in (host.tmp / 'rules.v4').read_text()


def test_a_suspicious_download_is_refused():
    with pytest.raises(ValueError):
        CloudflareOnlyService._parse_ranges('0.0.0.0/0\n', 4)
    with pytest.raises(ValueError):
        CloudflareOnlyService._parse_ranges('2400:cb00::/32\n', 4)
    with pytest.raises(ValueError):
        CloudflareOnlyService._parse_ranges('<html>captive portal</html>\n', 4)


# ── turning it off ──────────────────────────────────────────────────────────

def test_disable_reopens_80_and_443_to_everyone(app, host):
    CloudflareOnlyService.apply(True)

    result = CloudflareOnlyService.apply(False)

    assert result['success'] is True, result
    assert _host_web_rules(host) == [
        f'-A INPUT -p tcp -m multiport --dports 80,443 -m comment --comment {OPEN_TAG} -j ACCEPT']
    assert not any(TAG in line for line in host.ufw)
    assert any(OPEN_TAG in line for line in host.ufw)
    text = (host.tmp / 'rules.v4').read_text()
    assert TAG not in text.replace(OPEN_TAG, '') and OPEN_TAG in text
    assert CloudflareOnlyService.status(include_domains=False)['mode'] == 'off'


# ── detection ───────────────────────────────────────────────────────────────

def test_ufw_first_hosts_have_no_host_layer(app, host):
    rules = [shlex.split(r) for r in ['-A INPUT -j ufw-before-input',
                                      '-A INPUT -j REJECT --reject-with icmp-host-prohibited']]
    assert CloudflareOnlyService._host_layer_applies(rules) is False


def test_ufw_only_host_manages_ufw_alone(app, host):
    host.input = ['-A INPUT -j ufw-before-logging-input', '-A INPUT -j ufw-before-input']
    host.ufw = ['ufw allow 22/tcp', 'ufw allow 80/tcp', 'ufw allow 443/tcp']

    result = CloudflareOnlyService.apply(True)

    assert result['success'] is True and 'host_iptables' not in result['steps']
    assert 'ufw allow 80/tcp' not in host.ufw and 'ufw allow 443/tcp' not in host.ufw
    assert (host.tmp / 'rules.v4').read_text() == RULES_V4_TEXT


@pytest.mark.parametrize('line,kind', [
    ('ufw allow 80/tcp', 'open'),
    ('ufw allow 80,443/tcp', 'open'),
    ("ufw allow 'Nginx Full'", 'open'),
    ('ufw allow 22/tcp', None),
    ('ufw allow from 10.0.0.0/8 to any port 80', None),
    (f"ufw allow from 1.2.3.0/24 to any port 80,443 proto tcp comment '{TAG}'", 'managed'),
    (f"ufw allow from 1.2.3.0/24 to any port 80,443 proto tcp comment '{LEGACY_TAGS[0]}'", 'legacy'),
])
def test_ufw_rule_kinds(line, kind):
    assert CloudflareOnlyService._ufw_rule_kind(line) == kind


# ── job + API ───────────────────────────────────────────────────────────────

def test_weekly_job_does_nothing_until_the_switch_is_used(app, host):
    assert CloudflareOnlyService._run_refresh_job(None) == {'skipped': True}
    assert host.commands == []


def test_api_get_is_viewer_and_put_is_admin(client, auth_headers, developer_headers, host):
    assert client.get('/api/v1/firewall/cloudflare-only', headers=developer_headers).status_code == 200
    resp = client.put('/api/v1/firewall/cloudflare-only', json={'enabled': True},
                      headers=developer_headers)
    assert resp.status_code == 403


def test_api_returns_409_for_unproxied_domains(client, auth_headers, host, monkeypatch):
    monkeypatch.setattr(CloudflareOnlyService, 'domain_report', classmethod(
        lambda cls: [{'host': 'direct.example.com', 'state': 'not_proxied', 'ips': ['203.0.113.10']}]))

    resp = client.put('/api/v1/firewall/cloudflare-only', json={'enabled': True}, headers=auth_headers)

    assert resp.status_code == 409
    assert resp.get_json()['domains'][0]['host'] == 'direct.example.com'
