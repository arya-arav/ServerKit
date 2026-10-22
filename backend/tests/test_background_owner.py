"""One process owns the background work. Fork: arya-arav/ServerKit.

A second process that loads the app while the panel runs (a one-off script)
must not run interrupted-job recovery or start consumers: that failed the
panel's in-flight jobs. It waits for the lock instead, which is also how a
restarted worker takes over once the old one exits.
"""
import contextlib
import os
import threading

import pytest

from app.utils import background_owner

fcntl = pytest.importorskip('fcntl')


class FakeApp:
    def __init__(self, testing=False):
        self.config = {'TESTING': testing}

    @contextlib.contextmanager
    def app_context(self):
        yield


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = str(tmp_path / 'owner.lock')
    monkeypatch.setattr(background_owner, '_lock_path', lambda: path)
    monkeypatch.setattr(background_owner, '_held_fd', None)
    return path


def _hold(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_the_first_process_starts_right_away(lock_path):
    started = []
    background_owner.run_as_background_owner(FakeApp(), lambda: started.append(1))
    assert started == [1]


def test_a_second_process_waits_and_takes_over_when_the_owner_exits(lock_path):
    owner = _hold(lock_path)          # the running panel
    started = threading.Event()
    background_owner.run_as_background_owner(FakeApp(), started.set)
    assert not started.wait(0.3)      # recovery/consumers did NOT run

    os.close(owner)                   # the old worker exits
    assert started.wait(5)


def test_tests_and_lockless_hosts_start_directly(lock_path, monkeypatch):
    started = []
    owner = _hold(lock_path)
    background_owner.run_as_background_owner(FakeApp(testing=True), lambda: started.append('t'))
    monkeypatch.setattr(background_owner, '_lock_path', lambda: '/nonexistent-dir/x.lock')
    background_owner.run_as_background_owner(FakeApp(), lambda: started.append('d'))
    assert started == ['t', 'd']
    os.close(owner)
