"""Scoped file-integrity monitoring (FIM).

Baseline-and-diff over the paths ServerKit manages:

- ``nginx``    — /etc/nginx/sites-enabled + /etc/nginx/conf.d
- ``systemd``  — the serverkit-owned units in /etc/systemd/system
- ``app:<id>`` — an application docroot, on explicit per-app opt-in
                 (opt-in set stored in SettingsService key 'fim_app_optins')

Baselines are JSON files under ``STATE_DIR`` (``/var/serverkit/security/fim``
by default, overridable for tests). Everything is best-effort and
OS-agnostic: hashing works on any existing directory; on dev boxes the
default Linux paths simply don't exist and the scope reads as empty.

Changes found by the scheduled ``security.fim.check`` job feed the
Notifications Bus as ``integrity.changed`` events.

This complements — but does not depend on — config drift detection, which
compares rendered configs against what ServerKit would re-render. FIM only
answers "did files change since the operator last accepted a baseline?".
"""

import fnmatch
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app import paths

logger = logging.getLogger(__name__)

FIM_JOB_KIND = 'security.fim.check'
FIM_SCHEDULE_NAME = 'fim-check'
FIM_EVENT_KEY = 'integrity.changed'
OPTIN_SETTING_KEY = 'fim_app_optins'

_APP_SCOPE_RE = re.compile(r'^app:(\d+)$')

# Cap the change lists persisted/returned per category so a mass change
# (e.g. a full redeploy) can't balloon the state file or the API payload.
MAX_LISTED_CHANGES = 500
# Sample paths included in a notification.
NOTIFY_SAMPLE_CAP = 10


class FileIntegrityScopeError(ValueError):
    """Unknown/invalid scope or scope without a usable root (HTTP 400)."""


