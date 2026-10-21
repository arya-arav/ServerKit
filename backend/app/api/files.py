"""File Manager API endpoints for browsing, editing, and managing files."""

from flask import Blueprint, request, jsonify, send_file
from app.error_reporting import unexpected_response
from app.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError, permission_denied
from ..middleware.rbac import admin_required, permission_required
from ..services.file_service import FileService
from ..services.storage_provider_service import StorageProviderService
from ..services import archive_service
from ..services.archive_service import ArchiveService
from app.api._request import json_object
import os
import tempfile

files_bp = Blueprint('files', __name__)


@files_bp.route('/browse', methods=['GET'])
@permission_required('files', 'read')
def browse_directory():
    """List directory contents."""
    path = request.args.get('path', '/home')
    show_hidden = request.args.get('show_hidden', 'false').lower() == 'true'

    result = FileService.list_directory(path, show_hidden=show_hidden)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/info', methods=['GET'])
@permission_required('files', 'read')
def get_file_info():
    """Get information about a file or directory."""
    path = request.args.get('path')

    if not path:
        raise ValidationError('Path is required')

    if not FileService.is_path_allowed(path):
        raise PermissionDeniedError('Access denied')

    info = FileService.get_file_info(path)

    if not info or 'error' in info:
        raise NotFoundError(info.get('error', 'File not found') if info else 'File not found')
    return jsonify({'success': True, 'file': info}), 200


@files_bp.route('/read', methods=['GET'])
@permission_required('files', 'read')
def read_file():
    """Read file contents."""
    path = request.args.get('path')

    if not path:
        raise ValidationError('Path is required')

    result = FileService.read_file(path)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/write', methods=['POST'])
@permission_required('files', 'write')
def write_file():
    """Write content to a file."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    path = data.get('path')
    content = data.get('content')
    create_backup = data.get('create_backup', True)

    if not path:
        raise ValidationError('Path is required')

    if content is None:
        raise ValidationError('Content is required')

    result = FileService.write_file(path, content, create_backup=create_backup,
                                    expected_version=data.get('expected_version'))

    return jsonify({'success': True, **result}), 200


@files_bp.route('/create', methods=['POST'])
@permission_required('files', 'write')
def create_file():
    """Create a new file."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    path = data.get('path')
    content = data.get('content', '')

    if not path:
        raise ValidationError('Path is required')

    result = FileService.create_file(path, content)

    return jsonify({'success': True, **result}), 201


@files_bp.route('/mkdir', methods=['POST'])
@permission_required('files', 'write')
def create_directory():
    """Create a new directory."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    path = data.get('path')

    if not path:
        raise ValidationError('Path is required')

    result = FileService.create_directory(path)

    return jsonify({'success': True, **result}), 201


@files_bp.route('/delete', methods=['DELETE'])
@permission_required('files', 'write')
def delete_path():
    """Delete a file or directory."""
    path = request.args.get('path')

    if not path:
        raise ValidationError('Path is required')

    result = FileService.delete(path)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/trash', methods=['POST'])
@permission_required('files', 'write')
def trash_path():
    """Move a file or folder to the Recycle Bin (restorable for 30 days)."""
    from flask_jwt_extended import get_jwt_identity
    from ..services import file_trash_service
    path = json_object().get('path')
    if not path:
        raise ValidationError('Path is required')
    identity = get_jwt_identity()
    result = file_trash_service.trash(path, user_id=int(identity) if str(identity).isdigit() else None)
    return jsonify({'success': True, **result}), 200


@files_bp.route('/rename', methods=['POST'])
@permission_required('files', 'write')
def rename_path():
    """Rename a file or directory."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    path = data.get('path')
    new_name = data.get('new_name')

    if not path or not new_name:
        raise ValidationError('Path and new_name are required')

    result = FileService.rename(path, new_name)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/copy', methods=['POST'])
@permission_required('files', 'write')
def copy_path():
    """Copy a file or directory."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    src = data.get('src')
    dest = data.get('dest')

    if not src or not dest:
        raise ValidationError('Source and destination paths are required')

    result = FileService.copy(src, dest)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/move', methods=['POST'])
@permission_required('files', 'write')
def move_path():
    """Move a file or directory."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    src = data.get('src')
    dest = data.get('dest')

    if not src or not dest:
        raise ValidationError('Source and destination paths are required')

    result = FileService.move(src, dest)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/chmod', methods=['POST'])
