"""File integrity baseline/check skip sockets and FIFOs (fork: arya-arav/ServerKit).

Oracle's monitoring agent keeps a UNIX socket under /etc, and opening it raised
ENXIO ("No such device or address"), which aborted the whole baseline. A FIFO
is worse: opening it for reading blocks forever."""
import os
import socket

import pytest

from app.services.security_service import SecurityService


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / 'etc'
    (root / 'agenttmp').mkdir(parents=True)
    (root / 'app.conf').write_text('a=1\n')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(root / 'agenttmp' / 'SOCKETMANAGER'))
    os.mkfifo(root / 'pipe')
    monkeypatch.setattr(SecurityService, 'CONFIG_DIR', str(tmp_path / 'cfg'))
    monkeypatch.setattr(SecurityService, 'INTEGRITY_DB', str(tmp_path / 'cfg' / 'integrity.json'))
    monkeypatch.setattr(SecurityService, 'get_config',
                        classmethod(lambda cls: {'file_integrity': {'monitored_paths': [str(root)]}}))
    monkeypatch.setattr(SecurityService, '_log_alert', classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(SecurityService, '_send_security_notification', classmethod(lambda cls, *a, **k: None))
    yield root
    sock.close()


def test_baseline_skips_socket_and_fifo(tree):
    result = SecurityService.initialize_integrity_database([str(tree)])
    assert result['success'], result
    assert result['file_count'] == 1


def test_check_does_not_report_special_files_as_new(tree):
    SecurityService.initialize_integrity_database([str(tree)])
    (tree / 'new.conf').write_text('b=2\n')
    result = SecurityService.check_file_integrity()
    assert result['success'], result
    assert result['changes']['new'] == [str(tree / 'new.conf')]
