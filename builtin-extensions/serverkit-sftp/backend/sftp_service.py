"""SFTP access: jailed, per-folder SFTP accounts served by the system SSH daemon.

Each account is a Linux user in the ``sk-sftp`` group with no shell. An sshd
drop-in forces that group onto ``internal-sftp`` inside a root-owned chroot,
``/srv/sftp-jails/<user>``, whose only content is ``site/`` — a bind mount of
the folder the account was given. Write access to that folder is granted with
POSIX ACLs (the user plus default entries that keep the folder's owner and group
able to use new files), so the site's own permissions are never widened: PHP
running as the folder's group gains nothing.

Everything runs over the existing SSH port. Password logins are enabled only
inside the ``Match Group sk-sftp`` block; the rest of the server keeps whatever
the operator configured (normally key-only).
"""

import base64
import binascii
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app import paths
from app.plugins_sdk import store
from app.utils.system import (PackageManager, ServiceControl, is_command_available,
                              run_checked, run_privileged, unit_is_active,
                              write_privileged_file)

try:
    import grp
    import pwd
except ImportError:  # Windows dev box: the service is Linux-only
    grp = None
    pwd = None

SLUG = 'serverkit-sftp'

GROUP = 'sk-sftp'
JAIL_BASE = '/srv/sftp-jails'
MOUNT_NAME = 'site'
# sshd reads AuthorizedKeysFile *as the connecting user*, so every directory on
# the path must be traversable by them. /etc/serverkit is 0750 root, which is
# why the keys live under /etc/ssh (0755) instead.
KEYS_DIR = '/etc/ssh/serverkit-sftp-keys'
LEGACY_KEYS_DIR = '/etc/serverkit/sftp-keys'
MOUNTINFO = '/proc/self/mountinfo'
SSHD_DROPIN = '/etc/ssh/sshd_config.d/50-serverkit-sftp.conf'
FSTAB = '/etc/fstab'
FSTAB_MARKER = 'x-serverkit-sftp'

USERNAME_RE = re.compile(r'^[a-z][a-z0-9_-]{2,31}$')
# Bind-mount sources go into /etc/fstab, where whitespace needs octal escapes;
# refusing it is simpler than escaping it correctly everywhere.
SAFE_PATH_RE = re.compile(r'^/[A-Za-z0-9._@+/-]+$')
MIN_PASSWORD_LENGTH = 12

# A folder may be shared only from inside one of these trees (or an app's own
# folder). Each must be a strict subfolder, never the tree itself.
ALLOWED_ROOTS = ('/srv', '/var/www', '/home', paths.APPS_DIR)
# Never shareable, even when an application record points into them.
SYSTEM_PREFIXES = ('/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/opt/serverkit',
                   '/proc', '/root', '/run', '/sbin', '/sys', '/usr', '/var/lib', '/var/log')

KEY_TYPES = (
    'ssh-ed25519', 'ssh-rsa',
    'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
    'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com',
)

_ACCOUNTS_KEY = 'accounts'
_HOST_KEY = 'connection_host'


class SFTPError(Exception):
    """A refused request; the message is safe to show the operator."""


# --------------------------------------------------------------------------
# Pure helpers (no system access) — the parts the tests pin down.
# --------------------------------------------------------------------------

# Flags for internal-sftp. Only options every OpenSSH build has: Ubuntu's
# sftp-server rejects -m (force file mode) and exits at once, which sshd -t
# does not catch, so setup() also runs sftp-server with exactly these flags.
SFTP_FLAGS = ('-d', f'/{MOUNT_NAME}')


def render_sshd_dropin() -> str:
    """The sshd drop-in that confines the sk-sftp group."""
    return (
        '# Managed by ServerKit (serverkit-sftp extension). Changes are overwritten.\n'
        f'Match Group {GROUP}\n'
        f'    ChrootDirectory {JAIL_BASE}/%u\n'
        f'    ForceCommand internal-sftp {" ".join(SFTP_FLAGS)}\n'
        f'    AuthorizedKeysFile {KEYS_DIR}/%u\n'
        '    PasswordAuthentication yes\n'
        '    PubkeyAuthentication yes\n'
        '    PermitTTY no\n'
        '    AllowTcpForwarding no\n'
        '    AllowAgentForwarding no\n'
        '    AllowStreamLocalForwarding no\n'
        '    PermitTunnel no\n'
        '    X11Forwarding no\n'
    )


