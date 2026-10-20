"""Trust bugs in the wildcard/SAN certbot path.

The advanced SSL service ran certbot unprivileged (it cannot write
/etc/letsencrypt that way) and returned ``success: True`` without looking at
the result, so a failed issuance reported the would-be cert paths as if they
existed. Cloudflare credentials went to a predictable ``/tmp`` path and were
never deleted. Both issuance entry points are asserted here against the
``run_privileged`` seam, plus the domain validation that keeps crafted
domains from being parsed as certbot flags.
"""

import os
import stat

import pytest

import app.services.advanced_ssl_service as mod
from app.services.advanced_ssl_service import AdvancedSSLService
from app.services.advanced_ssl_service import DNS_CREDENTIALS_DIR as REAL_CREDENTIALS_DIR
from subprocess_stub import FakeProc


def _must_not_run_unprivileged(cmd, **kw):
    raise AssertionError(
        f'certbot must run through run_privileged, got run_unprivileged({cmd!r})')


@pytest.fixture
def certbot(monkeypatch, tmp_path):
    """run_privileged seam captured; PackageManager and the unprivileged door
    are both taken out — certbot has no business running unprivileged. The
    DNS credentials folder points at tmp_path."""
    from app.utils.system import PackageManager

    monkeypatch.setattr(PackageManager, 'is_available', staticmethod(lambda: False))
    monkeypatch.setattr(mod, 'run_unprivileged', _must_not_run_unprivileged)
    monkeypatch.setattr(mod, 'DNS_CREDENTIALS_DIR', str(tmp_path / 'serverkit-dns'))

    def write(path, content, mode=None, **kw):
        with open(path, 'w') as f:
            f.write(content)
        if mode and os.name != 'nt':
            os.chmod(path, int(mode, 8))
        return {'success': True, 'path': path}

    monkeypatch.setattr(mod, 'write_privileged_file', write)

    captured = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ['install', '-d']:
            os.makedirs(cmd[-1], exist_ok=True)
            return FakeProc()
        captured['cmd'] = list(cmd)
        captured['kw'] = kw
        return captured['proc']

    monkeypatch.setattr(mod, 'run_privileged', fake_run)
    return captured


class TestWildcardIssuance:
    def test_a_failed_certbot_is_a_failure_never_cert_paths(self, certbot):
        certbot['proc'] = FakeProc(returncode=1, stderr='DNS problem: NXDOMAIN')

        res = AdvancedSSLService.issue_wildcard_cert(
            'example.com', 'cloudflare', {'api_token': 'tok'})

        assert res['success'] is False
        assert 'NXDOMAIN' in res['error']
        assert 'certificate_path' not in res, 'a failed issuance reported cert paths'

    def test_a_successful_certbot_reports_the_cert_paths(self, certbot):
        certbot['proc'] = FakeProc(stdout='Congratulations!')

        res = AdvancedSSLService.issue_wildcard_cert(
            'example.com', 'cloudflare', {'api_token': 'tok'})

        assert res['success'] is True
        assert res['certificate_path'] == '/etc/letsencrypt/live/example.com/fullchain.pem'
        assert res['private_key_path'] == '/etc/letsencrypt/live/example.com/privkey.pem'

    def test_an_unsupported_provider_is_refused(self, certbot):
        res = AdvancedSSLService.issue_wildcard_cert(
            'example.com', 'digitalocean', {'api_token': 'tok'})
        assert res['success'] is False
        assert 'cmd' not in certbot, 'certbot ran for an unsupported provider'


class TestCloudflareCredentialsFile:
    """certbot records the credentials path in the renewal config and reads it
    again at every renewal, so the file must outlive the first run — in a
    root-only folder, owner-only, never in /tmp. Upstream deleted it after
    issuing and every wildcard certificate failed to renew. Fork: arya-arav/ServerKit."""

    def _creds_path(self, certbot):
        cmd = certbot['cmd']
        return cmd[cmd.index('--dns-cloudflare-credentials') + 1]

    def test_credentials_file_survives_for_renewals(self, certbot, tmp_path):
        certbot['proc'] = FakeProc(stdout='Congratulations!')

        res = AdvancedSSLService.issue_wildcard_cert(
            'example.com', 'cloudflare', {'api_token': 'tok'})

        assert res['success'] is True
        path = self._creds_path(certbot)
        assert path == str(tmp_path / 'serverkit-dns' / 'cloudflare-example.com.ini')
        assert REAL_CREDENTIALS_DIR.startswith('/etc/letsencrypt/')
        with open(path) as f:
            assert f.read() == 'dns_cloudflare_api_token = tok\n'
        if os.name != 'nt':
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_a_new_token_replaces_the_old_one(self, certbot):
        certbot['proc'] = FakeProc(stdout='Congratulations!')
        AdvancedSSLService.issue_wildcard_cert('example.com', 'cloudflare', {'api_token': 'old'})

        AdvancedSSLService.issue_wildcard_cert('example.com', 'cloudflare', {'api_token': 'new'})

        with open(self._creds_path(certbot)) as f:
            assert f.read() == 'dns_cloudflare_api_token = new\n'


class TestSanIssuance:
    def test_a_failed_certbot_is_a_failure(self, certbot):
        certbot['proc'] = FakeProc(returncode=1, stderr='webroot unreachable')

        res = AdvancedSSLService.issue_san_cert(['a.example.com', 'b.example.com'])

        assert res['success'] is False
        assert 'webroot unreachable' in res['error']

    def test_a_successful_certbot_reports_the_domains(self, certbot):
        certbot['proc'] = FakeProc(stdout='Congratulations!')

        res = AdvancedSSLService.issue_san_cert(['a.example.com', 'b.example.com'])

        assert res['success'] is True
        assert res['domains'] == ['a.example.com', 'b.example.com']
        for d in ('a.example.com', 'b.example.com'):
            assert d in certbot['cmd']


class TestDomainValidation:
    """A crafted domain must not survive into certbot argv, where a leading
    dash would be parsed as a flag, nor into the custom-cert file path."""

    def test_wildcard_rejects_a_flag_like_domain(self, certbot):
        res = AdvancedSSLService.issue_wildcard_cert(
            '--cert-name=evil', 'cloudflare', {'api_token': 'tok'})
        assert res['success'] is False
        assert 'cmd' not in certbot, 'certbot ran for an invalid domain'

    def test_wildcard_rejects_shell_metacharacters(self, certbot):
        res = AdvancedSSLService.issue_wildcard_cert(
            'example.com; rm -rf /', 'cloudflare', {'api_token': 'tok'})
        assert res['success'] is False
        assert 'cmd' not in certbot

    def test_san_rejects_one_bad_domain_out_of_many(self, certbot):
        res = AdvancedSSLService.issue_san_cert(['good.example.com', '--expand'])
        assert res['success'] is False
        assert 'cmd' not in certbot

    def test_upload_custom_cert_rejects_traversal(self):
        with pytest.raises(ValueError):
            AdvancedSSLService.upload_custom_cert('../../etc/cron.d', 'cert', 'key')
