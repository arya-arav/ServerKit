"""SFTP access builtin extension (serverkit-sftp).

Covers the parts that decide what a jailed account can reach: the sshd rule,
the folder policy (never a home directory or system path), fstab edits that
only ever touch the extension's own lines, key parsing, and that account
actions refuse users the extension did not create.
"""
import base64
import importlib
import struct
import sys

import pytest

from app.models.plugin import InstalledPlugin
from app.services import plugin_service

SLUG = 'serverkit-sftp'
_PKG = f'app.plugins.{SLUG}'


@pytest.fixture
def svc():
    assert plugin_service._ensure_builtin_backend_importable(SLUG)
    return importlib.import_module(f'{_PKG}.sftp_service')


def _pubkey(key_type='ssh-ed25519', comment='me@laptop'):
    blob = struct.pack('>I', len(key_type)) + key_type.encode() + struct.pack('>I', 32) + b'\x01' * 32
    return f'{key_type} {base64.b64encode(blob).decode()} {comment}'.strip()


# --- sshd rule ---------------------------------------------------------------

def test_sshd_dropin_confines_only_the_sftp_group(svc):
    text = svc.render_sshd_dropin()
    lines = [line.strip() for line in text.splitlines()]
    assert 'Match Group sk-sftp' in lines
    assert 'ChrootDirectory /srv/sftp-jails/%u' in lines
    assert 'ForceCommand internal-sftp -d /site -m 0664' in lines
    assert 'PermitTTY no' in lines
    assert 'AllowTcpForwarding no' in lines
    # sshd reads keys as the connecting user; /etc/serverkit is 0750 root.
    assert 'AuthorizedKeysFile /etc/ssh/serverkit-sftp-keys/%u' in lines
    # Password logins are switched on inside the Match block only.
    match_at = lines.index('Match Group sk-sftp')
    assert lines.index('PasswordAuthentication yes') > match_at


# --- usernames ---------------------------------------------------------------

@pytest.mark.parametrize('name', ['web', 'shop-dev', 'a_b1'])
def test_valid_usernames(svc, name):
    assert svc.validate_username(name) is None


@pytest.mark.parametrize('name', ['', 'ab', 'Root', '1abc', 'a b', 'x' * 33, '../etc', 'a;b'])
def test_invalid_usernames(svc, name):
    assert svc.validate_username(name)


# --- folder policy -------------------------------------------------------------

HOMES = ['/root', '/home/ubuntu', '/home/opc', '/var/www', '/nonexistent']


@pytest.mark.parametrize('path', [
    '/srv/shop/htdocs/shop.example.com',
    '/var/www/html',
    '/home/ubuntu/sites/blog',
    '/var/serverkit/apps/shop',
])
def test_target_allowed(svc, path):
    assert svc.check_target(path, app_paths=[], home_dirs=HOMES) is None


@pytest.mark.parametrize('path', [
    '/', '/srv', '/var/www', '/home',          # a whole tree, never a subfolder
    '/home/ubuntu',                            # a home directory (its .ssh)
    '/etc/nginx', '/root/x', '/opt/serverkit/backend', '/var/lib/docker',
    '/srv/sftp-jails/someone',                 # inside the jails
    '/tmp/upload',                             # outside every allowed tree
    '/srv/my site',                            # whitespace would break fstab
])
def test_target_refused(svc, path):
    assert svc.check_target(path, app_paths=[], home_dirs=HOMES)


def test_folder_containing_a_home_is_refused(svc):
    # /srv/users holds a user's home: sharing it would hand out that .ssh.
    assert svc.check_target('/srv/users', app_paths=[], home_dirs=['/srv/users/deploy'])


def test_app_folder_allowed_but_never_a_system_path(svc):
    assert svc.check_target('/data/apps/shop', app_paths=['/data/apps/shop'], home_dirs=HOMES) is None
    assert svc.check_target('/data/apps/shop/public', app_paths=['/data/apps/shop'], home_dirs=HOMES) is None
    # An app record cannot make a system folder or the filesystem root shareable.
    assert svc.check_target('/etc/myapp', app_paths=['/etc/myapp'], home_dirs=HOMES)
    assert svc.check_target('/data', app_paths=['/'], home_dirs=HOMES)


# --- fstab ---------------------------------------------------------------------

FSTAB = (
    'LABEL=cloudimg-rootfs / ext4 discard,errors=remount-ro 0 1\n'
    '# a comment mentioning /srv/sftp-jails/web/site\n'
    '/data /srv/sftp-jails/web/site none bind 0 0\n'  # operator's own line
)


def test_fstab_add_appends_marked_line_and_keeps_the_rest(svc):
    out = svc.fstab_add(FSTAB, '/srv/site', '/srv/sftp-jails/web/site')
    assert out.startswith(FSTAB)
    assert out.endswith('/srv/site /srv/sftp-jails/web/site none bind,nofail,x-serverkit-sftp 0 0\n')