def jail_dir(username: str) -> str:
    return f'{JAIL_BASE}/{username}'


def mount_point(username: str) -> str:
    return f'{JAIL_BASE}/{username}/{MOUNT_NAME}'


def validate_username(username: str) -> Optional[str]:
    """Return an error message, or None when the name is acceptable."""
    if not username or not USERNAME_RE.match(username):
        return ('Username must be 3-32 characters: lowercase letters, numbers, '
                '"-" or "_", starting with a letter.')
    return None


def fstab_add(text: str, source: str, target: str) -> str:
    """Return *text* with a bind-mount line for *target* (replacing any old one)."""
    kept = fstab_remove(text, target)
    if kept and not kept.endswith('\n'):
        kept += '\n'
    return kept + f'{source} {target} none bind,nofail,{FSTAB_MARKER} 0 0\n'


def fstab_remove(text: str, target: str) -> str:
    """Return *text* without the extension's line for *target*.

    Only lines carrying our marker option are ever removed, so an operator's
    own mount of the same path is left alone.
    """
    out = []
    for line in text.splitlines(keepends=True):
        fields = line.split()
        if (len(fields) >= 4 and not line.lstrip().startswith('#')
                and fields[1] == target and FSTAB_MARKER in fields[3].split(',')):
            continue
        out.append(line)
    return ''.join(out)


def parse_public_keys(text: str) -> Tuple[List[str], Optional[str]]:
    """Validate pasted public keys. Returns (keys, error).

    Each key is ``type base64 [comment]``. Leading key options (``command=``,
    ``from=``…) are refused: the extension writes its own ``restrict`` option and
    an operator-supplied option could undo it.
    """
    keys = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 2)
        if parts[0] not in KEY_TYPES:
            return [], f'Unsupported key line (expected a public key such as "ssh-ed25519 AAAA…"): {line[:40]}'
        if len(parts) < 2:
            return [], 'A public key line is missing its key data.'
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError):
            return [], 'A public key is not valid base64.'
        # The blob starts with its own length-prefixed type name.
        if len(blob) < 4 + len(parts[0]) or blob[4:4 + len(parts[0])] != parts[0].encode():
            return [], 'A public key does not match its declared type.'
        comment = parts[2] if len(parts) == 3 else ''
        keys.append(' '.join(p for p in (parts[0], parts[1], comment) if p))
    return keys, None


def mount_points(mountinfo: str) -> set:
    """Mount points listed in a /proc/<pid>/mountinfo text.

    ``os.path.ismount`` cannot be used: it compares the device of a path with
    its parent's, and a bind mount from the same filesystem has the same device,
    so every jail mount would read as "not mounted". Field 5 is the mount point,
    with spaces and other specials octal-escaped (``\\040``).
    """
    points = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4:
            points.add(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), fields[4]))
    return points


def render_authorized_keys(keys: List[str]) -> str:
    return ''.join(f'restrict {k}\n' for k in keys)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip('/') + '/')


def check_target(path: str, *, app_paths: List[str], home_dirs: List[str]) -> Optional[str]:
    """Policy check on an already-resolved folder. Returns an error or None.

    *app_paths* are ServerKit application folders (always shareable), and
    *home_dirs* are the home directories of system accounts: sharing one of
    those, or a folder that contains one, would hand out its ``.ssh``.
    """
    if not SAFE_PATH_RE.match(path) or '/../' in path or path.endswith('/..'):
        return 'Folder paths may contain only letters, numbers and . _ @ + - /'
    if path == '/' or any(_within(path, p) for p in SYSTEM_PREFIXES):
        return f'{path} is a system folder and cannot be shared.'
    if _within(path, JAIL_BASE) or _within(JAIL_BASE, path):
        return 'That folder overlaps the SFTP jails.'
    for home in home_dirs:
        if home and home != '/' and (path == home or _within(home, path)):
            return f'{path} is (or contains) the home directory {home}; share a subfolder of it instead.'
    if any(_within(path, a) for a in app_paths if a and a != '/'):
        return None
    for root in ALLOWED_ROOTS:
        if path != root and _within(path, root):
            return None
    roots = ', '.join(ALLOWED_ROOTS)
    return f'Only folders inside {roots} or an app folder can be shared.'


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def _store():
    return store.for_plugin(SLUG)


