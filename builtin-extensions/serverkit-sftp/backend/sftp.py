"""SFTP access API: per-folder, jailed SFTP accounts."""

from flask import Blueprint, jsonify, request

from app.middleware.rbac import admin_required, get_current_user, viewer_required
from app.models.audit_log import AuditLog
from app.services.audit_service import AuditService

from . import sftp_service as svc

sftp_bp = Blueprint('sftp', __name__)


def _error(exc: Exception, status: int = 400):
    return jsonify({'error': str(exc)}), status


def _audit(action: str, username: str, details: dict = None) -> None:
    user = get_current_user()
    AuditService.log(action, user_id=user.id if user else None,
                     target_type='sftp_account', target_id=username,
                     details=details or {})


@sftp_bp.route('/status', methods=['GET'])
@viewer_required
def get_status():
    return jsonify(svc.get_status()), 200


@sftp_bp.route('/setup', methods=['POST'])
@admin_required
def run_setup():
    try:
        status = svc.setup()
    except svc.SFTPError as exc:
        return _error(exc)
    _audit(AuditLog.ACTION_SETTINGS_UPDATE, 'setup', {'sshd_dropin': svc.SSHD_DROPIN})
    return jsonify(status), 200


@sftp_bp.route('/settings', methods=['PUT'])
@admin_required
def update_settings():
    data = request.get_json(silent=True) or {}
    try:
        host = svc.set_connection_host(data.get('host', ''))
    except svc.SFTPError as exc:
        return _error(exc)
    _audit(AuditLog.ACTION_SETTINGS_UPDATE, 'settings', {'host': host})
    return jsonify({'host': svc.connection_host()}), 200


@sftp_bp.route('/targets', methods=['GET'])
@viewer_required
def list_targets():
    return jsonify({'targets': svc.app_targets()}), 200


@sftp_bp.route('/accounts', methods=['GET'])
@viewer_required
def list_accounts():
    return jsonify({'accounts': svc.list_accounts()}), 200


@sftp_bp.route('/accounts', methods=['POST'])
@admin_required
def create_account():
    data = request.get_json(silent=True) or {}
    user = get_current_user()
    try:
        account = svc.create_account(
            (data.get('username') or '').strip(),
            (data.get('path') or '').strip(),
            password=data.get('password') or None,
            generate_password=bool(data.get('generate_password', True)),
            public_keys=data.get('public_keys') or '',
            app_id=data.get('app_id'),
            created_by=user.username if user else None,
        )
    except svc.SFTPError as exc:
        return _error(exc)
    _audit(AuditLog.ACTION_RESOURCE_CREATE, account['username'],
           {'path': account['path'], 'keys': len(account['keys'])})
    return jsonify(account), 201


@sftp_bp.route('/accounts/<username>', methods=['DELETE'])
@admin_required
def delete_account(username):
    try:
        svc.delete_account(username)
    except svc.SFTPError as exc:
        return _error(exc, 404)
    _audit(AuditLog.ACTION_RESOURCE_DELETE, username)
    return jsonify({'deleted': username}), 200


@sftp_bp.route('/accounts/<username>/password', methods=['POST'])
@admin_required
def reset_password(username):
    data = request.get_json(silent=True) or {}
    try:
        password = svc.reset_password(username, data.get('password') or None)
    except svc.SFTPError as exc:
        return _error(exc)
    _audit(AuditLog.ACTION_RESOURCE_UPDATE, username, {'changed': 'password'})
    return jsonify({'username': username, 'password': password}), 200


@sftp_bp.route('/accounts/<username>/keys', methods=['PUT'])
@admin_required
def set_keys(username):
    data = request.get_json(silent=True) or {}
    try:
        keys = svc.set_keys(username, data.get('public_keys') or '')
    except svc.SFTPError as exc:
        return _error(exc)
    _audit(AuditLog.ACTION_RESOURCE_UPDATE, username, {'changed': 'keys', 'keys': len(keys)})
    return jsonify({'username': username, 'keys': keys}), 200


@sftp_bp.route('/accounts/<username>/toggle', methods=['POST'])
@admin_required
def toggle_account(username):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled', True))
    try:
        svc.set_enabled(username, enabled)
    except svc.SFTPError as exc:
        return _error(exc)
    _audit(AuditLog.ACTION_RESOURCE_UPDATE, username, {'enabled': enabled})
    return jsonify({'username': username, 'enabled': enabled}), 200