@permission_required('files', 'write')
def change_permissions():
    """Change file/directory permissions."""
    data = json_object()

    if not data:
        raise ValidationError('Request body is required')

    path = data.get('path')
    mode = data.get('mode')

    if not path or not mode:
        raise ValidationError('Path and mode are required')

    result = FileService.change_permissions(
        path, mode, recursive=bool(data.get('recursive')), file_mode=data.get('file_mode') or None)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/chown', methods=['POST'])
@admin_required
def change_owner():
    """Change a file or folder's owner and group (admins only)."""
    data = json_object()
    path, owner, group = data.get('path'), data.get('owner'), data.get('group')
    if not path or not owner or not group:
        raise ValidationError('Path, owner and group are required')
    result = FileService.change_owner(path, owner, group, recursive=bool(data.get('recursive')))
    return jsonify({'success': True, **result}), 200


@files_bp.route('/owners', methods=['GET'])
@admin_required
def list_owners():
    """Users and groups to offer in the change-owner dialog."""
    return jsonify(FileService.list_owners()), 200


@files_bp.route('/search', methods=['GET'])
@permission_required('files', 'read')
def search_files():
    """Search for files matching a pattern."""
    directory = request.args.get('directory', '/home')
    pattern = request.args.get('pattern')
    max_results = request.args.get('max_results', 100, type=int)

    if not pattern:
        raise ValidationError('Search pattern is required')

    result = FileService.search(directory, pattern, max_results=max_results)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/disk-usage', methods=['GET'])
@permission_required('files', 'read')
def get_disk_usage():
    """Get disk usage for a path."""
    path = request.args.get('path', '/')

    result = FileService.get_disk_usage(path)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/disk-mounts', methods=['GET'])
@permission_required('files', 'read')
def get_disk_mounts():
    """Get disk usage for all mount points."""
    result = FileService.get_all_disk_mounts()

    return jsonify({'success': True, **result}), 200


@files_bp.route('/analyze', methods=['GET'])
@permission_required('files', 'read')
def analyze_directory():
    """Analyze directory sizes."""
    path = request.args.get('path', '/home')
    depth = request.args.get('depth', 2, type=int)
    limit = request.args.get('limit', 20, type=int)

    result = FileService.analyze_directory_sizes(path, depth=depth, limit=limit)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/type-breakdown', methods=['GET'])
@permission_required('files', 'read')
def get_type_breakdown():
    """Get file type breakdown for a directory."""
    path = request.args.get('path', '/home')
    max_depth = request.args.get('max_depth', 3, type=int)

    result = FileService.get_file_type_breakdown(path, max_depth=max_depth)

    return jsonify({'success': True, **result}), 200


@files_bp.route('/download', methods=['GET'])
@permission_required('files', 'read')
def download_file():
    """Download a file."""
    path = request.args.get('path')

    if not path:
        raise ValidationError('Path is required')

    if not FileService.is_path_allowed(path):
        raise permission_denied()

    if not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404

    if os.path.isdir(path):
        return jsonify({'error': 'Cannot download directory'}), 400

    return send_file(
        path,
        as_attachment=True,
        download_name=os.path.basename(path)
    )


