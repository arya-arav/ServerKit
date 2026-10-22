"""Exactly one process runs the panel's background work. Fork: arya-arav/ServerKit.

create_app() starts the job system, queue consumers and monitors, and first
fails every job still marked running, on the assumption that the process
that ran them just died. That assumption is wrong whenever a *second* process
loads the app while the panel is up (a one-off script, a stray `python -c`):
it killed the panel's in-flight jobs (a 2-minute Lynis scan showed "error"
every time) and started a duplicate consumer and scheduler.

The owner holds an exclusive flock for its lifetime. A process that can't
get it leaves the background alone and waits for the lock in a daemon
thread, which is what makes a restart work: the new gunicorn worker takes
over (recovery included) the moment the old one exits, and a short script
simply ends before ever getting it.
"""
import logging
import os
import threading

try:
    import fcntl
except ImportError:  # Windows dev boxes: no flock, keep the old behaviour
    fcntl = None

logger = logging.getLogger(__name__)

LOCK_NAME = '.background-owner.lock'
_held_fd = None  # kept open for the process lifetime; closing it releases the lock


def _lock_path():
    from app import paths
    return os.path.join(paths.SERVERKIT_DIR, LOCK_NAME)


def run_as_background_owner(app, start):
    """Run ``start()`` now if this process gets the lock, otherwise once the
    current owner exits. Without a lock directory (dev, tests) run it now."""
    global _held_fd
    if fcntl is None or app.config.get('TESTING'):
        start()
        return
    try:
        fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        start()
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info('Background work is owned by another process; waiting to take over')

        def take_over():
            global _held_fd
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until the owner exits
            _held_fd = fd
            logger.info('Took over background work from the previous process')
            with app.app_context():
                start()

        threading.Thread(target=take_over, name='serverkit-background-owner', daemon=True).start()
        return
    _held_fd = fd
    start()