def _accounts() -> Dict[str, dict]:
    return dict(_store().get(_ACCOUNTS_KEY, {}) or {})


def _save_accounts(accounts: Dict[str, dict]) -> None:
    _store().set(_ACCOUNTS_KEY, accounts)


def get_account(username: str) -> dict:
    """The stored record, or SFTPError — never act on a user we did not create."""
    if validate_username(username):
        raise SFTPError('Unknown SFTP account.')
    account = _accounts().get(username)
    if not account:
        raise SFTPError('Unknown SFTP account.')
    return account


# --------------------------------------------------------------------------
# System probes
# --------------------------------------------------------------------------

def _group_exists() -> bool:
    if grp is None:
        return False
    try:
        grp.getgrnam(GROUP)
        return True
    except KeyError:
        return False


def _user_exists(username: str) -> bool:
    if pwd is None:
        return False
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def _home_dirs() -> List[str]:
    if pwd is None:
        return []
    return sorted({os.path.realpath(u.pw_dir) for u in pwd.getpwall()
                   if u.pw_dir and not _within(u.pw_dir, JAIL_BASE) and u.pw_dir != f'/{MOUNT_NAME}'})


def _is_mounted(path: str) -> bool:
    try:
        with open(MOUNTINFO, encoding='utf-8') as fh:
            return path in mount_points(fh.read())
    except OSError:
        return False


def _check_sftp_flags() -> Optional[str]:
    """Run the sftp subsystem binary with SFTP_FLAGS; None when it accepts them.

    internal-sftp is the same code as the sftp-server binary, so an option the
    binary refuses would end every session right after login. With stdin at
    EOF a working sftp-server exits 0 at once.
    """
    binary = (_sshd_settings().get('subsystem', '').split() + ['', ''])[1]
    if not binary.startswith('/'):
        return None  # cannot locate it; sshd -t already passed
    result = run_checked([binary, '-e', '-d', '/', *SFTP_FLAGS[2:]], input='', timeout=10)
    if result.get('returncode') == 0:
        return None
    return ((result.get('stderr') or result.get('output') or '').strip() or 'unknown error')[:300]


def _sshd_unit() -> str:
    return 'ssh' if unit_is_active('ssh') else 'sshd'


def _sshd_settings() -> Dict[str, str]:
    result = run_checked(['sshd', '-T'], privileged=True, timeout=20)
    settings = {}
    for line in (result.get('output') or '').splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            settings.setdefault(parts[0].lower(), parts[1].strip())
    return settings


def _dropin_current() -> bool:
    try:
        with open(SSHD_DROPIN, encoding='utf-8') as fh:
            return fh.read() == render_sshd_dropin()
    except OSError:
        return False


def _container_bind_folders(project_dir: str) -> List[str]:
    """Host folders bind-mounted into the compose project's containers.

    An app's ``root_path`` is often only its compose/config folder, while the
    site's files sit in a folder its containers mount (``/srv/x/htdocs``) —
    the folder an SFTP user actually wants.
    """
    listed = run_checked(['docker', 'ps', '-aq', '--filter',
                          f'label=com.docker.compose.project.working_dir={project_dir}'],
                         privileged=True, timeout=20)
    ids = (listed.get('output') or '').split()
    if not listed.get('success') or not ids:
        return []
    inspected = run_checked(['docker', 'inspect', '--format',
                             '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}\n{{end}}{{end}}', *ids],
                            privileged=True, timeout=20)
    folders = []
    for line in (inspected.get('output') or '').splitlines():
        path = line.strip()
        if path and path not in folders and os.path.isdir(path):
            folders.append(os.path.realpath(path))
    return folders


