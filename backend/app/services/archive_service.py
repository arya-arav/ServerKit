"""Compress, extract and zip-for-download for the file manager.

Fork: arya-arav/ServerKit. Archives can be large, so the work runs as a job
on the Queue Bus (a request through Cloudflare is cut off after ~100 s); the
routes validate up front and the page polls /files/jobs/<id>.

Extraction treats every archive as hostile:
- an entry may only land inside the destination ("zip slip": absolute paths,
  drive letters and ".." are refused, and the resolved path is re-checked)
- symlinks, hardlinks, devices and FIFOs are skipped, never created
- total size and entry count are capped, a zip entry with an absurd
  compression ratio is refused, written bytes are counted rather than
  trusting the header, and free disk space is checked first
- permissions keep only rwx bits (no setuid/setgid/sticky); owners come from
  the destination folder, never from the archive
- nothing is overwritten unless asked: a first run reports the conflicts
"""
import os
import shutil
import stat
import tarfile
import time
import zipfile

from app.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.services.file_service import FileService

COMPRESS_JOB_KIND = 'files.archive.compress'
EXTRACT_JOB_KIND = 'files.archive.extract'
DOWNLOAD_JOB_KIND = 'files.archive.download'
JOB_KINDS = (COMPRESS_JOB_KIND, EXTRACT_JOB_KIND, DOWNLOAD_JOB_KIND)

FORMATS = {'zip': '.zip', 'tar.gz': '.tar.gz', 'tar': '.tar'}
_TAR_SUFFIXES = ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')

MAX_EXTRACT_BYTES = 20 * 1024 ** 3      # uncompressed total
MAX_ENTRIES = 200_000
MAX_RATIO = 1000                        # per zip entry, for entries over RATIO_MIN_BYTES
RATIO_MIN_BYTES = 64 * 1024 ** 2
DISK_HEADROOM = 256 * 1024 ** 2

# Zips built for download live here until fetched (or for DOWNLOAD_TTL).
DOWNLOAD_DIR = '/var/tmp/serverkit-downloads'
DOWNLOAD_TTL = 3600

_COPY_CHUNK = 1024 * 1024


def archive_kind(path):
    """'zip', 'tar' or None, from the file name."""
    name = path.lower()
    if name.endswith('.zip'):
        return 'zip'
    if name.endswith(_TAR_SUFFIXES):
        return 'tar'
    return None


def archive_stem(path):
    """The name without its archive extension: site.tar.gz -> site."""
    name = os.path.basename(path)
    lower = name.lower()
    for suffix in ('.zip',) + _TAR_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)] or name
    return name


def _safe_member_path(dest_real, name):
    """The absolute target for an archive entry, or None when it would land
    outside dest (absolute path, drive letter, '..', or a resolved escape).
    An entry for the archive's own root ("./") resolves to dest itself."""
    name = name.replace('\\', '/')
    if not name or name.startswith('/') or (len(name) > 1 and name[1] == ':'):
        return None
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts:
        return dest_real
    if any(p == '..' for p in parts):
        return None
    target = os.path.realpath(os.path.join(dest_real, *parts))
    if target != dest_real and not target.startswith(dest_real + os.sep):
        return None
    return target


def _free_bytes(path):
    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def _tree_size(paths, skip=()):
    total = 0
    for path in paths:
        if os.path.islink(path) or not os.path.isdir(path):
            total += os.lstat(path).st_size
            continue
        for root, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                if full not in skip:
                    try:
                        total += os.lstat(full).st_size
                    except OSError:
                        pass
    return total


