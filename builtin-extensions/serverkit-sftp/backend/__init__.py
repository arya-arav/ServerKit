"""ServerKit SFTP access extension backend package.

Jailed, per-folder SFTP accounts over the server's SSH daemon. Mounted under
``/api/v1/sftp`` via the manifest's ``url_prefix``.
"""
from .sftp import sftp_bp

__all__ = ['sftp_bp']
