"""Delete-to-trash for the file manager, on top of the Recycle Bin.

Fork: arya-arav/ServerKit. Deleting from the file manager moves the item into
TRASH_DIR (root-only) and records it as a ``file`` in the Recycle Bin, which
already lists, restores, purges and expires tombstones after 30 days. The
hooks below are what make that real for files: restore moves the item back,
purge (or expiry) deletes it from the trash for good.
"""
import os
import shutil
import uuid

from app import db, paths
from app.models.trashed_file import TrashedFile
from app.services.file_service import FileService

# Beside the editor's pre-save copies: outside every file-manager root, so
# the trash can't be browsed, edited or deleted from the file manager itself.
TRASH_DIR = os.path.join(paths.SERVERKIT_BACKUP_DIR, 'file-trash')


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)


def trash(path, user_id=None):
    """Move path to the trash. Same filesystem: an instant rename."""
    FileService.require_deletable(path)
    path = os.path.abspath(path)
    os.makedirs(TRASH_DIR, mode=0o700, exist_ok=True)
    os.chmod(TRASH_DIR, 0o700)
    slot = os.path.join(TRASH_DIR, uuid.uuid4().hex)
    os.mkdir(slot, 0o700)
    dest = os.path.join(slot, os.path.basename(path.rstrip('/')) or 'item')
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    size = None if is_dir else os.lstat(path).st_size
    try:
        shutil.move(path, dest)
    except BaseException:
        shutil.rmtree(slot, ignore_errors=True)
        raise
    row = TrashedFile(original_path=path, trash_path=dest, is_dir=is_dir, size=size)
    row.soft_delete(user_id)
    db.session.add(row)
    db.session.commit()
    return {'path': path, 'trash_id': row.id}


def _pre_restore(row):
    if not os.path.lexists(row.trash_path):
        return 'the trashed copy is gone from the trash folder'
    if os.path.lexists(row.original_path):
        return f'something already exists at {row.original_path} -- move or rename it first'
    if not FileService.is_path_writable(row.original_path):
        return f'{row.original_path} is not writable from the file manager'
    return None


def _on_restore(row):
    FileService.make_parents(os.path.dirname(row.original_path))
    shutil.move(row.trash_path, row.original_path)
    shutil.rmtree(os.path.dirname(row.trash_path), ignore_errors=True)
    # Back where it was: nothing left to keep in the bin.
    db.session.delete(row)
    db.session.commit()


def _on_purge(row):
    _remove(row.trash_path)
    slot = os.path.dirname(row.trash_path)
    if os.path.dirname(slot) == TRASH_DIR:
        shutil.rmtree(slot, ignore_errors=True)


def register():
    from app.services import recycle_bin_service
    recycle_bin_service.register(
        'file', TrashedFile, noun='file',
        label=lambda r: os.path.basename(r.original_path.rstrip('/')) or r.original_path,
        description=lambda r: r.original_path + ('/' if r.is_dir else ''),
        pre_restore=_pre_restore,
        on_restore=_on_restore,
        on_purge=_on_purge,
    )