class ArchiveService:

    # ── validation (runs in the request) ─────────────────────────────

    @staticmethod
    def check_sources(paths):
        if not isinstance(paths, list) or not paths:
            raise ValidationError('Choose at least one file or folder')
        clean = []
        for path in paths:
            if not isinstance(path, str) or not path:
                raise ValidationError('Invalid path')
            if not FileService.is_path_allowed(path):
                raise PermissionDeniedError(FileService._DENIED)
            if not os.path.lexists(path):
                raise NotFoundError(f'Not found: {os.path.basename(path)}')
            clean.append(os.path.abspath(path))
        return clean

    @classmethod
    def check_compress(cls, paths, dest, fmt):
        sources = cls.check_sources(paths)
        if fmt not in FORMATS:
            raise ValidationError(f'Unsupported format: {fmt}')
        if not isinstance(dest, str) or not dest.endswith(FORMATS[fmt]):
            raise ValidationError(f'The archive name must end in {FORMATS[fmt]}')
        FileService._require_writable(dest)
        if os.path.lexists(dest):
            raise ConflictError('A file with that name already exists', code='file_exists')
        return sources

    @staticmethod
    def check_extract(path, dest):
        if not isinstance(path, str) or not FileService.is_path_allowed(path):
            raise PermissionDeniedError(FileService._DENIED)
        if not os.path.isfile(path):
            raise NotFoundError('Archive not found')
        if archive_kind(path) is None:
            raise ValidationError('Not a supported archive (zip, tar, tar.gz, tar.bz2, tar.xz)')
        if not isinstance(dest, str) or not dest:
            raise ValidationError('Choose where to extract to')
        FileService._require_writable(dest)
        if os.path.exists(dest) and not os.path.isdir(dest):
            raise ConflictError('The destination is a file', code='dest_not_folder')

    # ── compress ─────────────────────────────────────────────────────

    @classmethod
    def compress(cls, paths, dest, fmt, own=True):
        """Write paths into a new archive at dest. Returns a summary."""
        sources = [os.path.abspath(p) for p in paths]
        size = _tree_size(sources)
        if _free_bytes(os.path.dirname(dest)) < size + DISK_HEADROOM:
            raise ValidationError('Not enough free disk space for the archive')

        tmp = os.path.join(os.path.dirname(dest), f'.{os.path.basename(dest)}.part')
        skip = {os.path.realpath(dest), os.path.realpath(tmp)}
        files = skipped = 0
        try:
            if fmt == 'zip':
                with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                    for src in sources:
                        base = os.path.dirname(src)
                        for full, is_dir in cls._walk(src):
                            if os.path.realpath(full) in skip:
                                continue
                            if os.path.islink(full):
                                skipped += 1  # zip has no portable symlink entry
                                continue
                            arc = os.path.relpath(full, base).replace(os.sep, '/')
                            if is_dir:
                                zf.write(full, arc + '/')
                            else:
                                zf.write(full, arc)
                                files += 1
            else:
                mode = 'w:gz' if fmt == 'tar.gz' else 'w'

                def keep(info):
                    nonlocal files
                    if info.isfile():
                        files += 1
                    return info

                with tarfile.open(tmp, mode) as tf:
                    for src in sources:
                        tf.add(src, arcname=os.path.basename(src), recursive=True,
                               filter=lambda info, s=src: cls._tar_keep(info, s, skip, keep))
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        if own:
            FileService.own_like_parent(dest)
        return {'path': dest, 'files': files, 'skipped': skipped, 'bytes': os.path.getsize(dest)}

    @staticmethod
    def _walk(src):
        """(path, is_dir) for src and, for a real folder, everything inside;
        symlinked folders are listed but not followed."""
        if os.path.islink(src) or not os.path.isdir(src):
            yield src, False
            return
        yield src, True
        for root, dirs, files in os.walk(src):
            for name in sorted(dirs):
                full = os.path.join(root, name)
                yield full, not os.path.islink(full)
            for name in sorted(files):
                yield os.path.join(root, name), False

    @staticmethod
    def _tar_keep(info, src, skip, keep):
        full = os.path.join(os.path.dirname(src), info.name)
        if os.path.realpath(full) in skip:
            return None
        return keep(info)

    # ── extract ──────────────────────────────────────────────────────

    @classmethod
    def extract(cls, path, dest, overwrite=False):
        """Extract path into dest. With conflicts and no overwrite, nothing is
        written and the result lists them."""
        kind = archive_kind(path)
        dest_real = os.path.realpath(dest)
        entries = cls._plan(path, kind, dest_real)

        total = sum(e['size'] for e in entries)
        if total > MAX_EXTRACT_BYTES:
            raise ValidationError('The archive is too large to extract here')
        if _free_bytes(dest_real) < total + DISK_HEADROOM:
            raise ValidationError('Not enough free disk space to extract this archive')

        conflicts = [e['target'] for e in entries
                     if e['type'] == 'file' and os.path.lexists(e['target'])]
        dir_clash = [e['target'] for e in entries
                     if os.path.lexists(e['target']) and (
                         (e['type'] == 'file' and os.path.isdir(e['target']))
                         or (e['type'] == 'dir' and not os.path.isdir(e['target'])))]
        if dir_clash:
            raise ConflictError(
                f'"{os.path.relpath(dir_clash[0], dest_real)}" is a folder in one place and a file in the other',
                code='archive_type_clash')
        if conflicts and not overwrite:
            return {'conflicts': len(conflicts),
                    'examples': [os.path.relpath(c, dest_real) for c in conflicts[:5]]}

        FileService.make_parents(dest_real)
        written = 0
        if kind == 'zip':
            with zipfile.ZipFile(path) as zf:
                for e in entries:
                    written += cls._write_entry(e, lambda e=e: zf.open(e['member']))
        else:
            with tarfile.open(path, 'r:*') as tf:
                for e in entries:
                    written += cls._write_entry(e, lambda e=e: tf.extractfile(e['member']))
        skipped = sum(1 for e in entries if e['type'] == 'skip')
        return {'dest': dest_real, 'files': sum(1 for e in entries if e['type'] == 'file'),
                'skipped': skipped, 'bytes': written, 'replaced': len(conflicts)}

    @classmethod
    def _plan(cls, path, kind, dest_real):
        """Every entry, checked, before anything is written."""
        entries = []
        if kind == 'zip':
            try:
                zf = zipfile.ZipFile(path)
            except zipfile.BadZipFile as exc:
                raise ValidationError('This zip file is damaged or not a zip') from exc
            with zf:
                infos = zf.infolist()
                if len(infos) > MAX_ENTRIES:
                    raise ValidationError('The archive has too many entries')
                for info in infos:
                    mode = (info.external_attr >> 16) & 0o177777
                    target = _safe_member_path(dest_real, info.filename)
                    if target is None:
                        raise ValidationError(f'Unsafe path in archive: {info.filename}')
                    if target == dest_real:
                        continue
                    if stat.S_ISLNK(mode):
                        entries.append({'type': 'skip', 'target': target})
                        continue
                    if (info.file_size > RATIO_MIN_BYTES and info.compress_size
                            and info.file_size / info.compress_size > MAX_RATIO):
                        raise ValidationError('The archive looks like a zip bomb')
                    is_dir = info.is_dir()
                    entries.append({'type': 'dir' if is_dir else 'file', 'target': target,
                                    'member': info, 'size': 0 if is_dir else info.file_size,
                                    'mode': mode & 0o777})
        else:
            try:
                tf = tarfile.open(path, 'r:*')
            except (tarfile.TarError, OSError, EOFError) as exc:
                raise ValidationError('This archive is damaged or not a tar file') from exc
            with tf:
                count = 0
                for member in tf:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ValidationError('The archive has too many entries')
                    target = _safe_member_path(dest_real, member.name)
                    if target is None:
                        raise ValidationError(f'Unsafe path in archive: {member.name}')
                    if target == dest_real:
                        continue
                    if member.isdir():
                        entries.append({'type': 'dir', 'target': target, 'member': member,
                                        'size': 0, 'mode': member.mode & 0o777})
                    elif member.isfile():
                        entries.append({'type': 'file', 'target': target, 'member': member,
                                        'size': member.size, 'mode': member.mode & 0o777})
                    else:
                        entries.append({'type': 'skip', 'target': target})
        for e in entries:
            e.setdefault('size', 0)
        return entries

    @staticmethod
    def _write_entry(entry, open_member):
        """Write one entry; returns bytes written. Never writes more than the
        entry declared, whatever the stream holds."""
        target = entry['target']
        if entry['type'] == 'skip':
            return 0
        if entry['type'] == 'dir':
            if not os.path.isdir(target):
                FileService.make_parents(target)
                if entry.get('mode'):
                    os.chmod(target, entry['mode'] | 0o700)
            return 0
        FileService.make_parents(os.path.dirname(target))
        is_new = not os.path.lexists(target)
        if os.path.islink(target):
            os.remove(target)  # never write through a link already on disk
            is_new = True
        written = 0
        src = open_member()
        if src is None:
            return 0
        with src, open(target, 'wb') as out:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > entry['size']:
                    raise ValidationError('The archive holds more data than it declares')
                out.write(chunk)
        os.chmod(target, (entry.get('mode') or 0o644) & 0o777)
        if is_new:
            FileService.own_like_parent(target)
        return written

    # ── zip for download ─────────────────────────────────────────────

    @classmethod
    def build_download(cls, paths, job_id):
        """Zip paths into the download area; the job result names the file."""
        cls.cleanup_downloads()
        os.makedirs(DOWNLOAD_DIR, mode=0o700, exist_ok=True)
        os.chmod(DOWNLOAD_DIR, 0o700)
        dest = os.path.join(DOWNLOAD_DIR, f'{job_id}.zip')
        result = cls.compress(paths, dest, 'zip', own=False)
        os.chmod(dest, 0o600)
        name = (os.path.basename(paths[0].rstrip('/')) if len(paths) == 1 else 'files') or 'files'
        return {'download_name': f'{name}.zip', 'files': result['files'],
                'skipped': result['skipped'], 'bytes': result['bytes']}

    @staticmethod
    def download_path(job_id):
        path = os.path.join(DOWNLOAD_DIR, f'{job_id}.zip')
        return path if os.path.isfile(path) else None

    @staticmethod
    def cleanup_downloads(now=None):
        now = now or time.time()
        if not os.path.isdir(DOWNLOAD_DIR):
            return
        for name in os.listdir(DOWNLOAD_DIR):
            full = os.path.join(DOWNLOAD_DIR, name)
            try:
                if now - os.path.getmtime(full) > DOWNLOAD_TTL:
                    os.remove(full)
            except OSError:
                pass

    # ── jobs ─────────────────────────────────────────────────────────

    @classmethod
    def run_job(cls, job):
        payload = job.get_payload()
        if job.kind == COMPRESS_JOB_KIND:
            return cls.compress(payload['paths'], payload['dest'], payload['format'])
        if job.kind == EXTRACT_JOB_KIND:
            return cls.extract(payload['path'], payload['dest'], bool(payload.get('overwrite')))
        if job.kind == DOWNLOAD_JOB_KIND:
            return cls.build_download(payload['paths'], job.id)
        raise ValidationError(f'Unknown archive job: {job.kind}')

    @classmethod
    def register_jobs(cls):
        from app.jobs import registry
        for kind in JOB_KINDS:
            registry.register(kind, cls.run_job, replace=True)
