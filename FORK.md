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
- **File integrity skips sockets and FIFOs** (`SecurityService._regular_file_stat`). Oracle's monitoring agent keeps a UNIX socket under /etc; opening it raised ENXIO and aborted the baseline ("No such device or address"), and a FIFO would have blocked the scan forever. Tests: `backend/tests/test_integrity_special_files.py`.
- **File integrity ignores its own database, and Accept clears the posture** (`SecurityService._is_own_database`, `_integrity_baseline_time`).
  - **Own database:** the integrity database lives in /etc/serverkit, inside the monitored /etc, and is written after the baseline scan. So every check reported it as a new file (or, after a re-baseline, a modified one), and the security posture went amber on its own.
  - **Accept:** the posture's "Integrity · 24h" count now only counts changes found after the current baseline, so accepting a reviewed change clears the warning at once instead of a day later.
  - Tests: `backend/tests/test_integrity_own_database.py`.
- **SFTP access extension** (`builtin-extensions/serverkit-sftp`, Files → SFTP access; install it from Marketplace → Built-in). Per-folder SFTP accounts for FileZilla/WinSCP over the existing SSH port, instead of plain FTP:
  - **Setup** installs `acl`, creates the `sk-sftp` group and `/srv/sftp-jails`, and writes `/etc/ssh/sshd_config.d/50-serverkit-sftp.conf` (`Match Group sk-sftp`: chroot, `internal-sftp` only, no TTY or forwarding, password logins allowed for this group only). The rule is checked with `sshd -t` before ssh reloads, and removed again if sshd rejects it.
  - **An account** is a no-shell user whose jail holds `site/`, a bind mount (an `/etc/fstab` line tagged `x-serverkit-sftp`) of the chosen folder. Write access comes from POSIX ACLs on that folder only, with default entries that keep the folder owner and group able to use new files. The site's own permissions are not widened.
  - **Folder policy:** app folders and subfolders of `/srv`, `/var/www` and `/home/<user>`. It refuses system paths, the jails, and any folder that is or contains a home directory, since that would hand out its `.ssh`.
  - **Actions** only ever touch users recorded by the extension. Deleting an account hands its uploaded files back to the folder owner.
  - **Deleting restores permissions** (1.0.3). Each account records whether its folder had any ACLs before (`acl_baseline`). When the last account on a folder (or on one around or inside it) is deleted and the folder had none, every ACL under it is removed (`setfacl -R -b`). Before, the owner and group defaults and the ACL mask stayed on every file forever. Folders with ACLs of their own keep them; only the account's entries go.
  - **Tests:** `backend/tests/test_sftp_extension.py`.