def test_fstab_add_replaces_previous_marked_line(svc):
    once = svc.fstab_add(FSTAB, '/srv/a', '/srv/sftp-jails/web/site')
    twice = svc.fstab_add(once, '/srv/b', '/srv/sftp-jails/web/site')
    assert twice.count('x-serverkit-sftp') == 1
    assert '/srv/b /srv/sftp-jails/web/site' in twice


def test_fstab_remove_only_touches_marked_lines(svc):
    added = svc.fstab_add(FSTAB, '/srv/site', '/srv/sftp-jails/web/site')
    assert svc.fstab_remove(added, '/srv/sftp-jails/web/site') == FSTAB


# --- mount detection -------------------------------------------------------------

MOUNTINFO = (
    '29 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
    # A bind mount from the same filesystem: os.path.ismount() reports False here.
    '512 29 8:1 /srv/site /srv/sftp-jails/web/site rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
    '513 29 8:1 /srv/a\\040b /srv/sftp-jails/x/my\\040site rw - ext4 /dev/sda1 rw\n'
)


def test_mount_points_sees_same_filesystem_bind_mounts(svc):
    points = svc.mount_points(MOUNTINFO)
    assert '/srv/sftp-jails/web/site' in points
    assert '/srv/sftp-jails/x/my site' in points  # octal escapes decoded
    assert '/srv/sftp-jails/web' not in points


# --- public keys ------------------------------------------------------------------

def test_parse_public_keys_accepts_keys_and_skips_blanks(svc):
    text = f'\n{_pubkey()}\n# note\n{_pubkey("ssh-rsa", "")}\n'
    keys, error = svc.parse_public_keys(text)
    assert error is None
    assert len(keys) == 2
    assert svc.render_authorized_keys(keys).splitlines()[0].startswith('restrict ssh-ed25519 ')


@pytest.mark.parametrize('line', [
    'command="/bin/sh" ' + 'ssh-ed25519 AAAA',   # options could undo `restrict`
    'ssh-ed25519 not-base64!!',
    'ssh-ed25519',
    'hello world',
])
def test_parse_public_keys_refuses_bad_lines(svc, line):
    keys, error = svc.parse_public_keys(line)
    assert error and keys == []


def test_parse_public_keys_refuses_type_mismatch(svc):
    blob = _pubkey('ssh-rsa').split()[1]
    keys, error = svc.parse_public_keys(f'ssh-ed25519 {blob}')
    assert error and keys == []


# --- account actions refuse users the extension did not create ---------------------

@pytest.mark.parametrize('name', ['ubuntu', 'root', 'opc', '../x'])
def test_actions_refuse_unknown_users(app, svc, fake_subprocess, name):
    with app.app_context():
        for action in (lambda: svc.delete_account(name),
                       lambda: svc.reset_password(name),
                       lambda: svc.set_enabled(name, False),
                       lambda: svc.set_keys(name, '')):
            with pytest.raises(svc.SFTPError):
                action()
    assert fake_subprocess.commands() == []


def test_create_account_runs_the_expected_steps(app, svc, fake_subprocess, monkeypatch):
    monkeypatch.setattr(svc, 'get_status', lambda: {'ready': True})
    monkeypatch.setattr(svc, 'resolve_target', lambda path: '/srv/site')
    monkeypatch.setattr(svc, 'app_targets', lambda: [])
    monkeypatch.setattr(svc, '_user_exists', lambda name: False)
    monkeypatch.setattr(svc, '_owner_and_group', lambda path: ('ubuntu', 'www-data'))
    monkeypatch.setattr(svc, '_update_fstab', lambda transform: None)
    monkeypatch.setattr(svc, '_is_mounted', lambda path: False)
    fake_subprocess.script(['useradd'])
    fake_subprocess.script(['chpasswd'])
    fake_subprocess.script(['tee'])
    fake_subprocess.script(['chmod'])
    fake_subprocess.script(['chown'])
    fake_subprocess.script(['install'])
    fake_subprocess.script(['mount'])
    fake_subprocess.script(['setfacl'])
    fake_subprocess.script(['find'])

    with app.app_context():
        account = svc.create_account('web', '/srv/site', password='correct-horse-battery')
        assert account['username'] == 'web'
        assert account['password'] == 'correct-horse-battery'
        assert svc.get_account('web')['path'] == '/srv/site'
        assert 'password' not in svc.get_account('web')  # never stored

    commands = fake_subprocess.commands()
    useradd = next(c for c in commands if c[0] == 'useradd')
    assert ['-s', '/usr/sbin/nologin'] == useradd[useradd.index('-s'):useradd.index('-s') + 2]
    assert ['-G', 'sk-sftp'] == useradd[useradd.index('-G'):useradd.index('-G') + 2]
    # The password goes through stdin, never onto an argv.
    assert all('correct-horse-battery' not in ' '.join(c) for c in commands)
    assert ['mount', '--bind', '/srv/site', '/srv/sftp-jails/web/site'] in commands
    assert ['setfacl', '-R', '-m', 'u:web:rwX', '/srv/site'] in commands
    defaults = next(c for c in commands if c[:4] == ['find', '/srv/site', '-type', 'd'])
    assert 'u:web:rwX,g:www-data:rX,u:ubuntu:rwX' in defaults