@files_bp.route('/upload', methods=['POST'])
@permission_required('files', 'write')
def upload_file():
    """Upload a file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    destination = request.form.get('destination')

    if not destination:
        raise ValidationError('Destination path is required')

    # Upload writes straight to disk without going through FileService's
    # mutating methods, so it has to repeat the writable check itself.
    if not FileService.is_path_writable(destination):
        raise permission_denied()

    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Check file size
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)

    if size > FileService.MAX_UPLOAD_SIZE:
        return jsonify({
            'error': f'File too large. Maximum size is {FileService._format_size(FileService.MAX_UPLOAD_SIZE)}'
        }), 400

    # Determine full path. A client-supplied filename is a name, never a
    # path: "../x" must not climb out of the destination folder.
    if os.path.isdir(destination):
        name = os.path.basename(file.filename.replace('\\', '/'))
        if name in ('', '.', '..'):
            raise ValidationError('Invalid file name')
        full_path = os.path.join(destination, name)
    else:
        full_path = destination

    if not FileService.is_path_writable(full_path):
        return jsonify({'error': 'Access denied'}), 403

    # Replacing an existing file is opt-in (the UI asks first), and a folder
    # is never replaced by a file.
    if os.path.isdir(full_path):
        raise ConflictError('A folder with that name already exists', code='folder_exists')
    overwrite = (request.form.get('overwrite') or '').lower() in ('1', 'true', 'yes')
    if os.path.exists(full_path) and not overwrite:
        raise ConflictError('A file with that name already exists', code='file_exists')

    try:
        # Ensure parent directory exists
        FileService.make_parents(os.path.dirname(full_path))

        # Save file. A replaced file keeps its owner; a new one takes the
        # folder's.
        is_new = not os.path.exists(full_path)
        file.save(full_path)
        if is_new:
            FileService.own_like_parent(full_path)

        return jsonify({
            'success': True,
            'path': full_path,
            'size': size
        }), 201

    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(e)


# ── Archives (fork): compress, extract, zip for download ──
# The work runs as a job (big archives outlast a proxied request); the page
# polls /files/jobs/<id>. Validation happens here so mistakes fail fast.

def _enqueue_archive_job(kind, payload):
    from flask_jwt_extended import get_jwt_identity
    from app.jobs.service import JobService
    job = JobService.enqueue(kind, payload=payload, max_attempts=1,
                             owner_type='user', owner_id=get_jwt_identity())
    return jsonify({'job_id': job.id}), 202


def _own_archive_job(job_id):
    """The caller's own archive job, or a 404 (never someone else's)."""
    from flask_jwt_extended import get_jwt_identity
    from app.jobs.service import JobService
    job = JobService.get(job_id)
    if (job is None or job.kind not in archive_service.JOB_KINDS
            or job.owner_type != 'user' or job.owner_id != str(get_jwt_identity())):
        raise NotFoundError('Job not found')
    return job


@files_bp.route('/compress', methods=['POST'])
@permission_required('files', 'write')
def compress_paths():
    """Pack files/folders into a new zip, tar.gz or tar archive."""
    data = json_object()
    fmt = data.get('format') or 'zip'
    paths = ArchiveService.check_compress(data.get('paths'), data.get('dest'), fmt)
    return _enqueue_archive_job(archive_service.COMPRESS_JOB_KIND,
                                {'paths': paths, 'dest': os.path.abspath(data['dest']), 'format': fmt})


@files_bp.route('/extract', methods=['POST'])
@permission_required('files', 'write')
def extract_archive():
    """Unpack an archive into a folder. Without overwrite, a run that would
    replace files writes nothing and reports the conflicts as its result."""
    data = json_object()
    ArchiveService.check_extract(data.get('path'), data.get('dest'))
    return _enqueue_archive_job(archive_service.EXTRACT_JOB_KIND, {
        'path': os.path.abspath(data['path']), 'dest': os.path.abspath(data['dest']),
        'overwrite': bool(data.get('overwrite')),
    })


@files_bp.route('/download-zip', methods=['POST'])
@permission_required('files', 'read')
def prepare_zip_download():
    """Zip folders/files for download; fetch it from /jobs/<id>/download."""
    data = json_object()
    paths = ArchiveService.check_sources(data.get('paths'))
    return _enqueue_archive_job(archive_service.DOWNLOAD_JOB_KIND, {'paths': paths})


@files_bp.route('/jobs/<job_id>', methods=['GET'])
@permission_required('files', 'read')
def get_archive_job(job_id):
    """Status of one of the caller's archive jobs."""
    job = _own_archive_job(job_id)
    return jsonify({'id': job.id, 'kind': job.kind, 'status': job.status,
                    'result': job.get_result(), 'error': job.error_message}), 200


def _ready_download(job):
    path = ArchiveService.download_path(job.id) if job.kind == archive_service.DOWNLOAD_JOB_KIND else None
    if job.status != 'succeeded' or not path:
        raise NotFoundError('The download is not ready or has expired')
    return path


@files_bp.route('/jobs/<job_id>/download-link', methods=['POST'])
@permission_required('files', 'read')
def archive_job_download_link(job_id):
    """A one-time link to the zip a download job built."""
    from flask_jwt_extended import get_jwt_identity
    from ..services import download_ticket_service
    job = _own_archive_job(job_id)
    _ready_download(job)
    return jsonify({'ticket': download_ticket_service.issue(get_jwt_identity(), 'job', job.id)}), 200


