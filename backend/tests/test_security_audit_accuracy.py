"""The Security → Audit checks report what the server really does. Fork: arya-arav/ServerKit.

- SSH: the effective config (sshd -T: drop-ins + defaults), not a substring
  of the main file. Upstream missed drop-ins and counted a commented line.
- Port 22 is informational, not a failed check.
- Updates: what `apt upgrade` would install now, not phased updates apt holds back.
"""
import subprocess

import pytest

from app.services import security_service
from app.services.security_service import SecurityService


def _proc(stdout, rc=0):
    return subprocess.CompletedProcess(['x'], rc, stdout=stdout, stderr='')


@pytest.fixture
def sshd_config(tmp_path, monkeypatch):
    main = tmp_path / 'sshd_config'
    main.write_text('#PermitRootLogin no\nInclude /etc/ssh/sshd_config.d/*.conf\n')
    monkeypatch.setattr(security_service.os.path, 'exists', lambda p: True)
    real_open = open

    def fake_open(path, *a, **k):
        return real_open(main if path == '/etc/ssh/sshd_config' else path, *a, **k)
    monkeypatch.setattr('builtins.open', fake_open)
    return main


def test_effective_settings_from_sshd_t_pass(sshd_config, monkeypatch):
    monkeypatch.setattr(security_service, 'run_privileged', lambda *a, **k: _proc(
        'port 22\npermitrootlogin prohibit-password\npasswordauthentication no\n'
        'kbdinteractiveauthentication no\n'))
    checks = SecurityService._audit_ssh_config()
    assert checks['total_checks'] == 2 and checks['passed_checks'] == 2
    messages = [f['message'] for f in checks['findings']]
    assert 'Root login is restricted' in messages
    assert 'Password authentication disabled' in messages
    assert 'SSH running on default port 22' in messages   # info, not counted


def test_password_login_on_is_reported(sshd_config, monkeypatch):
    monkeypatch.setattr(security_service, 'run_privileged', lambda *a, **k: _proc(
        'permitrootlogin yes\npasswordauthentication yes\n'))
    checks = SecurityService._audit_ssh_config()
    assert checks['passed_checks'] == 0
    assert {'Root login may be enabled', 'Password authentication is enabled'} <= {
        f['message'] for f in checks['findings']}


def test_fallback_reads_drop_ins_first_and_ignores_comments(tmp_path, monkeypatch):
    main = tmp_path / 'sshd_config'
    main.write_text('#PasswordAuthentication no\nPasswordAuthentication yes\nMatch User x\n  PermitRootLogin yes\n')
    drop = tmp_path / '00-hardening.conf'
    drop.write_text('PasswordAuthentication no\nPermitRootLogin prohibit-password\n')
    monkeypatch.setattr(security_service, 'run_privileged', lambda *a, **k: _proc('', rc=1))
    monkeypatch.setattr('glob.glob', lambda pattern: [str(drop)])
    eff = SecurityService._effective_sshd_config(str(main))
    assert eff['passwordauthentication'] == 'no'          # drop-in wins (first value)
    assert eff['permitrootlogin'] == 'prohibit-password'  # Match block ignored


APT_PHASED_ONLY = """Reading package lists...
The following upgrades have been deferred due to phasing:
  libsystemd0 systemd tmux
0 upgraded, 0 newly installed, 0 to remove and 3 not upgraded.
"""
APT_PENDING = """Reading package lists...
Inst git [1:2.51.0-1] (1:2.51.0-2 Ubuntu:26.04/resolute-updates [arm64])
Inst git-man [1:2.51.0-1] (1:2.51.0-2 Ubuntu:26.04/resolute-updates [all])
Conf git (1:2.51.0-2 Ubuntu:26.04/resolute-updates [arm64])
2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""


@pytest.mark.parametrize('output,passed,message', [
    (APT_PHASED_ONLY, 1, 'System is up to date (3 phased updates arriving gradually)'),
    (APT_PENDING, 0, '2 updates available'),
])
def test_updates_count_only_what_would_install(monkeypatch, output, passed, message):
    monkeypatch.setattr(security_service.PackageManager, 'detect', staticmethod(lambda: 'apt'))
    monkeypatch.setattr(security_service, 'run_checked',
                        lambda *a, **k: {'success': True, 'output': output, 'returncode': 0})
    checks = SecurityService._audit_updates()
    assert checks['passed_checks'] == passed
    assert checks['findings'][0]['message'] == message