class FileIntegrityService:
    """Baseline-and-diff file integrity monitoring over managed paths."""

    # Overridable in tests.
    STATE_DIR = os.path.join(paths.SERVERKIT_DIR, 'security', 'fim')

    NGINX_ROOTS = ['/etc/nginx/sites-enabled', '/etc/nginx/conf.d']
    SYSTEMD_ROOT = '/etc/systemd/system'
    # Only serverkit-owned units are in scope for the systemd walk.
    SYSTEMD_INCLUDE = ['serverkit*']

    # Default exclude patterns, matched (fnmatch) against the file's relative
    # path within its scope root and against every path segment.
    DEFAULT_EXCLUDES = {
        'nginx': ['*.log'],
        'systemd': [],
        'app': [
            '*.log',
            'logs/*',
            'log/*',
            'cache/*',
            'tmp/*',
            'temp/*',
            'node_modules/*',
            'vendor/cache/*',
            '.git/*',
            '__pycache__/*',
            '*.pyc',
            'wp-content/uploads/*',
            'wp-content/cache/*',
            'wp-content/upgrade/*',
        ],
    }

    # ------------------------------------------------------------------ #
    # Scope resolution
    # ------------------------------------------------------------------ #

    @classmethod
    def _scope_kind(cls, scope: str) -> str:
        if scope in ('nginx', 'systemd'):
            return scope
        if _APP_SCOPE_RE.match(scope or ''):
            return 'app'
        raise FileIntegrityScopeError(f'Unknown scope: {scope!r}')

    @classmethod
    def _scope_roots(cls, scope: str) -> List[str]:
        """Existing directories for a scope (may be empty on dev boxes)."""
        kind = cls._scope_kind(scope)
        if kind == 'nginx':
            roots = list(cls.NGINX_ROOTS)
        elif kind == 'systemd':
            roots = [cls.SYSTEMD_ROOT]
        else:
            app_id = int(_APP_SCOPE_RE.match(scope).group(1))
            from app.models.application import Application
            # query_active: this root is what the sweep walks, hashes and
            # notifies on. A baseline outlives the app, so a deleted one must
            # fail to resolve rather than hand back a docroot purge removed.
            application = Application.query_active().filter_by(id=app_id).first()
            if not application:
                raise FileIntegrityScopeError(f'Application {app_id} not found')
            roots = [application.root_path] if application.root_path else []
        return [r for r in roots if r and os.path.isdir(r)]

    @classmethod
    def _scope_excludes(cls, scope: str, options: Optional[Dict] = None) -> List[str]:
        kind = cls._scope_kind(scope)
        excludes = list(cls.DEFAULT_EXCLUDES.get(kind, []))
        if options and isinstance(options.get('exclude'), list):
            excludes.extend(str(p) for p in options['exclude'])
        return excludes

    @classmethod
    def _include_file(cls, scope: str, filename: str) -> bool:
        """Per-scope include filter on the file name (systemd → serverkit-*)."""
        if cls._scope_kind(scope) != 'systemd':
            return True
        return any(fnmatch.fnmatch(filename, pat) for pat in cls.SYSTEMD_INCLUDE)

    @staticmethod
    def _excluded(rel_path: str, excludes: List[str]) -> bool:
        rel = rel_path.replace(os.sep, '/')
        segments = rel.split('/')
        for pat in excludes:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(segments[-1], pat):
                return True
            # Directory patterns like 'cache/*' should also match nested
            # 'wp-content/cache/…' style trees when written bare.
            if pat.endswith('/*'):
                d = pat[:-2]
                if d in segments[:-1]:
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Hashing / snapshot
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, 'rb') as fh:
            for block in iter(lambda: fh.read(65536), b''):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _snapshot(cls, scope: str, roots: List[str], excludes: List[str]) -> Dict[str, Dict]:
        """Walk the scope roots → {relpath: {sha256, size, mode, mtime}}.

        With multiple roots (nginx) the relpath is prefixed with the root's
        basename ('sites-enabled/…', 'conf.d/…') so keys stay unambiguous.
        """
        prefix_keys = len(roots) > 1
        files: Dict[str, Dict] = {}
        for root in roots:
            prefix = os.path.basename(root.rstrip('/\\')) if prefix_keys else ''
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune excluded directories early (cheap on big docroots).
                rel_dir = os.path.relpath(dirpath, root)
                rel_dir = '' if rel_dir == '.' else rel_dir.replace(os.sep, '/')
                dirnames[:] = [
                    d for d in dirnames
                    if not cls._excluded((rel_dir + '/' + d if rel_dir else d) + '/x', excludes)
                ]
                for filename in filenames:
                    if not cls._include_file(scope, filename):
                        continue
                    rel = (rel_dir + '/' + filename) if rel_dir else filename
                    if cls._excluded(rel, excludes):
                        continue
                    key = (prefix + '/' + rel) if prefix else rel
                    full = os.path.join(dirpath, filename)
                    try:
                        stat = os.stat(full)
                        files[key] = {
                            'sha256': cls._sha256(full),
                            'size': stat.st_size,
                            'mode': stat.st_mode,
                            'mtime': stat.st_mtime,
                        }
                    except (PermissionError, FileNotFoundError, OSError):
                        continue  # best-effort: unreadable files are skipped
        return files

    # ------------------------------------------------------------------ #
    # State store
    # ------------------------------------------------------------------ #

    @classmethod
    def _state_path(cls, scope: str) -> str:
        # 'app:3' → 'app_3.json' (keep filenames filesystem-safe).
        return os.path.join(cls.STATE_DIR, scope.replace(':', '_') + '.json')

    @classmethod
    def _load_state(cls, scope: str) -> Optional[Dict]:
        path = cls._state_path(scope)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as fh:
                return json.load(fh)
        except (ValueError, OSError):
            logger.warning('FIM state for %s unreadable; treating as no baseline', scope)
            return None

    @classmethod
    def _save_state(cls, scope: str, state: Dict) -> None:
        os.makedirs(cls.STATE_DIR, exist_ok=True)
        path = cls._state_path(scope)
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(state, fh)
        os.replace(tmp, path)

    @classmethod
    def _baselined_scopes(cls) -> List[str]:
        if not os.path.isdir(cls.STATE_DIR):
            return []
        scopes = []
        for name in sorted(os.listdir(cls.STATE_DIR)):
            if name.endswith('.json'):
                scopes.append(name[:-5].replace('app_', 'app:', 1)
                              if name.startswith('app_') else name[:-5])
        return scopes

    # ------------------------------------------------------------------ #
    # Opt-ins (per-app)
    # ------------------------------------------------------------------ #

    @classmethod
    def get_app_optins(cls) -> List[int]:
        from app.services.settings_service import SettingsService
        raw = SettingsService.get(OPTIN_SETTING_KEY)
        if not raw:
            return []
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            return sorted({int(i) for i in ids})
        except (ValueError, TypeError):
            return []

    @classmethod
    def set_app_optins(cls, app_ids: List[int]) -> List[int]:
        try:
            ids = sorted({int(i) for i in (app_ids or [])})
        except (ValueError, TypeError):
            raise FileIntegrityScopeError('app_ids must be a list of integers')

        previous = set(cls.get_app_optins())
        from app.services.settings_service import SettingsService
        SettingsService.set(OPTIN_SETTING_KEY, json.dumps(ids))

        # Drop stale baselines for apps that were opted out (best-effort).
        for removed in previous - set(ids):
            try:
                path = cls._state_path(f'app:{removed}')
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        return ids

    # ------------------------------------------------------------------ #
    # Core operations
    # ------------------------------------------------------------------ #

    @classmethod
    def baseline(cls, scope: str, options: Optional[Dict] = None) -> Dict:
        """(Re-)create the baseline for a scope. Returns a summary dict."""
        roots = cls._scope_roots(scope)  # raises on unknown scope
        excludes = cls._scope_excludes(scope, options)
        files = cls._snapshot(scope, roots, excludes)
        state = {
            'scope': scope,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'roots': roots,
            'excludes': excludes,
            'files': files,
            'last_check': None,
        }
        cls._save_state(scope, state)
        return {
            'scope': scope,
            'created_at': state['created_at'],
            'file_count': len(files),
            'roots': roots,
        }

    @classmethod
    def check(cls, scope: str) -> Dict:
        """Diff current filesystem state against the scope baseline."""
        cls._scope_kind(scope)  # validate early
        state = cls._load_state(scope)
        if state is None:
            raise FileIntegrityScopeError(f'No baseline for scope {scope!r} — baseline it first')

        roots = cls._scope_roots(scope)
        excludes = state.get('excludes') or cls._scope_excludes(scope)
        current = cls._snapshot(scope, roots, excludes)
        baseline_files = state.get('files') or {}

        added = sorted(set(current) - set(baseline_files))
        removed = sorted(set(baseline_files) - set(current))
        modified = []
        for rel in sorted(set(current) & set(baseline_files)):
            was, now = baseline_files[rel], current[rel]
            what = []
            if now['sha256'] != was['sha256']:
                what.append('hash')
            if now['size'] != was['size']:
                what.append('size')
            if now['mode'] != was['mode']:
                what.append('mode')
            if what:
                modified.append({'path': rel, 'what': what})

        result = {
            'scope': scope,
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'baseline_created_at': state.get('created_at'),
            'added': added[:MAX_LISTED_CHANGES],
            'removed': removed[:MAX_LISTED_CHANGES],
            'modified': modified[:MAX_LISTED_CHANGES],
            'counts': {
                'added': len(added),
                'removed': len(removed),
                'modified': len(modified),
            },
            'total_changes': len(added) + len(removed) + len(modified),
        }
        state['last_check'] = result
        cls._save_state(scope, state)
        return result

    @classmethod
    def accept(cls, scope: str) -> Dict:
        """Operator acknowledges changes → re-baseline the scope."""
        # Preserve any operator-supplied extra excludes across accepts by
        # re-baselining with the extras the previous baseline carried.
        state = cls._load_state(scope)
        options = None
        if state and state.get('excludes'):
            defaults = cls._scope_excludes(scope)
            extra = [e for e in state['excludes'] if e not in defaults]
            if extra:
                options = {'exclude': extra}
        return cls.baseline(scope, options=options)

    @classmethod
    def check_all(cls) -> Dict:
        """Check every baselined scope; notify admins when changes are found."""
        results = {}
        for scope in cls._baselined_scopes():
            try:
                result = cls.check(scope)
            except FileIntegrityScopeError as exc:
                results[scope] = {'error': str(exc)}
                continue
            except Exception as exc:  # never let one scope kill the sweep
                logger.warning('FIM check for %s failed: %s', scope, exc)
                results[scope] = {'error': str(exc)}
                continue
            results[scope] = result
            if result['total_changes'] > 0:
                sample = (result['added'] + result['removed']
                          + [m['path'] for m in result['modified']])[:NOTIFY_SAMPLE_CAP]
                cls._notify(scope, result['counts'], sample, result)
        return results

    # ------------------------------------------------------------------ #
    # Status (for the API/UI)
    # ------------------------------------------------------------------ #

    @classmethod
    def get_status(cls) -> Dict:
        """Scopes + baseline metadata + last results, for GET /security/fim."""
        optins = cls.get_app_optins()
        scopes = []

        app_names = {}
        if optins:
            try:
                from app.models.application import Application
                # Deliberately NOT query_active: this is only a label map, so a
                # stale opt-in still renders its name. Liveness is enforced in
                # _scope_roots, which leaves the scope unavailable.
                for application in Application.query.filter(Application.id.in_(optins)).all():
                    app_names[application.id] = application.name
            except Exception:
                pass  # status must degrade gracefully outside a DB context

        for scope in ['nginx', 'systemd'] + [f'app:{i}' for i in optins]:
            try:
                roots = cls._scope_roots(scope)
            except FileIntegrityScopeError:
                roots = []
            state = cls._load_state(scope)
            entry = {
                'scope': scope,
                'roots': roots,
                'available': bool(roots),
                'baseline': None,
                'last_check': None,
            }
            match = _APP_SCOPE_RE.match(scope)
            if match:
                app_id = int(match.group(1))
                entry['app_id'] = app_id
                entry['app_name'] = app_names.get(app_id)
            if state:
                entry['baseline'] = {
                    'created_at': state.get('created_at'),
                    'file_count': len(state.get('files') or {}),
                }
                entry['last_check'] = state.get('last_check')
            scopes.append(entry)

        return {'scopes': scopes, 'app_optins': optins}

    # ------------------------------------------------------------------ #
    # Notifications (best-effort)
    # ------------------------------------------------------------------ #

    @classmethod
    def _notify(cls, scope: str, counts: Dict, sample_paths: List[str],
                result: Optional[Dict] = None) -> None:
        try:
            from app.plugins_sdk import notify
            notify.send(
                FIM_EVENT_KEY,
                to='admins',
                data={
                    'scope': scope,
                    'counts': counts,
                    'sample_paths': sample_paths,
                    **cls._describe_changes(scope, counts, result or {}),
                },
                category='security',
            )
        except Exception as exc:  # never let a notification failure break a check
            logger.debug('notify %s failed: %s', FIM_EVENT_KEY, exc)

    @staticmethod
    def _describe_changes(scope: str, counts: Dict, result: Dict) -> Dict:
        """``summary`` + ``facts`` for the generic email/in-app template (fork).

        The event used to carry only scope/counts/sample_paths, which the
        generic template doesn't render, so the email said "changes detected"
        and nothing about which files.
        """
        where = {'nginx': 'nginx configuration', 'systemd': 'systemd units'}.get(scope)
        if where is None:
            match = _APP_SCOPE_RE.match(scope)
            where = f'app #{match.group(1)} files' if match else scope
        total = sum(counts.values())
        lines = ([f'added: {p}' for p in result.get('added', [])]
                 + [f'removed: {p}' for p in result.get('removed', [])]
                 + [f"modified ({', '.join(m['what'])}): {m['path']}" for m in result.get('modified', [])])
        shown = lines[:NOTIFY_SAMPLE_CAP]
        more = f' (+{total - len(shown)} more)' if total > len(shown) else ''
        summary = (f"{total} file{'s' if total != 1 else ''} changed in the {where} "
                   f"since the last accepted baseline: " + '; '.join(shown) + more
                   + '. If you made this change, accept it in Security → File integrity; '
                     'otherwise investigate.')
        facts = [{'label': 'Area', 'value': where}]
        for key in ('modified', 'added', 'removed'):
            if counts.get(key):
                facts.append({'label': key.capitalize(), 'value': str(counts[key])})
        if result.get('baseline_created_at'):
            facts.append({'label': 'Baseline from', 'value': result['baseline_created_at'][:16].replace('T', ' ') + ' UTC'})
        return {'summary': summary, 'facts': facts}

    # ------------------------------------------------------------------ #
    # Job registration
    # ------------------------------------------------------------------ #

    @classmethod
    def run_check_job(cls, job) -> Dict:
        """Job handler for the scheduled sweep (kind: security.fim.check)."""
        results = cls.check_all()
        return {
            'scopes_checked': len(results),
            'scopes_changed': sum(
                1 for r in results.values() if r.get('total_changes')
            ),
        }

    @classmethod
    def register_jobs(cls):
        """Register the FIM job handler + notification event.
        Called once at app startup (see app/__init__.py)."""
        from app.jobs import registry
        registry.register(FIM_JOB_KIND, cls.run_check_job, replace=True)
        try:
            from app.notifications import catalog
            catalog.register(
                FIM_EVENT_KEY,
                'File integrity changes detected',
                severity='warning',
                category='security',
            )
        except Exception as exc:
            logger.debug('FIM event registration failed: %s', exc)