def app_targets() -> List[dict]:
    """Folders of ServerKit applications, for the folder picker.

    One entry per app folder plus one per folder its containers mount, each
    only if it passes the same policy as a typed-in path.
    """
    from app.models.application import Application

    homes = _home_dirs()
    targets = []
    for application in Application.query_active().all():
        root = application.root_path
        if not root or not os.path.isdir(root):
            continue
        root = os.path.realpath(root)
        domains = sorted(application.live_domains, key=lambda d: not d.is_primary)
        folders = [f for f in _container_bind_folders(root) if f != root] + [root]
        for folder in dict.fromkeys(folders):
            trusted = [root] if _within(folder, root) else []
            if check_target(folder, app_paths=trusted, home_dirs=homes):
                continue
            targets.append({
                'key': f'{application.id}:{folder}',
                'app_id': application.id,
                'name': application.name,
                'path': folder,
                'is_app_root': folder == root,
                'domain': domains[0].name if domains else None,
            })
    targets.sort(key=lambda t: ((t['name'] or '').lower(), t['is_app_root'], t['path']))
    return targets


def resolve_target(path: str) -> str:
    """Resolve and police a requested folder; returns its real path."""
    if not path or not path.startswith('/'):
        raise SFTPError('Choose a folder (an absolute path).')
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise SFTPError(f'{path} is not an existing folder.')
    error = check_target(real, app_paths=[t['path'] for t in app_targets()],
                         home_dirs=_home_dirs())
    if error:
        raise SFTPError(error)
    return real


def connection_host() -> Optional[str]:
    override = _store().get(_HOST_KEY)
    if override:
        return override
    try:
        from app.services.site_domain_service import SiteDomainService
        return SiteDomainService.server_ip()
    except Exception:  # noqa: BLE001 — a missing setting is not an error here
        return None


def set_connection_host(host: str) -> str:
    host = (host or '').strip()
    if host and not re.match(r'^[A-Za-z0-9.:-]{1,253}$', host):
        raise SFTPError('Enter a hostname or IP address.')
    if host:
        _store().set(_HOST_KEY, host)
    else:
        _store().delete(_HOST_KEY)
    return host


def get_status() -> dict:
    acl = is_command_available('setfacl')
    group = _group_exists()
    dropin = _dropin_current()
    jails = os.path.isdir(JAIL_BASE) and os.path.isdir(KEYS_DIR)
    settings = _sshd_settings() if (dropin and group) else {}
    return {
        'ready': acl and group and dropin and jails,
        'checks': {
            'acl_tools': acl,
            'group': group,
            'sshd_rule': dropin,
            'jail_folder': jails,
        },
        'host': connection_host(),
        'port': int(settings.get('port', '22').split()[0]) if settings else 22,
        'accounts': len(_accounts()),
    }


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

def _require(proc_or_result, what: str) -> None:
    if isinstance(proc_or_result, dict):
        ok = proc_or_result.get('success')
        detail = proc_or_result.get('error') or proc_or_result.get('stderr')
    else:
        ok = proc_or_result.returncode == 0
        detail = proc_or_result.stderr
    if not ok:
        raise SFTPError(f'{what} failed: {(detail or "").strip()[:300] or "unknown error"}')


