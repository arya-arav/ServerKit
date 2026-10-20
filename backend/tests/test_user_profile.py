"""A user's first/last name and profile picture (PUT /api/v1/auth/me).
Fork: arya-arav/ServerKit."""
import base64

import pytest

from app.utils.profile import AVATAR_MAX_BYTES, clean_avatar, clean_name

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
WEBP = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 24


def data_url(mime, raw):
    return f'data:{mime};base64,{base64.b64encode(raw).decode()}'


@pytest.mark.parametrize('mime,raw', [('image/png', PNG), ('image/jpeg', JPEG), ('image/webp', WEBP)])
def test_png_jpeg_and_webp_pictures_are_accepted(mime, raw):
    assert clean_avatar(data_url(mime, raw)) == data_url(mime, raw)


@pytest.mark.parametrize('value', [
    data_url('image/svg+xml', b'<svg onload="alert(1)"/>'),   # SVG can carry script
    data_url('image/png', b'<html><script>alert(1)</script>'),  # bytes are not a PNG
    data_url('image/png', JPEG),                                 # declared type does not match
    'data:image/png;base64,***',
    'https://example.com/me.png',
    data_url('image/png', PNG + b'\x00' * AVATAR_MAX_BYTES),     # too large
])
def test_anything_else_is_refused(value):
    with pytest.raises(ValueError):
        clean_avatar(value)


def test_empty_removes_the_picture():
    assert clean_avatar('') is None
    assert clean_avatar(None) is None


def test_names_are_tidied():
    assert clean_name('  Ada \n  Lovelace ') == 'Ada Lovelace'
    assert clean_name('   ') is None
    assert clean_name('Zo\x00e') == 'Zoe'
    with pytest.raises(ValueError):
        clean_name('x' * 81)


def test_profile_name_and_picture_round_trip(client, auth_headers):
    resp = client.put('/api/v1/auth/me', headers=auth_headers, json={
        'first_name': 'Ada', 'last_name': 'Lovelace', 'avatar': data_url('image/png', PNG)})

    assert resp.status_code == 200, resp.get_json()
    user = resp.get_json()['user']
    assert user['display_name'] == 'Ada Lovelace'
    assert user['avatar'] == data_url('image/png', PNG)

    me = client.get('/api/v1/auth/me', headers=auth_headers).get_json()['user']
    assert (me['first_name'], me['last_name']) == ('Ada', 'Lovelace')


def test_display_name_falls_back_to_the_username(client, auth_headers):
    resp = client.put('/api/v1/auth/me', headers=auth_headers,
                      json={'first_name': '', 'last_name': '', 'avatar': ''})
    user = resp.get_json()['user']
    assert user['display_name'] == user['username']
    assert user['avatar'] is None


def test_a_bad_picture_is_a_400(client, auth_headers):
    resp = client.put('/api/v1/auth/me', headers=auth_headers,
                      json={'avatar': data_url('image/svg+xml', b'<svg/>')})
    assert resp.status_code == 400
