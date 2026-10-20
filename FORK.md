# This fork

`arya-arav/ServerKit` is a fork of [jhd3197/ServerKit](https://github.com/jhd3197/ServerKit) (MIT). It runs its maintainer's own panel.

## What differs from upstream

- **Certificate expiry check** (`backend/app/services/doctor_service.py`, `_cert_check`). Upstream only reads `Domain.ssl_expires_at`, which nothing fills, so the health page always warned "expiry is not being monitored". The fork also reads the certificates on disk (`/etc/letsencrypt/live/*/cert.pem`, `/etc/ssl/serverkit/*/cert.pem`). It reports whether `certbot.timer` renews them automatically and warns if automatic renewal is off. Tests: `backend/tests/test_doctor_cert_check.py`.
- **Updates come from this fork.** `scripts/update.sh` and `install.sh` default to `GITHUB_REPO=arya-arav/ServerKit`. `update.sh` rebuilds from this fork's `main` on the server, because the fork publishes no release tarballs. Both `sudo serverkit update` and the panel's Update button follow this fork.
- **Cloudflare-proxied domains in the DNS check** (`_dns_check_one`). A domain whose addresses are all Cloudflare edge IPs (`CLOUDFLARE_NETWORKS`) reports ok ("proxied through Cloudflare") instead of "not this server". Mixed or unrelated addresses still warn. Tests are at the end of `backend/tests/test_doctor_dns.py`.
- **Custom-domain HTTPS survives re-renders** (`SiteDomainService.own_domain_cert_paths`). Upstream only kept HTTPS for base-domain wildcard certs, so a doctor "Fix", drift repair or app edit rewrote a custom domain's vhost as HTTP-only, and HTTPS then served the panel. `enable_ssl` also saved the key path to a non-existent attribute (`ssl_private_key_path`); it now uses `ssl_key_path`. Tests: `backend/tests/test_own_domain_cert_vhost.py`.
- **Let's Encrypt behind the dotfile deny.** The nginx configs use `location ^~ /.well-known/acme-challenge/`. Without `^~`, the regex `location ~ /\.` (deny all) wins, and certbot's HTTP-01 challenge gets a 403.

The panel's "update available" notice still watches upstream releases, so it tells you when there is something new to merge.

## Taking upstream updates

```bash
git fetch upstream
git merge upstream/main        # resolve conflicts, keep the fork changes above
git push origin main
# then on the server:
sudo serverkit update
```

## Server-side changes not in this repo

These live on the server only. Re-check them after an update.

- `/etc/nginx/conf.d/cloudflare-realip.conf` (real visitor IPs behind Cloudflare).
- Cloudflare allow lists for Fail2ban (`/etc/fail2ban/jail.d/zz-cloudflare-ignore.local`) and CrowdSec (`/etc/crowdsec/parsers/s02-enrich/cloudflare-whitelist.yaml`).
