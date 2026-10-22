"""Tier-1 backup verification.

Every ``backup.policy.run`` writes a ``manifest.json`` next to its archives and
then runs a cheap, always-on verification pass over the produced archive(s):

  * **manifest** (:func:`write_manifest`) — file list, per-artifact sha256,
    sizes, chain refs; the sha256 of the *primary* archive is denormalized onto
    the :class:`BackupRun` row (``checksum_sha256``) so later offsite checks can
    compare against a STORED hash rather than one recomputed from the same
    possibly-corrupt local file (Decision 3).
  * **verifier** (:func:`verify_run_tier1`) — a full ``tar -t`` style listing
    (readability) plus a sha256 spot-check of the primary archive against the
    stored value, promoting the run up the ladder
    ``none → listed → hashed``. Only a real restore drill ever reaches
    ``drilled``.

Everything here is cross-platform: the listing falls back to Python's
``tarfile`` where GNU ``tar`` is unavailable (e.g. Windows dev), and the hash is
computed in Python.
"""
import gzip
import hashlib
import json
import logging
import os
import tarfile
from datetime import datetime

logger = logging.getLogger(__name__)

# Bump when the manifest shape changes in a backward-incompatible way.
MANIFEST_VERSION = 1
MANIFEST_NAME = 'manifest.json'

_HASH_CHUNK = 1 << 20  # 1 MiB


# --------------------------------------------------------------------------- #
# Hashing helpers
# --------------------------------------------------------------------------- #

def sha256_file(path):
    """Return the hex sha256 of a file, or ``None`` if it is missing/unreadable."""
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(_HASH_CHUNK), b''):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _base_dir(run, meta):
    """Directory that holds a run's artifacts (where manifest.json lives)."""
    primary = meta.get('primary_archive')
    if primary:
        return os.path.dirname(primary) or (run.storage_path or '.')
    sp = run.storage_path
    if sp and os.path.isdir(sp):
        return sp
    if sp:
        return os.path.dirname(sp) or '.'
    return '.'


# --------------------------------------------------------------------------- #
# Manifest writer
# --------------------------------------------------------------------------- #

def write_manifest(run, meta):
    """Write ``manifest.json`` next to a run's archives and denormalize the
    primary archive's sha256 onto the run row.

    Returns the manifest dict. Does NOT commit — the caller owns the session.
    """
    base = _base_dir(run, meta)
    primary = meta.get('primary_archive')
    primary_name = os.path.basename(primary) if primary else None
    # Only a run that owns a directory lists it. A single-file run (a database
    # dump, a files path-list archive) lives in a folder shared with every other
    # run, so listing it would hash all of them and clobber one manifest.json;
    # for those the primary's hash is recorded on the run row only.
    own_dir = bool(run.storage_path) and os.path.isdir(run.storage_path)

    artifacts = []
    if own_dir and os.path.isdir(base):
        for entry in sorted(os.listdir(base)):
            if entry == MANIFEST_NAME:
                continue
            full = os.path.join(base, entry)
            if not os.path.isfile(full):
                continue
            artifacts.append({
                'name': entry,
                'size': os.path.getsize(full),
                'sha256': sha256_file(full),
            })
    elif primary and os.path.isfile(primary):
        # storage_path is a single file (e.g. a database dump).
        artifacts.append({
            'name': primary_name,
            'size': os.path.getsize(primary),
            'sha256': sha256_file(primary),
        })

    primary_sha256 = None
    for art in artifacts:
        if art['name'] == primary_name:
            primary_sha256 = art['sha256']
            break
    if primary_sha256 is None and primary and os.path.isfile(primary):
        primary_sha256 = sha256_file(primary)

    manifest = {
        'version': MANIFEST_VERSION,
        'run_id': run.id,
        'kind': meta.get('kind') or run.kind,
        'engine': meta.get('engine'),
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'primary_archive': primary_name,
        'primary_sha256': primary_sha256,
        'artifacts': artifacts,
        'totals': {
            'count': len(artifacts),
            'bytes': sum(a['size'] for a in artifacts),
        },
        'chain': {
            'incremental': bool(meta.get('incremental')),
            'full_run_id': meta.get('full_run_id'),
        },
        'tool_versions': _tool_versions(),
    }

    # Denormalize the primary hash onto the run (Decision 3).
    if primary_sha256:
        run.checksum_sha256 = primary_sha256

    if own_dir and os.path.isdir(base):
        try:
            with open(os.path.join(base, MANIFEST_NAME), 'w', encoding='utf-8') as fh:
                json.dump(manifest, fh, indent=2)
        except OSError as exc:  # best-effort — the row still carries the hash
            logger.warning('could not write manifest for run %s: %s', run.id, exc)

    return manifest