def setup() -> dict:
    """Prepare the server once: ACL tools, group, folders and the sshd rule.

    The drop-in is validated with ``sshd -t`` before ssh is reloaded; a config
    sshd rejects is removed again so a bad write can never lock anyone out.
    """
    if not is_command_available('setfacl'):
        _require(PackageManager.install(['acl']), 'Installing the acl package')
    if not _group_exists():
        _require(run_privileged(['groupadd', '--system', GROUP]), 'Creating the sk-sftp group')
    for folder in (JAIL_BASE, KEYS_DIR):
        _require(run_privileged(['install', '-d', '-m', '755', '-o', 'root', '-g', 'root', folder]),
                 f'Creating {folder}')
    # 1.0.0 kept keys under /etc/serverkit, which sshd could not read (see KEYS_DIR).
    if os.path.isdir(LEGACY_KEYS_DIR):
        for name in os.listdir(LEGACY_KEYS_DIR):
            if not validate_username(name):
                run_privileged(['install', '-m', '644', '-o', 'root', '-g', 'root',
                                f'{LEGACY_KEYS_DIR}/{name}', f'{KEYS_DIR}/{name}'])
        run_privileged(['rm', '-rf', LEGACY_KEYS_DIR])

    _require(write_privileged_file(SSHD_DROPIN, render_sshd_dropin(), mode='644', owner='root:root'),
             'Writing the sshd rule')
    test = run_checked(['sshd', '-t'], privileged=True, timeout=30)
    if not test.get('success'):
        run_privileged(['rm', '-f', SSHD_DROPIN])
        raise SFTPError('sshd rejected the SFTP rule, so it was removed again: '
                        + (test.get('stderr') or test.get('error') or '').strip()[:300])
    flags_error = _check_sftp_flags()
    if flags_error:
        run_privileged(['rm', '-f', SSHD_DROPIN])
        raise SFTPError('The SFTP server rejected its options, so the rule was removed again: '
                        + flags_error)
    _require(ServiceControl.reload(_sshd_unit()), 'Reloading ssh')
    return get_status()


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

def _owner_and_group(path: str) -> Tuple[str, str]:
    st = os.stat(path)
    owner = pwd.getpwuid(st.st_uid).pw_name if pwd else str(st.st_uid)
    group = grp.getgrgid(st.st_gid).gr_name if grp else str(st.st_gid)
    return owner, group


def _set_password(username: str, password: str) -> None:
    result = run_checked(['chpasswd'], privileged=True, input=f'{username}:{password}\n', timeout=30)
    _require(result, 'Setting the password')


def _write_keys(username: str, keys: List[str]) -> None:
    _require(write_privileged_file(f'{KEYS_DIR}/{username}', render_authorized_keys(keys),
                                   mode='644', owner='root:root'),
             'Writing the authorized keys')


def _grant_acl(username: str, target: str) -> None:
    owner, group = _owner_and_group(target)
    _require(run_privileged(['setfacl', '-R', '-m', f'u:{username}:rwX', target], timeout=600),
             'Granting folder access')
    defaults = [f'u:{username}:rwX', f'g:{group}:rX']
    if owner != 'root':
        defaults.append(f'u:{owner}:rwX')
    _require(run_privileged(['find', target, '-type', 'd', '-exec', 'setfacl', '-d', '-m',
                             ','.join(defaults), '{}', '+'], timeout=600),
             'Setting default folder permissions')


def _has_extended_acl(target: str) -> bool:
    """Whether anything under *target* already carries a POSIX ACL.

    ``getfacl -s`` lists only files whose ACL goes beyond the mode bits.
    """
    result = run_checked(['getfacl', '-R', '-s', '-p', target], privileged=True, timeout=600)
    return bool((result.get('output') or '').strip())


def _acl_baseline(target: str, accounts: Dict[str, dict]) -> str:
    """'none' when the folder had no ACLs before the extension touched it.

    An account sharing the folder (or a folder around or inside it) has
    already added ACLs, so its baseline is inherited instead of probed.
    """
    for other in accounts.values():
        path = other.get('path') or ''
        if path and (_within(target, path) or _within(path, target)):
            return other.get('acl_baseline', 'existing')
    return 'existing' if _has_extended_acl(target) else 'none'


def _revoke_acl(username: str, target: str, *, restore: bool = False) -> None:
    """Remove the account's ACL entries; with *restore*, every ACL under the folder.

    *restore* is only passed when the folder had no ACLs before the extension
    and no other account still uses it: the default entries for the owner and
    group (and the ACL mask on every file) were ours, and would otherwise stay
    forever.
    """
    if restore:
        run_privileged(['setfacl', '-R', '-b', target], timeout=600)
        return
    run_privileged(['setfacl', '-R', '-x', f'u:{username}', target], timeout=600)
    run_privileged(['find', target, '-type', 'd', '-exec', 'setfacl', '-d', '-x',
                    f'u:{username}', '{}', '+'], timeout=600)