- **The panel answers only on its own domain** (`nginx/sites-available/serverkit.conf`, `install.sh`, `scripts/update.sh`). Upstream made the panel the nginx `default_server`. Any hostname pointing at the server with no site of its own (a DNS record added before the domain was attached to a service, a removed domain, a scanner's random Host) showed the panel login.
  - In secure (HTTPS) mode a catch-all default server now drops unknown names: `return 444` on :80, while ACME challenges still pass, and `ssl_reject_handshake` on :443.
  - The panel blocks use `server_name YOUR_DOMAIN`, which install/update fill in from `/etc/serverkit/panel-domain` together with the certificate path.
  - HTTP-only installs without a panel domain are unchanged, so first-time setup by IP still works.
- **HTTPS covers every domain of a service** (`SiteDomainService.enable_app_https`, `domains.py` enable/disable/renew SSL, `SettingsTab.jsx`, `Domains.jsx`). A service's domains share one vhost, but upstream "Enable SSL" asked for a certificate for the clicked name only and put it on the whole vhost, so aliases had a name mismatch.
  - The service Settings tab issued a certificate for the primary domain and only flipped a flag. It never stored the path or touched nginx.
  - Both buttons now request one certificate for all the service's domains, primary first, with certbot `--cert-name <primary>` so the certificate is expanded in place. Names with no DNS record are skipped and reported. Covered domains get the certificate and the vhost is re-rendered at once.
  - Disable SSL re-renders too. Renew uses the certificate's lineage, not the alias name. Attaching a domain to a service that already serves HTTPS returns an `ssl_notice`.
  - Tests: `backend/tests/test_app_https_all_domains.py`.
- **Updates no longer re-open 80/443 to the world** (`scripts/update.sh` `ensure_firewall`, `scripts/lib/firewall.sh` `firewall_port_has_rule`).
  - Upstream ran `ufw allow 80/tcp` and `443/tcp` on every update. On this server that re-added allow-from-anywhere rules next to the Cloudflare-only ones.
  - It now opens a web port only when the firewall has no rule for it at all. Restricted rules count as rules, for ufw, iptables, firewalld and nftables.
  - Tests: T25b in `scripts/test/test_install.sh`, and the `ensure_firewall` cases at the end of `scripts/test/test_update.sh`.
- **Built-in extensions are refreshed on update** (`plugin_service._builtin_backend_is_stale` / `repair_missing_plugins`). An installed builtin's backend is a copy under `backend/app/plugins/<slug>`, which update.sh carries forward unchanged. A builtin therefore kept running the code of the panel version it was first installed on, and every fix needed a manual uninstall and reinstall. The boot repair pass now also reinstalls a builtin-sourced extension, in place with its record and data kept, when its copied backend differs from the `builtin-extensions/` this version ships. Tests: the `repair_*builtin*` cases in `backend/tests/test_plugins_pipeline.py`.
- **Redirect a service's other domains to its primary** (`Application.redirect_aliases`, migration `111_redirect_aliases`; `PUT /api/v1/apps/<id>/redirect-aliases`; switch in service Settings → Domains, shown when a service has 2+ domains).
  - Upstream served every alias as an identical copy of the site.
  - When the switch is on, `NginxService.render_site_config(canonical_domain=…)` serves the site on the primary only. Every other domain gets a 301 to the same path there: on :80, and on :443 with the shared certificate.
  - A service's domains are now rendered primary first. The HTTP→HTTPS redirect targets `$server_name`, the first listed name, which used to be whatever order the database returned.
  - Changing the primary domain, or a domain's SSL flag, re-renders the vhost at once.
  - Tests: `backend/tests/test_redirect_aliases.py`.
- **Move a domain to another service** (`POST /api/v1/domains/<id>/move`; Domains drawer → "Move to another service"). Upstream could only delete a domain and add it again. Moving needs edit rights on both services and re-publishes both vhosts. The source site is taken down when that was its last domain, as delete does.
  - The domain becomes the target's primary when the target has none.
  - Its HTTPS is switched off, because the certificate belonged to the source's lineage, and the response says to run Enable SSL on the target.
  - Deleting or moving a primary now promotes the oldest remaining domain (`_ensure_primary`). Before, the service was left with no primary.
  - Tests: `backend/tests/test_move_domain.py`.
- **Verify DNS checks access** (`domains.py` `verify_domain`). It was the only per-domain route without the live-service and `can_access_app` gate, so any logged-in user could probe any domain id, including those of deleted services. Tests: `backend/tests/test_verify_domain_access.py`.
- **Cloudflare-only web traffic** (`cloudflare_only_service.py`; `GET/PUT /api/v1/firewall/cloudflare-only`, `POST …/refresh`; card in Security → Firewall). A switch that lets ports 80 and 443 accept Cloudflare's edge only, so nobody who finds the origin IP can go around Cloudflare's WAF or Access.
  - **Oracle images:** INPUT rejects everything before ufw's chains, so for IPv4 ufw never sees a packet. When the host has that reject, the rules go into INPUT ahead of it, live and in `/etc/iptables/rules.v4`. The file is edited as text and checked with `iptables-restore --test`. It is never `netfilter-persistent save`d, which would freeze ufw's, Docker's and CrowdSec's chains into it.
  - **ufw:** gets one rule per range, IPv4 and IPv6.
  - **Rules replaced:** open-to-everyone 80/443 rules and hand-made `cloudflare-web` rules. Rules are added before old ones are removed, so Cloudflare is never cut off mid-change.
  - **Real visitor IPs:** nginx gets `conf.d/serverkit-cloudflare-realip.conf` from the same list, unless another file already sets `real_ip_header`.
  - **Proxy check:** turning it on is refused (409) while a served domain or the panel domain resolves to anything but Cloudflare; `force` overrides. Turning it off reopens 80/443 with a `serverkit-web` rule.
  - **Range list:** fetched from cloudflare.com/ips-v4 and /ips-v6, and refused if it looks wrong. Re-checked weekly (`cloudflare-ips-refresh` job, a no-op until the switch is used). The bundled fallback list (`app/utils/cloudflare_ips.py`) is shared with the doctor's DNS check.
  - **Never touched:** port 22.
  - Tests: `backend/tests/test_cloudflare_only.py`.
- **Setup checks read where the panel writes** (`SetupHealthService._base_domain`, `_dns_mode`, `_https_enabled`, `_email_delivery_item`). Two warnings never cleared on a configured panel:
  - **Base domain:** the base domain, DNS mode and HTTPS flag were read from the legacy `sites_*` settings, while Settings → Site settings writes the base-domain registry, so it always reported "no base domain".
  - **Email:** email delivery only counted provider connections, not the alert email channel in Settings → Notifications, which sends over its own SMTP settings.
  - Tests: the `registered_in_site_settings` and `alert_email_channel` cases in `backend/tests/test_setup_health.py`.
- **"Backups scheduled" counts schedules** (`SetupHealthService._backup_policy_item`). Upstream only counted per-service backup policies, so a server backed up by a schedule from Backups → Schedules still got "nothing is being backed up". Tests: the `backup_policy` cases in `backend/tests/test_setup_health.py`.
- **Off-site backup upkeep** (`BackupService._cleanup_remote`, `_record_remote`).
  - **Cleanup:** the daily cleanup now also deletes off-site copies older than the retention (`remote_retention_days`, defaulting to `retention_days`). Upstream pruned local copies only, so the bucket grew forever. Only objects under the destination's path prefix are touched, and nothing is swept when no prefix is set.
  - **Status:** a file backup's metadata now says `synced` after a successful upload; upstream rewrote it only for directory backups.
  - **No double upload:** a scheduled upload is skipped when auto-upload already sent the backup.
  - **Test connection** (`StorageProviderService._unmask_secrets`): the Storage form shows saved keys masked (`abcd****`) and sends the masks back. Saving already swapped them for the stored values, but Test connection used them as typed and always failed with SignatureDoesNotMatch. Both now share the swap.
  - Tests: `backend/tests/test_backup_offsite_upkeep.py`, `backend/tests/test_storage_test_connection.py`.
- **Run a backup schedule now** (`POST /api/v1/backups/schedules/<id>/run`, `BackupService.run_schedule_now`; play button on Backups → Schedules). Queues one background run of the schedule outside its timetable. Upstream's table already had the button slot but nothing behind it. Tests: `backend/tests/test_backup_run_now.py`.
- **Back up databases that run in Docker** (`BackupService._dump_container_database`; Database schedule target `container:<container>/<database>`, or `container:<container>` for all databases). Upstream's database backups ran `mysqldump`/`pg_dump` on the host only, which fails for the usual Docker setup where the database is a service in the app's compose stack.
  - **How it runs:** the engine (MariaDB/MySQL/Percona or PostgreSQL) comes from the container image, and the dump runs inside the container with the credentials it was started with, so the panel stores no password.
  - **Safety:** a dump without its completion trailer is deleted rather than kept as a backup, and the container and database names are validated before anything runs.
  - Tests: `backend/tests/test_backup_container_db.py`.
- **Wildcard certificates renew** (`AdvancedSSLService.issue_wildcard_cert`, `DNS_CREDENTIALS_DIR`). certbot records the Cloudflare credentials path in the renewal config and reads it again at every renewal.
  - **The bug:** upstream wrote the token to a `/tmp` file and deleted it after issuing, so every wildcard certificate failed to renew 60 days later.
  - **The fix:** the file is now kept at `/etc/letsencrypt/serverkit-dns/cloudflare-<domain>.ini` (folder 700, file 600) and rewritten on every issuance, so a rotated token takes effect.
  - **Route 53 still has the same gap:** its keys are passed as environment variables that a renewal does not have.
  - Tests: `TestCloudflareCredentialsFile` in `backend/tests/test_advanced_ssl_hardening.py`.
- **Sidebar collapse toggle and an app top bar** (`Sidebar.jsx`, `AppTopBar.jsx`, `AccountMenu.jsx`, `layout/_app-topbar.scss`).
  - **Collapse / Expand:** a labelled button at the bottom-left of the sidebar (icon only in the rail), and Ctrl+B, switch between the full sidebar and a narrow icon rail. The page column stretches and narrows with it, both animated, and the choice is remembered. Upstream only offered the rail inside the user menu's Layout switcher.
  - **The rail stays narrow:** it no longer opens over the page on hover; each icon shows its name in a styled tooltip with an arrow beside the rail instead (`.rail-tip`).
  - **App top bar:** a slim bar across the page column carries the notification bell and the account menu at the top right. Page content starts below it, and full-page tools subtract its height.
  - **Where the account stays:** on mobile and in the top-bar layout there is no app top bar, so the account stays in the sidebar there.
  - **The "+" button:** the brand row's quick-create button is gone; the command palette it opened is still Ctrl+K, and the mobile top bar keeps its "+". The brand row's "Star on GitHub" link is gone too, and toasts pop up at the bottom right (above the status bar) instead of over the top bar.
  - **Setup health icon** (`SetupHealthIndicator.jsx`, admins): a shield beside the bell, green when setup is complete, amber/red with a count when items are open; it opens the Doctor. The dashboard's full-width "Setup complete" strip is gone, and its card only appears when there is something to review.
- **First/last name and profile picture** (`User.first_name`, `last_name`, `avatar`, migration `112_user_profile_name_avatar`; `app/utils/profile.py`; Settings → Profile).
  - **Name:** the top bar shows "First Last" (`display_name`) instead of the username when a name is set.
  - **Picture:** cropped to a square and scaled to 256 px in the browser, and stored as a data URL so an `<img>` can show it without the bearer token. The server accepts PNG, JPEG and WEBP only, checks the bytes match the declared type (no SVG: it can carry script), and caps it at 256 KB.
  - Tests: `backend/tests/test_user_profile.py`.
- **Remove notifications from the bell** (`NotificationDelivery.dismissed_at`, migration `113_notification_dismissed_at`; `POST /api/v1/notifications/inbox/<id>/dismiss` and `/inbox/clear`). Upstream could only mark notifications read, so they stayed in the bell forever.
  - **One at a time:** each notification has a remove (✕) button.
  - **All at once:** once nothing is unread, the header's "Mark all read" becomes "Clear all".
  - **History is kept:** removed notifications leave only the user's bell and inbox; the admin delivery log keeps them.
  - Tests: the dismiss/clear cases in `backend/tests/test_notification_bus.py`.
- **File manager code editor** (`components/file-manager/editor/`: `CodeEditor.jsx`, `EditorWorkspace.jsx`, `languages.js`; CodeMirror 6, lazy-loaded). Upstream edited files in a plain textarea.
  - **Editor:** open files show as tabs over the file list (Edit in the preview pane or the right-click menu). Syntax highlighting covers PHP, JS/TS, Python, HTML, CSS, JSON, YAML, Markdown, XML, SQL, nginx, shell, Dockerfile, .env/ini and TOML, with colours from theme tokens.
  - **Keys:** Ctrl+S saves, Ctrl+F finds and replaces, and there are Save all, Reload and line-wrap buttons, plus a line/column status bar.
  - **No lost work:** you're asked before closing a tab or the page with unsaved changes.
  - **Edit conflicts** (`FileService.write_file(expected_version)`): reads return the file's mtime as `version`, and a save against an older version is refused with 409 `file_changed`. The editor then offers Overwrite or Reload instead of silently overwriting an edit made on the server.
  - **Backups out of the web root** (`FileService._backup_before_edit`): upstream saved `<file>.bak` next to the file, so a web root served `wp-config.php.bak` or `index.php.bak` as plain text. Pre-save copies now go to `/var/backups/serverkit/file-edits/` (700/600), with the newest 10 kept per file.
  - **Shortcut fix:** the file manager's own shortcuts ignore contenteditable fields and are off while the editor is open. Without that, Backspace in the editor went up a folder and Delete deleted the selected files.
  - Tests: `backend/tests/test_file_editor_saves.py`.
- **File manager moves, copies and folder uploads** (`components/file-manager/transfer.js`). Upstream had copy/move endpoints but no way to use them from the file manager.
  - **Drag to move:** drag files or folders (the whole selection if the item is selected) onto a folder row or card, a folder in the sidebar tree, or a breadcrumb. Hold Ctrl or Alt to copy instead.
  - **Copy, cut, paste:** Ctrl+C / Ctrl+X / Ctrl+V, the selection bar's Copy and Cut, and right-click Copy, Cut and "Paste into folder". A bar shows what's waiting to be pasted. Copying into the same folder, or onto a taken name, makes "name (copy)", then "name (copy 2)".
  - **New file:** creates an empty file and opens it in the code editor.
  - **Folder upload:** an "Upload folder" button, and folders dragged in from the desktop keep their structure.
  - **No silent overwrite** (`POST /files/upload`): an existing file is replaced only with `overwrite=1`, otherwise 409 `file_exists`. The UI asks once per batch. A folder is never replaced by a file.
  - **Upload name fix:** the uploaded filename is used as a name only, so `../x` can't land outside the chosen folder.
  - **Into-itself guard** (`FileService._refuse_into_itself`): a folder can't be copied or moved inside itself. Upstream's copytree would recurse into its own output.
  - **Ownership** (`FileService.own_like_parent`, `make_parents`, `_own_like_source`): the panel runs as root, so upstream made every copy, upload, new file and new folder `root:root`, which a site or SFTP user then can't write. Now copies keep each item's owner. New files and folders, uploads included, take the owner of the folder they land in. A replaced upload keeps its owner.
  - **Symlinks are copied as links.** Upstream's copy followed them, which could pull in files from outside the allowed roots.
  - Tests: `backend/tests/test_file_transfers.py`.
- **File manager archives** (`services/archive_service.py`, `components/file-manager/archive.js`; `POST /files/compress`, `/files/extract`, `/files/download-zip`, `GET /files/jobs/<id>[/download]`). Upstream couldn't compress, extract, or download a folder.
  - **Compress** (selection bar, right-click): zip, tar.gz or tar, saved in the current folder. Zip has no portable links, so symlinks are skipped (and counted). Tar keeps them as links.
  - **Extract** (right-click on .zip/.tar/.tar.gz/.tgz/.tar.bz2/.tar.xz): into a folder, by default one named after the archive. If files would be replaced, nothing is written and you're asked first.
  - **Download as zip:** folders and multi-item selections download as one zip. It's built in `/var/tmp/serverkit-downloads` (700/600) and deleted once sent, or after an hour.
  - **Runs as a job** (`files.archive.*` kinds on the Queue Bus). Big archives outlast a request through Cloudflare, so the page polls the job, and the upload tray shows it while it runs. Users only see their own archive jobs.
  - **Hostile-archive rules** (checked for every entry before anything is written):
    - Absolute paths, drive letters and `..` are refused, and each resolved target must stay in the destination, even through existing symlinks.
    - Links, hardlinks and devices are skipped.
    - Limits: 20 GiB total, 200k entries, and a zip-bomb ratio check. Written bytes are capped at the declared size, and free disk space is checked first.
    - setuid/setgid/sticky bits are dropped. Owners come from the destination folder, never from the archive.
  - Tests: `backend/tests/test_file_archives.py`.
- **File manager trash, permissions grid, owners** (`services/file_trash_service.py`, `models/trashed_file.py`, migration `114_trashed_files`; `POST /files/trash`, `POST /files/chown`, `GET /files/owners`). Upstream deleted permanently, had only an octal box for permissions, and couldn't change owners.
  - **Delete goes to the Recycle Bin.** Items move into `/var/backups/serverkit/file-trash` (700, outside every file-manager root) and show up in Settings → Recycle bin as kind `file`. Restore moves the item back and refuses to overwrite anything now at that path. Purge, or the bin's 30-day expiry, deletes it for good. Restore and purge are admin-only, as for every bin item.
  - **Permanent delete** is Shift+Delete or right-click "Delete permanently". Remote agents and S3 have no bin, so deletes there stay permanent.
  - **Delete fixes:** a symlinked folder is removed as a link (upstream's rmtree refused it), a broken link can be deleted (upstream said "not found"), and a whole allowed root (`/var/www` itself) can't be deleted or trashed.
  - **Permissions grid:** read/write/execute checkboxes for owner, group and others, kept in sync with the octal box. For folders, "Also apply to everything inside" gives files their own mode (default 644), so 755 on a tree doesn't make every file executable. Symlinks inside are skipped: chmod follows links.
  - **Change owner** (admins only, right-click "Owner…"): user and group are picked from the server's accounts (root, uid ≥ 1000, and web service users), optionally recursive. Links are changed themselves (lchown), never their targets.
  - Tests: `backend/tests/test_file_trash_and_permissions.py`.
- **SMTP card works without the Email extension** (`ConnectionsHub.jsx` `coreSmtpRelay`). Upstream's Settings → Connections "SMTP relay" card only called `/api/v1/email/relay*`, which only exists when the Email extension is installed. Without it, Test and Save failed with "The method is not allowed for the requested URL".
  - **Fallback:** without the extension, the card edits the panel's own SMTP connection, the same one Settings → Notifications sends with. That is the core SMTP email provider, or, while there is none, the Notifications email config (`source: 'notifications'` on test). The checkbox becomes "Send the panel's email through this server".
  - **Test before saving** (`POST /api/v1/notifications/admin/email-providers/test`, `EmailProviderService.test_config`): checks the settings in the form without storing them. A blank password reuses the stored one.
  - **Edit in place** (`PUT /admin/email-providers/<id>` now also takes credential fields, sender and `is_active`; `EmailProviderService.update_settings`). A blank or masked password is kept. Providers now return their non-secret `settings` (host, port, username), never secrets.
  - Tests: `backend/tests/test_email_provider_edit_and_test_config.py`.
- **Scripts are executable in git** (`scripts/*.sh` that the installer chmods, and `serverkit`). Upstream tracks them as 644 and the installer chmods them, so the server's checkout always showed 12 mode-only changes.
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

Upstream lands work on `dev` and releases it to `main` later. `git log main..upstream/dev` shows what is waiting there, and it can be merged early the same way (done on 2026-10-21 for the Gitea lockdown and featured templates).

## Server-side changes not in this repo

These live on the server only. Re-check them after an update.

- Real visitor IPs behind Cloudflare are now written by the Cloudflare-only switch (`/etc/nginx/conf.d/serverkit-cloudflare-realip.conf`); the old hand-made `cloudflare-realip.conf` was removed.
- `/etc/cloud/cloud.cfg.d/99-keep-hostname.cfg` keeps the hostname `serverkit` across reboots on Oracle Cloud.
- Cloudflare allow lists for Fail2ban (`/etc/fail2ban/jail.d/zz-cloudflare-ignore.local`) and CrowdSec (`/etc/crowdsec/parsers/s02-enrich/cloudflare-whitelist.yaml`).