def test_create_account_requires_setup(app, svc, monkeypatch, fake_subprocess):
    monkeypatch.setattr(svc, 'get_status', lambda: {'ready': False})
    with app.app_context(), pytest.raises(svc.SFTPError, match='setup'):
        svc.create_account('web', '/srv/site')
    assert fake_subprocess.commands() == []


def test_short_password_refused_before_any_command(app, svc, monkeypatch, fake_subprocess):
    monkeypatch.setattr(svc, 'get_status', lambda: {'ready': True})
    monkeypatch.setattr(svc, 'resolve_target', lambda path: '/srv/site')
    monkeypatch.setattr(svc, '_user_exists', lambda name: False)
    with app.app_context(), pytest.raises(svc.SFTPError, match='12'):
        svc.create_account('web', '/srv/site', password='short')
    assert fake_subprocess.commands() == []


# --- install / routes ------------------------------------------------------------------

@pytest.fixture
def install_dirs(tmp_path, monkeypatch):
    backend = tmp_path / 'plugins_backend'
    frontend = tmp_path / 'plugins_frontend'
    backend.mkdir()
    frontend.mkdir()
    monkeypatch.setattr(plugin_service, 'BACKEND_PLUGINS_DIR', str(backend))
    monkeypatch.setattr(plugin_service, 'FRONTEND_PLUGINS_DIR', str(frontend))
    app_pkg_plugins = importlib.import_module('app.plugins')
    added = str(backend)
    if added not in app_pkg_plugins.__path__:
        app_pkg_plugins.__path__.append(added)
    yield
    if added in app_pkg_plugins.__path__:
        app_pkg_plugins.__path__.remove(added)
    for name in list(sys.modules):
        if name == _PKG or name.startswith(_PKG + '.'):
            del sys.modules[name]


@pytest.mark.fresh_app
def test_install_registers_routes_and_gates_mutations(app, client, auth_headers, viewer_headers, install_dirs):
    plugin = plugin_service.install_builtin_extension(SLUG)
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE
    assert plugin.url_prefix == '/api/v1/sftp'

    assert client.get('/api/v1/sftp/accounts', headers=auth_headers).status_code == 200
    assert client.get('/api/v1/sftp/accounts', headers=viewer_headers).status_code == 200
    # Every mutating route is admin-only.
    for method, url in (('post', '/api/v1/sftp/setup'),
                        ('put', '/api/v1/sftp/settings'),
                        ('post', '/api/v1/sftp/accounts'),
                        ('delete', '/api/v1/sftp/accounts/web'),
                        ('post', '/api/v1/sftp/accounts/web/password'),
                        ('put', '/api/v1/sftp/accounts/web/keys'),
                        ('post', '/api/v1/sftp/accounts/web/toggle')):
        resp = getattr(client, method)(url, headers=viewer_headers, json={})
        assert resp.status_code == 403, (method, url, resp.status_code)

    assert plugin_service.uninstall_plugin(plugin.id) is True


def test_app_targets_offer_container_folders_but_apply_the_policy(app, svc, monkeypatch):
    class _Dom:
        name, is_primary = 'shop.example.com', True

    class _App:
        id, name, root_path, live_domains = 7, 'shop', '/srv/shop/stack', [_Dom()]

    class _Query:
        @staticmethod
        def all():
            return [_App()]

    from app.models.application import Application
    monkeypatch.setattr(Application, 'query_active', classmethod(lambda cls: _Query()))
    monkeypatch.setattr(svc.os.path, 'isdir', lambda p: True)
    monkeypatch.setattr(svc.os.path, 'realpath', lambda p: p)
    monkeypatch.setattr(svc, '_home_dirs', lambda: ['/home/ubuntu'])
    monkeypatch.setattr(svc, '_container_bind_folders',
                        lambda root: ['/srv/shop/htdocs', '/etc/nginx', '/home/ubuntu', '/srv/shop/stack'])

    paths = [t['path'] for t in svc.app_targets()]
    # Mounted site folder first, app folder last; system paths and homes never offered.
    assert paths == ['/srv/shop/htdocs', '/srv/shop/stack']