def _restore_allowed(username: str, account: dict, accounts: Dict[str, dict]) -> bool:
    if account.get('acl_baseline') != 'none':
        return False
    target = account.get('path') or ''
    return not any(
        name != username and other.get('path')
        and (_within(target, other['path']) or _within(other['path'], target))
        for name, other in accounts.items())


def _update_fstab(transform) -> None:
    try:
        with open(FSTAB, encoding='utf-8') as fh:
            current = fh.read()
    except OSError as exc:
        raise SFTPError(f'Could not read {FSTAB}: {exc}') from exc
    updated = transform(current)
    if updated != current:
        # tee is not atomic; keep the last good copy next to it.
        _require(run_privileged(['cp', '-p', FSTAB, f'{FSTAB}.serverkit-sftp.bak']),
                 f'Backing up {FSTAB}')
        _require(write_privileged_file(FSTAB, updated), f'Updating {FSTAB}')


def _mount(username: str, target: str) -> None:
    jail, point = jail_dir(username), mount_point(username)
    for folder in (jail, point):
        _require(run_privileged(['install', '-d', '-m', '755', '-o', 'root', '-g', 'root', folder]),
                 f'Creating {folder}')
    _update_fstab(lambda text: fstab_add(text, target, point))
    if not _is_mounted(point):
        _require(run_privileged(['mount', '--bind', target, point]), 'Mounting the folder')


def _unmount(username: str) -> None:
    point = mount_point(username)
    if _is_mounted(point):
        run_privileged(['umount', point])
    _update_fstab(lambda text: fstab_remove(text, point))
    run_privileged(['rmdir', point])
    run_privileged(['rmdir', jail_dir(username)])


def _public(account: dict, last_logins: Dict[str, str]) -> dict:
    username = account['username']
    return {
        'username': username,
        'path': account['path'],
        'app_id': account.get('app_id'),
        'app_name': account.get('app_name'),
        'domain': account.get('domain'),
        'enabled': bool(account.get('enabled', True)),
        'has_password': bool(account.get('has_password')),
        'keys': account.get('keys', []),
        'created_at': account.get('created_at'),
        'created_by': account.get('created_by'),
        'mounted': _is_mounted(mount_point(username)),
        'exists': _user_exists(username),
        'last_login': last_logins.get(username),
    }


_ACCEPTED_RE = re.compile(r'^(\S+)\s.*Accepted (?:password|publickey) for (\S+) from')


def _last_logins(usernames: List[str]) -> Dict[str, str]:
    """Most recent accepted login per account, from the ssh journal (90 days)."""
    if not usernames:
        return {}
    result = run_checked(['journalctl', '-u', 'ssh', '-u', 'sshd', '--since', '-90d',
                          '--no-pager', '-o', 'short-iso', '-r', '-n', '5000',
                          '--grep', 'Accepted (password|publickey) for'],
                         privileged=True, timeout=30)
    wanted = set(usernames)
    found = {}
    for line in (result.get('output') or '').splitlines():
        match = _ACCEPTED_RE.match(line)
        if match and match.group(2) in wanted and match.group(2) not in found:
            found[match.group(2)] = match.group(1)
    return found


def list_accounts() -> List[dict]:
    accounts = _accounts()
    logins = _last_logins(list(accounts))
    return [_public(a, logins) for a in sorted(accounts.values(), key=lambda a: a['username'])]


