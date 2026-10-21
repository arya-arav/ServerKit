"""One-time links for browser downloads. Fork: arya-arav/ServerKit.

A browser download is a plain navigation, so it can't carry the
``Authorization`` header the API requires, and putting the login token in the
URL leaks it into proxy logs and browser history (upstream did, and the API
never read it, so downloads just failed with 401). Instead the page asks for a
ticket with its normal auth and the browser fetches ``/files/fetch/<ticket>``:

- signed with the app's SECRET_KEY, so it can't be forged or edited
- valid for TTL seconds and redeemable once
- bound to the user and to one file path or one download job
"""
import secrets
import threading
import time

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.exceptions import NotFoundError

TTL = 60
_SALT = 'serverkit-file-download'

_used = {}
_lock = threading.Lock()


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=_SALT)


def issue(user_id, kind, target):
    """A ticket for ``kind`` 'path' (target: file path) or 'job' (job id)."""
    return _serializer().dumps({
        'u': str(user_id), 'k': kind, 't': target, 'n': secrets.token_urlsafe(9),
    })


def redeem(ticket):
    """The ticket's payload, once. Expired, forged or reused -> 404."""
    try:
        data = _serializer().loads(ticket, max_age=TTL)
    except (SignatureExpired, BadSignature) as exc:
        raise NotFoundError('This download link has expired. Start the download again.') from exc
    now = time.time()
    with _lock:
        for nonce, at in list(_used.items()):
            if now - at > TTL * 2:
                del _used[nonce]
        if data['n'] in _used:
            raise NotFoundError('This download link was already used. Start the download again.')
        _used[data['n']] = now
    return data
