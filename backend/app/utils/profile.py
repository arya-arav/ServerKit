"""Checks for a user's display name and profile picture. Fork: arya-arav/ServerKit.

The picture is stored as a data URL on the user row, so it reaches the page
inside the user object; an <img> cannot send the bearer token a separate
image route would need. Only PNG, JPEG and WEBP are accepted, and the bytes
must match the declared type: SVG and anything else could carry script.
"""
import base64
import binascii
import re

NAME_MAX = 80
AVATAR_MAX_BYTES = 256 * 1024

_DATA_URL_RE = re.compile(r'^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)$')
_SIGNATURES = {
    'image/png': lambda raw: raw.startswith(b'\x89PNG\r\n\x1a\n'),
    'image/jpeg': lambda raw: raw.startswith(b'\xff\xd8\xff'),
    'image/webp': lambda raw: raw[:4] == b'RIFF' and raw[8:12] == b'WEBP',
}


def clean_name(value):
    """A first or last name, tidied; None when empty. Raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Names must be text.')
    name = ''.join(ch for ch in ' '.join(value.split()) if ch.isprintable())
    if len(name) > NAME_MAX:
        raise ValueError(f'Names can be at most {NAME_MAX} characters.')
    return name or None


def clean_avatar(value):
    """A PNG/JPEG/WEBP data URL, re-encoded; None removes it. Raises ValueError."""
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValueError('Upload a PNG, JPEG or WEBP image.')
    match = _DATA_URL_RE.match(''.join(value.split()))
    if not match:
        raise ValueError('Upload a PNG, JPEG or WEBP image.')
    mime, payload = match.groups()
    if len(payload) > AVATAR_MAX_BYTES * 4 // 3 + 4:
        raise ValueError('The picture is too large (256 KB at most).')
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('The picture could not be read.') from None
    if len(raw) > AVATAR_MAX_BYTES:
        raise ValueError('The picture is too large (256 KB at most).')
    if not _SIGNATURES[mime](raw):
        raise ValueError('The file is not a valid PNG, JPEG or WEBP image.')
    return f'data:{mime};base64,{base64.b64encode(raw).decode()}'