def create_account(username: str, path: str, *, password: Optional[str] = None,
                   generate_password: bool = True, public_keys: str = '',
                   app_id: Optional[int] = None, created_by: Optional[str] = None) -> dict:
    if not get_status()['ready']:
        raise SFTPError('Run SFTP setup first.')
    error = validate_username(username)
    if error:
        raise SFTPError(error)
    if username in _accounts() or _user_exists(username):
        raise SFTPError(f'A user named {username} already exists on this server.')

    target = resolve_target(path)
    keys, key_error = parse_public_keys(public_keys)
    if key_error:
        raise SFTPError(key_error)
    if password:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise SFTPError(f'Passwords must be at least {MIN_PASSWORD_LENGTH} characters.')
    elif generate_password:
        password = secrets.token_urlsafe(18)
    if not password and not keys:
        raise SFTPError('Give the account a password, a public key, or both.')

    try:
        app_id = int(app_id) if app_id not in (None, '') else None
    except (TypeError, ValueError):
        app_id = None
    app = next((t for t in app_targets() if t['app_id'] == app_id), None) if app_id else None
    baseline = _acl_baseline(target, _accounts())

    created_user = False
    try:
        _require(run_privileged(['useradd', '-M', '-d', f'/{MOUNT_NAME}', '-s', '/usr/sbin/nologin',
                                 '-G', GROUP, '-c', 'ServerKit SFTP', username]),
                 'Creating the user')
        created_user = True
        if password:
            _set_password(username, password)
        _write_keys(username, keys)
        _mount(username, target)
        _grant_acl(username, target)
    except SFTPError:
        _cleanup(username, target, user_created=created_user,
                 restore_acl=_restore_allowed(username, {'path': target, 'acl_baseline': baseline},
                                              _accounts()))
        raise

    accounts = _accounts()
    accounts[username] = {
        'username': username,
        'path': target,
        'acl_baseline': baseline,
        'app_id': app['app_id'] if app else None,
        'app_name': app['name'] if app else None,
        'domain': app['domain'] if app else None,
        'enabled': True,
        'has_password': bool(password),
        'keys': keys,
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'created_by': created_by,
    }
    _save_accounts(accounts)
    result = _public(accounts[username], {})
    result['password'] = password  # shown once; never stored
    return result


def _cleanup(username: str, target: str, *, user_created: bool, restore_acl: bool = False) -> None:
    """Best-effort undo of a half-created or deleted account."""
    run_privileged(['pkill', '-KILL', '-u', username])
    _unmount(username)
    if user_created:
        owner, group = _owner_and_group(target)
        # Files the account uploaded would otherwise belong to a uid nobody has.
        run_privileged(['find', target, '-user', username, '-exec', 'chown', f'{owner}:{group}', '{}', '+'],
                       timeout=600)
    _revoke_acl(username, target, restore=restore_acl)
    if user_created:
        run_privileged(['userdel', username])
    run_privileged(['rm', '-f', f'{KEYS_DIR}/{username}'])


def delete_account(username: str) -> None:
    account = get_account(username)
    accounts = _accounts()
    _cleanup(username, account['path'], user_created=_user_exists(username),
             restore_acl=_restore_allowed(username, account, accounts))
    accounts.pop(username, None)
    _save_accounts(accounts)


def reset_password(username: str, password: Optional[str] = None) -> str:
    get_account(username)
    if password:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise SFTPError(f'Passwords must be at least {MIN_PASSWORD_LENGTH} characters.')
    else:
        password = secrets.token_urlsafe(18)
    _set_password(username, password)
    accounts = _accounts()
    accounts[username]['has_password'] = True
    _save_accounts(accounts)
    return password


def set_keys(username: str, public_keys: str) -> List[str]:
    account = get_account(username)
    keys, error = parse_public_keys(public_keys)
    if error:
        raise SFTPError(error)
    if not keys and not account.get('has_password'):
        raise SFTPError('An account without a password needs at least one key.')
    _write_keys(username, keys)
    accounts = _accounts()
    accounts[username]['keys'] = keys
    _save_accounts(accounts)
    return keys


def set_enabled(username: str, enabled: bool) -> None:
    get_account(username)
    # An expired account is refused by PAM for passwords AND keys, which a
    # locked password (usermod -L) is not.
    _require(run_privileged(['usermod', '--expiredate', '' if enabled else '1', username]),
             'Updating the account')
    if not enabled:
        run_privileged(['pkill', '-KILL', '-u', username])
    accounts = _accounts()
    accounts[username]['enabled'] = enabled
    _save_accounts(accounts)
