# This fork

`arya-arav/ServerKit` is a fork of [jhd3197/ServerKit](https://github.com/jhd3197/ServerKit) (MIT). It runs its maintainer's own panel.

## What differs from upstream

- **Certificate expiry check** (`backend/app/services/doctor_service.py`, `_cert_check`). Upstream only reads `Domain.ssl_expires_at`, which nothing fills, so the health page always warned "expiry is not being monitored". The fork also reads the certificates on disk (`/etc/letsencrypt/live/*/cert.pem`, `/etc/ssl/serverkit/*/cert.pem`). It reports whether `certbot.timer` renews them automatically and warns if automatic renewal is off. Tests: `backend/tests/test_doctor_cert_check.py`.
- **Updates come from this fork.** `scripts/update.sh` and `install.sh` default to `GITHUB_REPO=arya-arav/ServerKit`. `update.sh` rebuilds from this fork's `main` on the server, because the fork publishes no release tarballs. Both `sudo serverkit update` and the panel's Update button follow this fork.
- **Cloudflare-proxied domains in the DNS check** (`_dns_check_one`). A domain whose addresses are all Cloudflare edge IPs (`CLOUDFLARE_NETWORKS`) reports ok ("proxied through Cloudflare") instead of "not this server". Mixed or unrelated addresses still warn. The shared `_aaaa_conflicts` helper (also behind the domain page's Verify DNS) ignores a proxied record's Cloudflare IPv6 addresses, and Verify DNS notes that the domain is proxied. Tests are at the end of `backend/tests/test_doctor_dns.py`.
- **Custom-domain HTTPS survives re-renders** (`SiteDomainService.own_domain_cert_paths`). Upstream only kept HTTPS for base-domain wildcard certs, so a doctor "Fix", drift repair or app edit rewrote a custom domain's vhost as HTTP-only, and HTTPS then served the panel. `enable_ssl` also saved the key path to a non-existent attribute (`ssl_private_key_path`); it now uses `ssl_key_path`. Tests: `backend/tests/test_own_domain_cert_vhost.py`.
- **Templates search keeps focus** (`frontend/src/pages/Templates.jsx`). Each keystroke refetched and the page swapped to its loading skeleton, which unmounted the top-bar search box; now only the first load shows the skeleton.
- **New template: PHP (Nginx + PHP-FPM)** (`backend/templates/php-nginx-fpm.yaml`). Any PHP from 7.0 to 8.4, with mysqli, pdo_mysql, opcache and bcmath, a web-root and upload-size option, and the port published on 127.0.0.1 only. Tests: `backend/tests/test_php_nginx_fpm_template.py`. Checked on the server with PHP 7.0 and 8.3.
- **App details card** on the service Overview (`AppDetailsCard.jsx`, `GET /api/v1/apps/<id>/details`, `app_details_service.py`): the template, its version and the options chosen (secrets masked server-side), each container's image, state, ports and detected PHP version, domains with HTTPS expiry, folder and compose file. Tests: `backend/tests/test_app_details.py`.
- **Recipes page layout and units** (`Recipes.jsx`, `_recipes.scss`). The page now uses the tab-group frame (`sk-tabgroup__inner`), like the other tabs, so it has margins and the grid no longer runs off the right. Category filters are real chips, not unstyled `sk-pill` buttons with a blank active "All". Recipe memory (`memoryMB`) was multiplied by a GB constant, so 8192 MB showed as "8 TB" and the install preflight compared the wrong numbers.
- **AI agent and game server templates**: `hermes-agent`, `openclaw`, `minecraft-java`, `minecraft-bedrock`, `factorio`, `palworld`, `terraria`. All have ARM64 images and were started on the Oracle ARM server. Hermes uses host networking with its dashboard on 127.0.0.1 (Hermes refuses non-loopback binds without dashboard auth). OpenClaw seeds `gateway.mode` on first start, since a fresh install will not start without it. Game ports are public by design; on this server they also need an entry in `/etc/docker-ports-guard.allow`. Left out: Core Keeper (its SteamCMD self-update breaks under Box64 on ARM), Luanti (ships no game since 5.8), and x86-only servers (Valheim, Satisfactory, CS2, Rust, Enshrouded, 7 Days to Die, Don't Starve Together).
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