# ── Downloads by one-time link (fork) ──
# A browser download can't send the Authorization header, and a login token
# in the URL ends up in logs (upstream put it there, and nothing read it, so
# every download 401'd). The page gets a 60-second single-use ticket instead.

@files_bp.route('/download-link', methods=['POST'])
@permission_required('files', 'read')
def file_download_link():
    """A one-time link for downloading one file."""
    from flask_jwt_extended import get_jwt_identity
    from ..services import download_ticket_service
    path = json_object().get('path')
    if not path:
        raise ValidationError('Path is required')
    if not FileService.is_path_allowed(path):
        raise permission_denied()
    if not os.path.isfile(path):
        raise NotFoundError('File not found')
    return jsonify({'ticket': download_ticket_service.issue(get_jwt_identity(), 'path', os.path.abspath(path))}), 200


@files_bp.route('/fetch/<ticket>', methods=['GET'])
def fetch_download(ticket):
    """Serve a one-time download. Public on purpose: the signed, expiring,
    single-use ticket is the credential, bound to who asked for it."""
    from app.jobs.service import JobService
    from ..services import download_ticket_service
    data = download_ticket_service.redeem(ticket)
    if data['k'] == 'path':
        path = data['t']
        # Re-checked at fetch: the file may have moved or the roots changed.
        if not FileService.is_path_allowed(path) or not os.path.isfile(path):
            raise NotFoundError('File not found')
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))
    if data['k'] == 'job':
        job = JobService.get(data['t'])
        if job is None or job.owner_type != 'user' or job.owner_id != data['u']:
            raise NotFoundError('The download is not ready or has expired')
        path = _ready_download(job)
        name = (job.get_result() or {}).get('download_name') or 'files.zip'
        response = send_file(path, as_attachment=True, download_name=name)
        response.call_on_close(lambda: os.path.exists(path) and os.remove(path))
        return response
    raise NotFoundError('Unknown download')


# ── S3 / object-storage browser (reuses the configured backup storage creds) ──

@files_bp.route('/s3/browse', methods=['GET'])
@permission_required('files', 'read')
def s3_browse():
    """List a bucket prefix in the same entry shape as the local browser."""
    path = request.args.get('path', '/')
    result = StorageProviderService.s3_browse(path)
    return jsonify(result), 200 if result.get('success') else 400


@files_bp.route('/s3/read', methods=['GET'])
@permission_required('files', 'read')
def s3_read():
    """Read a text object for in-app editing."""
    path = request.args.get('path')
    if not path:
        raise ValidationError('Path is required')
    result = StorageProviderService.s3_read(path)
    return jsonify(result), 200 if result.get('success') else 400


@files_bp.route('/s3/write', methods=['POST'])
@permission_required('files', 'write')
def s3_write():
    """Write (create or overwrite) an object from text content."""
    data = json_object()
    path = data.get('path')
    content = data.get('content')
    if not path:
        raise ValidationError('Path is required')
    if content is None:
        raise ValidationError('Content is required')
    result = StorageProviderService.s3_write(path, content)
    return jsonify(result), 200 if result.get('success') else 400


@files_bp.route('/s3/delete', methods=['DELETE'])
@permission_required('files', 'write')
def s3_delete():
    """Delete an object (or every object beneath a prefix)."""
    path = request.args.get('path')
    if not path:
        raise ValidationError('Path is required')
    result = StorageProviderService.s3_delete(path)
    return jsonify(result), 200 if result.get('success') else 400


@files_bp.route('/s3/download-url', methods=['GET'])
@permission_required('files', 'read')
def s3_download_url():
    """Return a short-lived presigned URL the browser can download directly."""
    path = request.args.get('path')
    if not path:
        raise ValidationError('Path is required')
    result = StorageProviderService.s3_presigned_get(path)
    return jsonify(result), 200 if result.get('success') else 400


@files_bp.route('/s3/upload', methods=['POST'])
@permission_required('files', 'write')
def s3_upload():
    """Upload a file into the bucket at the given prefix."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    destination = request.form.get('destination', '/')
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    result = StorageProviderService.s3_upload(destination, file.filename, file.stream)
    return jsonify(result), 201 if result.get('success') else 400
