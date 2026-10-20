"""The template catalog: bundled, local, extension and remote-repository templates, their caches, and repository management.

Mixed into TemplateService; callers keep using TemplateService."""
import copy
import hashlib
import json
import os
import requests
import time
import yaml
from typing import Any, Dict, List, Optional
from datetime import datetime


class TemplateCatalogMixin:
    @classmethod
    def extension_dirs(cls) -> List[str]:
        """Where extension YAMLs are looked for, highest precedence first.

        The synced/operator directory shadows the bundled one, exactly like
        :meth:`list_local_templates` does for app templates.
        """
        return [os.path.join(cls.TEMPLATES_DIR, cls.EXTENSIONS_SUBDIR),
                os.path.join(cls.LOCAL_TEMPLATES_DIR, cls.EXTENSIONS_SUBDIR)]

    @classmethod
    def validate_extension(cls, document: Dict) -> Dict:
        """Validate an extension document (the whole file, not just the block)."""
        errors = []
        if not isinstance(document, dict):
            return {'valid': False, 'errors': ['extension file must be a mapping']}
        for field in ('name', 'version', 'description'):
            if not document.get(field):
                errors.append(f'Missing required field: {field}')
        if 'extension' not in document:
            errors.append("Missing required 'extension' block")
        else:
            errors.extend(cls._extension_block_errors(document['extension']))
        if 'compose' in document or 'dockerfile' in document:
            errors.append('An extension installs into a running engine and must '
                          'not declare a compose/dockerfile of its own')
        if errors:
            return {'valid': False, 'errors': errors}
        return {'valid': True, 'errors': []}

    @classmethod
    def _extension_block_errors(cls, extension: Any) -> List[str]:
        """Hard errors in an ``extension:`` block (shape only, never opinions)."""
        if not isinstance(extension, dict):
            return ["'extension' must be a mapping"]
        errors = []
        protocol = extension.get('protocol')
        if not protocol:
            errors.append('extension.protocol is required (which engines can host it)')
        elif protocol not in cls.ENGINE_PROTOCOLS:
            errors.append(
                f"extension.protocol '{protocol}' is not one of: "
                f"{', '.join(cls.ENGINE_PROTOCOLS)}")
        elif protocol == 'none':
            errors.append("extension.protocol 'none' cannot host an extension: "
                          'there is no client to execute the statement with')
        statement = extension.get('statement')
        if not statement or not isinstance(statement, str) or not statement.strip():
            errors.append('extension.statement is required and must be a non-empty string')
        for key in ('available_query', 'installed_query', 'image_hint', 'size', 'unit'):
            value = extension.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f'extension.{key} must be a string')
        for key in ('images', 'templates', 'versions'):
            value = extension.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f'extension.{key} must be a list of strings')
        return errors

    @classmethod
    def extension_metadata(cls, document: Dict, extension_id: str = None) -> Optional[Dict]:
        """Normalized extension record, or ``None`` when the document is not one.

        Everything a consumer needs is read off the YAML; nothing about any
        particular extension is hardcoded here.
        """
        if not isinstance(document, dict) or not isinstance(document.get('extension'), dict):
            return None
        block = document['extension']
        versions = block.get('versions')
        if not isinstance(versions, list) or not versions:
            versions = [document.get('version')] if document.get('version') else []
        images = [i for i in (block.get('images') or []) if isinstance(i, str)]
        return {
            'id': extension_id or document.get('id'),
            'name': document.get('name'),
            'version': document.get('version'),
            'description': document.get('description'),
            'icon': document.get('icon'),
            'website': document.get('website'),
            'documentation': document.get('documentation'),
            'family': cls.EXTENSION_FAMILY,
            'protocol': block.get('protocol'),
            'statement': block.get('statement'),
            'available_query': block.get('available_query'),
            'installed_query': block.get('installed_query'),
            # [] means "any engine template speaking `protocol`".
            'templates': [t for t in (block.get('templates') or []) if isinstance(t, str)],
            'images': images,
            'image_hint': block.get('image_hint'),
            # An extension with no declared images makes no image claim, so it
            # is never blocked on one.
            'requires_image': bool(images),
            'size': block.get('size'),
            'unit': block.get('unit'),
            'versions': [str(v) for v in versions],
        }

    @classmethod
    def list_extension_templates(cls) -> List[Dict]:
        """Every extension declared under a ``templates/extensions/`` directory.

        Derived from the files, like the engine catalog: dropping a YAML in
        makes the extension appear, with no code change anywhere.
        """
        found: List[Dict] = []
        seen = set()
        for directory in cls.extension_dirs():
            if not os.path.isdir(directory):
                continue
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith(('.yaml', '.yml')):
                    continue
                extension_id = filename.rsplit('.', 1)[0]
                if extension_id in seen:
                    continue
                record = cls._load_extension_file(os.path.join(directory, filename),
                                                  extension_id)
                if record:
                    seen.add(extension_id)
                    found.append(record)
        return found

    @classmethod
    def _load_extension_file(cls, path: str, extension_id: str) -> Optional[Dict]:
        """Parse + validate one extension file, or ``None`` (logged) if broken."""
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                document = yaml.safe_load(fh)
        except Exception:
            return None
        validation = cls.validate_extension(document if isinstance(document, dict) else {})
        if not validation['valid']:
            return None
        return cls.extension_metadata(document, extension_id=extension_id)

    @classmethod
    def get_extension_template(cls, extension_id: str) -> Optional[Dict]:
        """One normalized extension by id, or ``None``."""
        if not extension_id:
            return None
        for record in cls.list_extension_templates():
            if record['id'] == extension_id:
                return record
        return None

    @classmethod
    def list_local_templates(cls) -> List[Dict]:
        """List locally available templates.

        Deliberately NOT provider-gated: this is the raw "what is bundled on
        disk" view, which ``build_repo_index`` needs to describe the bundle
        faithfully for publishing. Provider-owned templates are hidden from
        the *catalog* instead — :meth:`list_all_templates` and
        :meth:`get_template` apply the availability gate, so an
        absent extension means hidden cards and refused installs even though
        the YAML remains bundled.

        Parsing is memoized per file (see the cache block near the top of the
        class); a repeat listing stats each file instead of re-parsing it.
        """
        templates = []
        seen_ids = set()
        seen_paths = set()

        for templates_dir in [cls.TEMPLATES_DIR, cls.LOCAL_TEMPLATES_DIR]:
            if not os.path.isdir(templates_dir):
                continue

            try:
                with os.scandir(templates_dir) as scan:
                    # Sorted so a same-id collision (foo.yaml vs foo.yml)
                    # resolves the same way on every platform; os.listdir order
                    # is filesystem-dependent.
                    dir_entries = sorted(scan, key=lambda e: e.name)
            except OSError:
                continue

            for dir_entry in dir_entries:
                if not dir_entry.name.endswith(('.yaml', '.yml')):
                    continue
                seen_paths.add(dir_entry.path)

                template_id = dir_entry.name.rsplit('.', 1)[0]
                if template_id in seen_ids:
                    continue
                try:
                    # DirEntry.stat() reuses the data the directory scan
                    # already returned where the platform provides it, so this
                    # is cheaper than a separate os.stat per file.
                    info = dir_entry.stat()
                except OSError:
                    continue

                entry = cls._local_entry(dir_entry.path, template_id,
                                         (info.st_mtime_ns, info.st_size))
                if entry is not None:
                    # A file that fails to parse deliberately does NOT claim
                    # the id, so a valid file of the same name in the fallback
                    # directory can still supply it.
                    seen_ids.add(template_id)
                    templates.append(entry)

        cls._prune_local_cache(seen_paths)
        return templates

    @classmethod
    def invalidate_local_cache(cls) -> None:
        """Drop the parsed-template cache.

        Ordinary writes do not need this -- the cache is keyed on
        (mtime_ns, size), so syncing or editing a template invalidates exactly
        that file on the next listing. Provided for tests and any out-of-band
        mutation that could defeat mtime comparison."""
        with cls._local_cache_lock:
            cls._local_cache = {}

    @classmethod
    def _prune_local_cache(cls, seen_paths: set) -> None:
        """Forget files that are no longer on disk, so a long-running panel
        that churns templates does not grow the cache without bound."""
        with cls._local_cache_lock:
            stale = [p for p in cls._local_cache if p not in seen_paths]
            for path in stale:
                del cls._local_cache[path]

    @classmethod
    def _local_entry(cls, filepath: str, template_id: str,
                     stat_key: tuple) -> Optional[Dict]:
        """Catalog projection for one bundled template, memoized on ``stat_key``.

        Returns None when the file does not parse -- and caches that negative
        result too, so one malformed YAML does not cost a parse on every
        render. Returns a deep copy so a caller mutating the result (or its
        nested ``repo``/``engine``/``categories``) cannot corrupt the cache."""
        with cls._local_cache_lock:
            cached = cls._local_cache.get(filepath)
            if cached is not None and cached['key'] == stat_key:
                return copy.deepcopy(cached['entry'])

        entry = cls._parse_local_entry(filepath, template_id)

        with cls._local_cache_lock:
            cls._local_cache[filepath] = {'key': stat_key, 'entry': entry}
        return copy.deepcopy(entry)

    @classmethod
    def _parse_local_entry(cls, filepath: str, template_id: str) -> Optional[Dict]:
        """Parse one template file into its catalog projection, or None."""
        result = cls.parse_template(filepath)
        if not result.get('success'):
            return None

        template = result['template']
        kind = template.get('kind', 'compose')
        entry = {
            'id': template_id,
            'name': template.get('name'),
            'version': template.get('version'),
            'description': template.get('description'),
            'icon': template.get('icon'),
            'categories': template.get('categories', []),
            'featured': bool(template.get('featured')),
            'kind': kind,
            'source': 'local',
            'filepath': filepath
        }
        # Repo templates surface a minimal repo summary so the catalog grid
        # can badge them and link to the wizard without a second fetch.
        if kind == 'repo' and isinstance(template.get('repo'), dict):
            repo = template['repo']
            entry['repo'] = {
                'url': repo.get('url'),
                'branch': repo.get('branch', 'main'),
            }
        # A database engine declares itself with a top-level `engine:` block.
        # This listing is a fixed projection, so without carrying the block
        # through, the Databases catalog could never see an engine without
        # re-parsing every YAML file.
        engine = cls.engine_metadata(template)
        if engine:
            entry['engine'] = engine
        return entry

    @classmethod
    def list_engine_templates(cls) -> List[Dict]:
        """Every available template carrying an ``engine:`` block.

        This is what the Databases engine catalog renders. It is derived
        entirely from the template sources -- drop a new engine YAML into
        ``backend/templates/`` (or sync a repository that ships one) and it
        appears here with no code change.
        """
        return [t for t in cls.list_all_templates()
                if isinstance(t.get('engine'), dict)]

    @classmethod
    def build_repo_index(cls, repo_name: str = 'serverkit-official') -> Dict:
        """Build the ``index.json`` document that describes the locally-bundled
        templates as a publishable repository.

        This is the exact shape :meth:`fetch_remote_templates` / :meth:`sync_templates`
        consume from ``<repo_url>/index.json`` (templates served at
        ``<repo_url>/templates/<id>.yaml``). Publishing a repo is then just:
        host the ``templates/*.yaml`` files plus this ``index.json``. Lets
        Prompture Hub & friends ship template updates without a panel release.
        """
        templates = []
        for t in cls.list_local_templates():
            entry = {
                'id': t['id'],
                'name': t.get('name'),
                'version': t.get('version'),
                'description': t.get('description'),
                'icon': t.get('icon'),
                'categories': t.get('categories', []),
            }
            if t.get('featured'):
                entry['featured'] = True
            # Carried in the index so a panel that syncs this repo can list the
            # engine catalog without fetching all 100+ template files first.
            if isinstance(t.get('engine'), dict):
                entry['engine'] = t['engine']
            templates.append(entry)
        return {
            'name': repo_name,
            'schema_version': cls.SCHEMA_VERSION,
            'generated_at': datetime.now().isoformat(),
            'count': len(templates),
            'templates': templates,
        }

    @classmethod
    def export_repo_index(cls, dest_path: str = None) -> Dict:
        """Write :meth:`build_repo_index` to ``dest_path`` (defaults to
        ``index.json`` alongside the bundled templates). Returns a status dict."""
        index = cls.build_repo_index()
        path = dest_path or os.path.join(cls.LOCAL_TEMPLATES_DIR, 'index.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=2)
            return {'success': True, 'path': path, 'count': index['count']}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def invalidate_remote_cache(cls, repo_url: str = None) -> None:
        """Drop cached remote indexes -- all of them, or just ``repo_url``.

        Called whenever the repo list changes so a freshly added repo appears
        immediately instead of after the TTL, and a removed one stops being
        served from cache."""
        with cls._remote_cache_lock:
            if repo_url is None:
                cls._remote_cache = {}
            else:
                cls._remote_cache.pop(repo_url, None)

    @classmethod
    def _cached_remote(cls, repo_url: str) -> Optional[List[Dict]]:
        """Unexpired cache entry for ``repo_url``, or None. Returns copies so a
        caller mutating an entry cannot corrupt the cache."""
        with cls._remote_cache_lock:
            entry = cls._remote_cache.get(repo_url)
            if entry and entry['expires'] > time.monotonic():
                return [dict(t) for t in entry['templates']]
        return None

    @classmethod
    def _last_good_remote(cls, repo_url: str) -> Optional[List[Dict]]:
        """Last payload this repo successfully served, regardless of expiry."""
        with cls._remote_cache_lock:
            entry = cls._remote_cache.get(repo_url) or {}
            last_good = entry.get('last_good')
            return [dict(t) for t in last_good] if last_good else None

    @classmethod
    def _store_remote(cls, repo_url: str, templates: List[Dict], ok: bool) -> None:
        """Cache ``templates`` for ``repo_url``. A successful payload also
        becomes the last-good; a failure carries the previous last-good
        forward, so repeated failures keep serving it instead of decaying to
        an empty catalog on the second one."""
        ttl = cls._REMOTE_TTL_SECONDS if ok else cls._REMOTE_ERROR_TTL_SECONDS
        with cls._remote_cache_lock:
            previous = cls._remote_cache.get(repo_url) or {}
            cls._remote_cache[repo_url] = {
                'templates': [dict(t) for t in templates],
                'expires': time.monotonic() + ttl,
                'ok': ok,
                'last_good': ([dict(t) for t in templates] if ok
                              else previous.get('last_good')),
            }

    @classmethod
    def fetch_remote_templates(cls, repo_url: str, force: bool = False) -> List[Dict]:
        """Fetch a repository's template index, memoized (see the cache block
        near the top of the class). ``force=True`` bypasses the cache, which is
        what an explicit sync wants."""
        if not force:
            cached = cls._cached_remote(repo_url)
            if cached is not None:
                return cached

        templates = []
        try:
            index_url = f"{repo_url}/index.json"
            response = requests.get(index_url, timeout=cls._REMOTE_TIMEOUT_SECONDS)
            response.raise_for_status()

            index = response.json()
            for template_info in index.get('templates', []):
                template_info['source'] = 'remote'
                template_info['repo_url'] = repo_url
                templates.append(template_info)

        except Exception as e:
            print(f"Failed to fetch templates from {repo_url}: {e}")
            # Keep serving the last good index rather than blanking a catalog
            # the operator has already seen, but still back off so a dead repo
            # is not retried on every single page load.
            last_good = cls._last_good_remote(repo_url)
            cls._store_remote(repo_url, last_good or [], ok=False)
            return last_good or []

        cls._store_remote(repo_url, templates, ok=True)
        return [dict(t) for t in templates]

    @classmethod
    def list_all_templates(cls, category: str = None, search: str = None) -> List[Dict]:
        """List all available templates from all sources, deduped by id.

        Bundled and registry templates deliberately overlap -- the bundle is the
        offline floor and the registry is the growth path (docs/REGISTRIES.md) --
        so the same id legitimately arrives from both. Concatenating them listed
        every bundled template twice. **Local wins**, matching
        :meth:`get_template`, which already resolves local directories before
        any repo; among repos, the first enabled one to claim an id keeps it.
        """
        by_id = {}

        def _claim(entry):
            """First source to claim an id keeps it."""
            template_id = entry.get('id')
            if template_id and template_id not in by_id:
                by_id[template_id] = entry

        # Local templates first -- they outrank every repo.
        for entry in cls.list_local_templates():
            _claim(entry)

        # Remote templates fill in ids the bundle does not carry.
        config = cls.get_config()
        for repo in config.get('repos', []):
            if repo.get('enabled', True):
                for entry in cls.fetch_remote_templates(repo['url']):
                    _claim(entry)

        templates = list(by_id.values())

        # Filter by category
        if category:
            templates = [t for t in templates if category in t.get('categories', [])]

        # Provider-owned templates from any source (incl. synced remote repos)
        # are hidden while their extension is absent.
        templates = [t for t in templates if cls.template_available(t.get('id'))]

        # Search filter
        if search:
            search_lower = search.lower()
            templates = [
                t for t in templates
                if search_lower in t.get('name', '').lower()
                or search_lower in t.get('description', '').lower()
            ]

        return templates

    @classmethod
    def get_template(cls, template_id: str) -> Dict:
        """Get full template details."""
        if not cls.template_available(template_id):
            owner = cls.PROVIDER_OWNED_TEMPLATES.get(template_id)
            return {
                'success': False,
                'error': f"Template '{template_id}' is provided by the "
                         f"'{owner}' extension, which is not installed or not active",
            }
        # Check local directories (system dir, then bundled fallback)
        for templates_dir in [cls.TEMPLATES_DIR, cls.LOCAL_TEMPLATES_DIR]:
            for ext in ['.yaml', '.yml']:
                filepath = os.path.join(templates_dir, f"{template_id}{ext}")
                if os.path.exists(filepath):
                    result = cls.parse_template(filepath)
                    if result.get('success'):
                        template = result['template']
                        template['source'] = 'local'
                        template['filepath'] = filepath
                        return {'success': True, 'template': template}
                    return result

        # Check remote repos
        config = cls.get_config()
        for repo in config.get('repos', []):
            if not repo.get('enabled', True):
                continue

            try:
                url = f"{repo['url']}/templates/{template_id}.yaml"
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    template = yaml.safe_load(response.text)
                    validation = cls.validate_template(template)
                    if validation['valid']:
                        template['source'] = 'remote'
                        template['repo_url'] = repo['url']
                        return {'success': True, 'template': template}
            except Exception:
                continue

        return {'success': False, 'error': 'Template not found'}

    @classmethod
    def add_repository(cls, name: str, url: str) -> Dict:
        """Add a template repository."""
        config = cls.get_config()

        # Check if already exists
        for repo in config.get('repos', []):
            if repo['url'] == url:
                return {'success': False, 'error': 'Repository already exists'}

        config.setdefault('repos', []).append({
            'name': name,
            'url': url.rstrip('/'),
            'enabled': True,
            'added_at': datetime.now().isoformat()
        })

        return cls.save_config(config)

    @classmethod
    def remove_repository(cls, url: str) -> Dict:
        """Remove a template repository."""
        config = cls.get_config()
        config['repos'] = [r for r in config.get('repos', []) if r['url'] != url]
        return cls.save_config(config)

    @classmethod
    def list_repositories(cls) -> List[Dict]:
        """List configured template repositories."""
        config = cls.get_config()
        return config.get('repos', cls.DEFAULT_REPOS)

    @classmethod
    def sync_templates(cls) -> Dict:
        """Sync templates from all repositories."""
        os.makedirs(cls.TEMPLATES_DIR, exist_ok=True)

        config = cls.get_config()
        synced = 0
        unverified = 0  # saved, but the index pinned no sha256 for them
        errors = []

        for repo in config.get('repos', []):
            if not repo.get('enabled', True):
                continue

            try:
                # Fetch index
                index_url = f"{repo['url']}/index.json"
                response = requests.get(index_url, timeout=30)
                response.raise_for_status()

                index = response.json()

                # Download each template
                for template_info in index.get('templates', []):
                    template_id = template_info.get('id')
                    if not template_id:
                        continue

                    try:
                        template_url = f"{repo['url']}/templates/{template_id}.yaml"
                        response = requests.get(template_url, timeout=30)
                        response.raise_for_status()

                        # Verify against the checksum the index pinned for this
                        # entry. A template is a deploy definition -- images,
                        # ports, volumes, env -- so a swapped file is worth
                        # refusing outright, and the index already carries the
                        # hash for every official entry.
                        #
                        # Missing hash is allowed (third-party repos may not
                        # publish one) and counted, mirroring how extensions
                        # treat unsigned-vs-invalid: absent is a caveat, wrong
                        # is a hard stop.
                        expected = (template_info.get('sha256') or '').strip().lower()
                        if expected:
                            actual = hashlib.sha256(response.content).hexdigest()
                            if actual != expected:
                                errors.append(
                                    f"Checksum mismatch for {template_id}: index pinned "
                                    f"{expected[:12]}..., downloaded {actual[:12]}.... Not saved."
                                )
                                continue
                        else:
                            unverified += 1

                        # Written as bytes so what lands on disk is exactly the
                        # content that was hashed (text mode would rewrite line
                        # endings on Windows and no longer match).
                        filepath = os.path.join(cls.TEMPLATES_DIR, f"{template_id}.yaml")
                        with open(filepath, 'wb') as f:
                            f.write(response.content)

                        synced += 1
                    except Exception as e:
                        errors.append(f"Failed to sync {template_id}: {e}")

            except Exception as e:
                errors.append(f"Failed to sync from {repo['name']}: {e}")

        config['last_sync'] = datetime.now().isoformat()
        cls.save_config(config)

        return {
            'success': True,
            'synced': synced,
            'unverified': unverified,
            'errors': errors if errors else None
        }

    @classmethod
    def get_categories(cls) -> List[str]:
        """Get all available template categories."""
        templates = cls.list_all_templates()
        categories = set()
        for template in templates:
            categories.update(template.get('categories', []))
        return sorted(categories)

    @classmethod
    def create_local_template(cls, template_data: Dict) -> Dict:
        """Create a local template."""
        validation = cls.validate_template(template_data)
        if not validation['valid']:
            return {'success': False, 'errors': validation['errors']}

        os.makedirs(cls.TEMPLATES_DIR, exist_ok=True)

        template_id = template_data['name'].lower().replace(' ', '-')
        filepath = os.path.join(cls.TEMPLATES_DIR, f"{template_id}.yaml")

        if os.path.exists(filepath):
            return {'success': False, 'error': 'Template with this name already exists'}

        try:
            with open(filepath, 'w') as f:
                yaml.dump(template_data, f, default_flow_style=False, sort_keys=False)

            return {'success': True, 'template_id': template_id, 'filepath': filepath}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def delete_local_template(cls, template_id: str) -> Dict:
        """Delete a local template."""
        for ext in ['.yaml', '.yml']:
            filepath = os.path.join(cls.TEMPLATES_DIR, f"{template_id}{ext}")
            if os.path.exists(filepath):
                os.remove(filepath)
                return {'success': True}

        return {'success': False, 'error': 'Template not found'}