def read_manifest(run, meta=None):
    """Load a run's on-disk manifest, or ``None`` if absent/unreadable."""
    meta = meta if meta is not None else (run.get_metadata() or {})
    base = _base_dir(run, meta)
    path = os.path.join(base, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _tool_versions():
    versions = {'python_tarfile': True}
    try:
        from app.utils.system import is_command_available
        versions['gnu_tar'] = bool(is_command_available('tar'))
    except Exception:  # noqa: BLE001
        pass
    return versions


# --------------------------------------------------------------------------- #
# Post-run verifier (Tier 1)
# --------------------------------------------------------------------------- #

def _list_archive(path):
    """Return ``(ok, member_count, error)`` for a backup artifact, using
    Python's ``tarfile``/``gzip`` so it works cross-platform.

    Tar archives (any compression) list their members. A gzip file that is not a
    tar — the shape of every database backup (``*.sql.gz``) — is read through
    to the end instead and counts as one member; opening it as a tar used to
    report every database dump as unreadable."""
    try:
        with tarfile.open(path, 'r:*') as tar:
            members = tar.getmembers()
        return True, len(members), None
    except Exception as exc:  # noqa: BLE001 — any read failure means unreadable
        if not _is_plain_gzip(path):
            return False, 0, str(exc)
    return _read_gzip_through(path)


_GZIP_MAGIC = b'\x1f\x8b'


def _is_plain_gzip(path):
    """True for a gzip file whose content is not a tar archive (a broken
    .tar.gz must keep failing as a tar, not pass as a gzip'd blob)."""
    try:
        with open(path, 'rb') as fh:
            if fh.read(2) != _GZIP_MAGIC:
                return False
        with gzip.open(path, 'rb') as gz:
            head = gz.read(512)
    except (OSError, EOFError):
        return True  # gzip framing is there; _read_gzip_through reports the damage
    return head[257:262] != b'ustar'


def _read_gzip_through(path):
    """Decompress a gzip file to the end; any CRC/length/truncation error makes
    it unreadable."""
    try:
        with gzip.open(path, 'rb') as gz:
            while gz.read(_HASH_CHUNK):
                pass
        return True, 1, None
    except (OSError, EOFError) as exc:  # zlib.error is an OSError subclass here
        return False, 0, f'gzip stream is corrupt: {exc}'


def _stored_sha256(run, meta):
    if getattr(run, 'checksum_sha256', None):
        return run.checksum_sha256
    manifest = read_manifest(run, meta)
    if manifest and manifest.get('primary_sha256'):
        return manifest['primary_sha256']
    return (meta or {}).get('primary_sha256')


def verify_run_tier1(run, meta):
    """Verify a freshly produced run and set its verify ladder position.

    Ladder: ``none`` (archive unreadable) → ``listed`` (readable but checksum
    unverified/mismatch) → ``hashed`` (checksum matches the stored manifest).

    Mutates ``run.verify_level`` / ``run.verify_error`` / ``run.verified_at``
    (does NOT commit) and returns a probes dict.
    """
    primary = meta.get('primary_archive')
    probes = {'listed': [], 'hashed': False, 'sha256': None}
    run.verified_at = datetime.utcnow()

    if not primary or not os.path.exists(primary):
        run.verify_level = 'none'
        run.verify_error = 'Primary archive not found on disk (not readable).'
        probes['listed'].append({'name': os.path.basename(primary) if primary else None,
                                  'ok': False, 'error': 'missing'})
        return probes

    ok, member_count, err = _list_archive(primary)
    probes['listed'].append({
        'name': os.path.basename(primary), 'ok': ok,
        'members': member_count, 'error': err,
    })
    if not ok:
        run.verify_level = 'none'
        run.verify_error = f'Primary archive is not readable: {(err or "")[:200]}'
        return probes

    stored = _stored_sha256(run, meta)
    actual = sha256_file(primary)
    probes['sha256'] = actual

    if stored and actual and actual == stored:
        run.verify_level = 'hashed'
        run.verify_error = None
        probes['hashed'] = True
    else:
        run.verify_level = 'listed'
        probes['hashed'] = False
        if stored and actual and actual != stored:
            run.verify_error = (f'Checksum mismatch: expected {stored[:12]}…, '
                                f'got {actual[:12]}…')
        else:
            run.verify_error = 'No stored checksum available to compare against.'
    return probes
