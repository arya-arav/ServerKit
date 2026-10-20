#!/usr/bin/env bash
#
# ServerKit staging testbed — the BOX half. Runs on the target server, from the
# payload that `scripts/stage.sh push` uploaded to "<STAGE_DIR>.src".
#
#   bash <STAGE_DIR>.src/scripts/stage-remote.sh bootstrap [--expose]
#   bash <STAGE_DIR>.src/scripts/stage-remote.sh deploy
#   bash <STAGE_DIR>.src/scripts/stage-remote.sh verify  [--full]
#   bash <STAGE_DIR>.src/scripts/stage-remote.sh status
#   bash <STAGE_DIR>.src/scripts/stage-remote.sh logs
#   bash <STAGE_DIR>.src/scripts/stage-remote.sh destroy
#
# It is deliberately self-contained: it reads its config from the ".stage.env"
# that push wrote next to it, and every step is one command. That means an
# operator in any shell, or an agent driving through an SSH channel with
# per-command approval, can run the SAME steps command-by-command — no local
# half required, no credentials in the conversation.
#
# The staging instance is a PARALLEL install (own dir, own venv, own generated
# .env with fresh keys + its own SQLite file, own systemd unit
# `serverkit-staging`, bound to 127.0.0.1). The live panel on the box is never
# touched in the default parallel mode. It reuses scripts/update.sh's deploy
# functions (venv rebuild, migrate, health wait) instead of reimplementing them.
#
# Source-able: sourcing defines every function and returns before the run block
# (the BASH_SOURCE guard), so scripts/test/test_stage.sh can unit-test the pure
# helpers (target guard, .env rendering, bind host) with no box.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Locate ourselves + the shared pure-helper lib (co-uploaded in the payload).
# ---------------------------------------------------------------------------
SR_SELF="${BASH_SOURCE[0]}"
SR_SCRIPT_DIR="$(cd "$(dirname "$SR_SELF")" && pwd)"
SR_SRC="$(cd "$SR_SCRIPT_DIR/.." && pwd)"   # = <STAGE_DIR>.src (payload root)
# shellcheck source=lib/stage-common.sh
source "$SR_SCRIPT_DIR/lib/stage-common.sh"

# ---------------------------------------------------------------------------
# Terminal styling (mirrors stage.sh; degrades with no TTY / NO_COLOR).
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    ESC=$'\033'; RST="${ESC}[0m"; BLD="${ESC}[1m"
    C_OK="${ESC}[38;5;42m"; C_WARN="${ESC}[38;5;220m"
    C_ERR="${ESC}[38;5;203m"; C_LINK="${ESC}[38;5;81m"; C_FOG="${ESC}[38;5;244m"
else
    RST=''; BLD=''; C_OK=''; C_WARN=''; C_ERR=''; C_LINK=''; C_FOG=''
fi
sr_good() { printf '  %s✔%s %s\n' "$C_OK"   "$RST" "$1"; }
sr_warn() { printf '  %s▴%s %s\n' "$C_WARN" "$RST" "$1" >&2; }
sr_step() { printf '  %s❯%s %s\n' "$C_LINK" "$RST" "$1"; }
sr_info() { printf '  %s•%s %s\n' "$C_FOG"  "$RST" "$1"; }
sr_die()  { printf '  %s✘%s %s\n' "$C_ERR" "$RST" "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Overridable paths (defaults are the real box; tests point them at fixtures).
# ---------------------------------------------------------------------------
SR_LIVE_DIR="${SERVERKIT_DIR:-/opt/serverkit}"
SR_SYSTEMD_DIR="${SERVERKIT_SYSTEMD_DIR:-/etc/systemd/system}"
SR_UNIT="${SR_UNIT_NAME:-serverkit-staging}"
SR_LOG_DIR="${SR_LOG_DIR_OVERRIDE:-/var/log/serverkit-staging}"

# Config values loaded from .stage.env by sr_load_config.
STAGE_DIR=""; STAGE_PORT=""; STAGE_MODE=""; STAGE_EXPOSE=""
STAGE_PROFILE=""; STAGE_COMMIT=""; STAGE_PAYLOAD_MODE=""
# Derived.
SR_VENV=""; SR_ENV_FILE=""; SR_DB=""; SR_HEALTH_URL=""

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no host mutation, no network).
# ---------------------------------------------------------------------------

# Interpret a flag value as boolean-ish. Echoes 1 for truthy, 0 otherwise.
sr_bool() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) printf '1' ;;
        *)             printf '0' ;;
    esac
}

# The bind host for the staging backend. Loopback unless deliberately exposed —
# verification runs on-box, so nothing half-tested is reachable off the box by
# default (Decision 3). Arg: exposed (0|1).
sr_bind_host() {
    if [ "$(sr_bool "${1:-0}")" = "1" ]; then printf '0.0.0.0'; else printf '127.0.0.1'; fi
}

# Read a KEY=value from an env file (last wins), value unquoted. Empty if absent.
sr_env_get() {
    local key="$1" file="$2"
    [ -f "$file" ] || return 0
    sed -n "s/^${key}=//p" "$file" 2>/dev/null | tail -1
}

# Guard the target directory (Decision 3): a parallel staging deploy must NEVER
# land on the live install dir, and must never nuke its own uploaded payload.
# Returns 0 when the target is safe, 1 (with a reason on stderr) when refused.
# Args: <target_dir> <live_dir> <mode> <src_dir>
sr_guard_target() {
    local target="$1" live="$2" mode="$3" src="$4"
    # Compare on normalized paths where possible; fall back to string compare
    # (the dirs may not exist yet on a first bootstrap).
    local nt nl
    nt="$(cd "$target" 2>/dev/null && pwd || printf '%s' "$target")"
    nl="$(cd "$live" 2>/dev/null && pwd || printf '%s' "$live")"
    if [ "$nt" = "$src" ]; then
        printf 'STAGE_DIR equals the uploaded payload dir (%s) — refusing to overwrite it.\n' "$src" >&2
        return 1
    fi
    if [ "$nt" = "$nl" ] && [ "$mode" != "replace" ]; then
        printf 'STAGE_DIR (%s) is the LIVE install dir. Refusing in parallel mode; set STAGE_MODE=replace only on a scratch box.\n' "$nt" >&2
        return 1
    fi
    return 0
}

# Render the staging .env content to stdout. PURE — the caller supplies the
# already-resolved keys so re-bootstraps preserve them (converge, don't rotate).
# Args: <secret_key> <jwt_secret> <enc_key> <db_path> <port> <bind_host>
sr_render_env() {
    local secret="$1" jwt="$2" enc="$3" db="$4" port="$5" bind="$6"
    cat <<EOF
# ServerKit STAGING instance — generated by stage-remote.sh bootstrap.
# Disposable: destroy + re-bootstrap to reset. NOT the live panel.

SECRET_KEY=$secret
JWT_SECRET_KEY=$jwt
SERVERKIT_ENCRYPTION_KEY=$enc

# Own SQLite file — never shares the live panel's database.
DATABASE_URL=sqlite:///$db

# Staging marker — the panel renders a banner and /api/v1/system/health echoes
# staging:true so a verdict can prove which instance answered.
SERVERKIT_STAGING=1

# Loopback-bound by default; CORS covers the on-box verification origin.
CORS_ORIGINS=http://127.0.0.1:$port,http://localhost:$port
HOST=$bind
PORT=$port

# Plain HTTP on loopback — HTTPS stays optional and is irrelevant on-box.
SERVERKIT_SSL_MODE=insecure
FLASK_ENV=production
EOF
}

# ---------------------------------------------------------------------------
# Config loader — reads the .stage.env push wrote next to this script.
# ---------------------------------------------------------------------------
sr_load_config() {
    local cfg="$SR_SRC/.stage.env"
    [ -f "$cfg" ] || sr_die "No .stage.env at $cfg — run 'stage.sh push <profile>' first."
    # shellcheck source=/dev/null
    source "$cfg"
    [ -n "$STAGE_DIR" ]  || sr_die ".stage.env missing STAGE_DIR"
    [ -n "$STAGE_PORT" ] || sr_die ".stage.env missing STAGE_PORT"
    STAGE_MODE="${STAGE_MODE:-parallel}"
    STAGE_EXPOSE="$(sr_bool "${STAGE_EXPOSE:-0}")"
    SR_VENV="$STAGE_DIR/venv"
    SR_ENV_FILE="$STAGE_DIR/.env"
    SR_DB="$STAGE_DIR/backend/instance/serverkit.db"
    SR_HEALTH_URL="http://127.0.0.1:$STAGE_PORT/api/v1/system/health"
}

# Optionally source the init-system abstraction for systemd ops (daemon-reload,
# enable, start/stop, is-active). Best-effort: absent on a stripped payload.
sr_source_init() {
    [ -f "$SR_SCRIPT_DIR/lib/init.sh" ] || return 0
    # shellcheck source=lib/init.sh
    source "$SR_SCRIPT_DIR/lib/init.sh"
}

# Source scripts/update.sh purely for its deploy functions (rebuild_virtualenv,
# require_venv, migrate_database, wait_for_service, locate_python). update.sh is
# source-able (BASH_SOURCE guard returns before its run block), but its TOP-LEVEL
# argument parser reads "$@" — so we MUST clear positional params first, or it
# would see our step name ("deploy") as an unknown option and exit 1. We source
# it inside a function with `set --` cleared; function definitions land globally.
sr_source_update() {
    [ -f "$SR_SCRIPT_DIR/update.sh" ] || sr_die "update.sh missing from payload — cannot deploy."
    set --
    # Point update.sh's derived paths at the staging install (harmless globals;
    # the functions we call are all explicitly parameterized by dir anyway).
    export SERVERKIT_DIR="$STAGE_DIR"
    export SERVERKIT_NO_LOG=1
    # shellcheck source=update.sh
    source "$SR_SCRIPT_DIR/update.sh"
}

# Wait until the staging backend answers its health endpoint on the loopback
# port (proves the process actually came up, not just that systemd says active).
sr_wait_health() {
    local timeout="${1:-45}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if curl -sf --max-time 5 "$SR_HEALTH_URL" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

# Restart (or start) the staging systemd unit. Distinct unit name → the live
# `serverkit` service is never touched.
sr_restart_unit() {
    command -v systemctl >/dev/null 2>&1 || { sr_warn "systemctl absent — cannot manage $SR_UNIT"; return 1; }
    systemctl restart "$SR_UNIT" 2>/dev/null || systemctl start "$SR_UNIT" 2>/dev/null
}

# Record the deployed commit + payload mode so `status` can report drift and the
# verdict can attribute a green run to an exact tree.
sr_marker_file() { printf '%s/.serverkit-staging-deployed' "$STAGE_DIR"; }
sr_write_marker() {
    printf 'STAGE_DEPLOYED_COMMIT=%s\nSTAGE_DEPLOYED_MODE=%s\n' \
        "${STAGE_COMMIT:-unknown}" "${STAGE_PAYLOAD_MODE:-head}" > "$(sr_marker_file)"
}

# ---------------------------------------------------------------------------
# bootstrap — converge the staging instance: dir, generated .env (fresh keys,
# own SQLite, staging port, STAGING=1), systemd unit from the shipped template,
# loopback bind. Idempotent: re-running preserves keys and reconverges the unit.
# ---------------------------------------------------------------------------
sr_bootstrap() {
    local expose="${1:-0}"
    [ "$(sr_bool "$expose")" = "1" ] && STAGE_EXPOSE=1

    printf '\n  %s%sServerKit staging · bootstrap%s  %s(%s, port %s, %s)%s\n\n' \
        "$BLD" "$C_LINK" "$RST" "$C_FOG" "${STAGE_PROFILE:-staging}" "$STAGE_PORT" "$STAGE_MODE" "$RST"

    # Decision 3 guardrail — never clobber the live panel or the payload.
    sr_guard_target "$STAGE_DIR" "$SR_LIVE_DIR" "$STAGE_MODE" "$SR_SRC" \
        || sr_die "Refused to bootstrap: unsafe STAGE_DIR."
    [ "$STAGE_MODE" = "replace" ] && sr_warn "STAGE_MODE=replace — deploying OVER $STAGE_DIR (scratch box only)."

    sr_step "Preparing directories under $STAGE_DIR"
    mkdir -p "$STAGE_DIR/backend/instance" "$SR_LOG_DIR"

    # Keys: reuse existing (converge) or mint fresh so a re-bootstrap does not
    # invalidate the running instance's sessions/encrypted rows.
    local secret jwt enc bind
    secret="$(sr_env_get SECRET_KEY "$SR_ENV_FILE")"
    jwt="$(sr_env_get JWT_SECRET_KEY "$SR_ENV_FILE")"
    enc="$(sr_env_get SERVERKIT_ENCRYPTION_KEY "$SR_ENV_FILE")"
    if [ -z "$secret" ]; then secret="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"; fi
    if [ -z "$jwt" ]; then jwt="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"; fi
    if [ -z "$enc" ]; then
        enc="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())' 2>/dev/null || openssl rand -base64 32)"
    fi
    bind="$(sr_bind_host "$STAGE_EXPOSE")"

    sr_step "Writing generated .env (fresh keys reused across re-bootstraps)"
    sr_render_env "$secret" "$jwt" "$enc" "$SR_DB" "$STAGE_PORT" "$bind" > "$SR_ENV_FILE"
    chmod 600 "$SR_ENV_FILE" 2>/dev/null || true

    # systemd unit from the shipped template (same one install/update render),
    # re-pointed at the staging dir/venv/port/log and (unless exposed) rebound
    # to loopback. A distinct unit name means the live `serverkit` unit is
    # never touched.
    sr_install_unit "$bind"

    sr_good "Bootstrapped staging instance ($SR_UNIT) at $STAGE_DIR"
    sr_info "Next: stage-remote.sh deploy"
}

# Render + install the systemd unit for the staging instance, then reload the
# daemon. Loopback rebind is a post-render sed so the shared template keeps its
# single 0.0.0.0 placeholder untouched.
sr_install_unit() {
    local bind="$1"
    local template="$SR_SRC/templates/serverkit-backend.service.in"
    local unit_path="$SR_SYSTEMD_DIR/$SR_UNIT.service"
    [ -f "$template" ] || sr_die "Service template missing: $template"

    sr_step "Installing systemd unit $SR_UNIT.service"
    mkdir -p "$SR_SYSTEMD_DIR"
    sed -e "s|@SERVERKIT_DIR@|$STAGE_DIR|g" \
        -e "s|@SERVERKIT_VENV_DIR@|$SR_VENV|g" \
        -e "s|@PORT@|$STAGE_PORT|g" \
        -e "s|@USER@|root|g" \
        -e "s|@LOG_DIR@|$SR_LOG_DIR|g" \
        "$template" > "$unit_path"
    # Loopback rebind (default). Under --expose we keep 0.0.0.0 + open a firewall
    # rule in a later step; the banner + verdict make the exposure obvious.
    if [ "$bind" = "127.0.0.1" ]; then
        sed -i "s|-b 0.0.0.0:$STAGE_PORT|-b 127.0.0.1:$STAGE_PORT|g" "$unit_path"
    fi
    # Distinguish the unit description + journal identifier from the live panel.
    sed -i "s|Description=ServerKit Backend API|Description=ServerKit STAGING Backend (${STAGE_PROFILE:-staging})|" "$unit_path"
    sed -i "s|SyslogIdentifier=serverkit|SyslogIdentifier=$SR_UNIT|" "$unit_path"

    sr_source_init
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload 2>/dev/null || sr_warn "daemon-reload failed — unit may be stale"
        systemctl enable "$SR_UNIT" 2>/dev/null || true
    fi
}

# Seed a deterministic staging admin (admin/admin) so verify's login check is
# reproducible. Idempotent: skips if any admin already exists. Mirrors the
# `serverkit create-admin` CLI path (set_password + complete_setup), bypassing
# the register route's 8-char rule because these are throwaway staging creds we
# control. Best-effort: a seed failure only weakens the login check, not deploy.
sr_seed_admin() {
    [ -x "$SR_VENV/bin/python" ] || return 0
    ( cd "$STAGE_DIR/backend" && DATABASE_URL="sqlite:///$SR_DB" FLASK_ENV=production \
      "$SR_VENV/bin/python" - <<'PY'
import os
os.environ.setdefault("FLASK_ENV", "production")
try:
    from app import create_app, db
    from app.models import User
    app = create_app()
    with app.app_context():
        if not User.query.filter_by(username="admin").first():
            u = User(email="admin@staging.local", username="admin", role="admin", is_active=True)
            u.set_password("admin")
            db.session.add(u)
            db.session.commit()
            try:
                from app.services.settings_service import SettingsService
                SettingsService.complete_setup(user_id=u.id)
            except Exception:
                pass
            print("seeded staging admin")
        else:
            print("staging admin already present")
except Exception as exc:  # never fail the deploy over a seed
    print(f"admin seed skipped: {exc}")
PY
    ) 2>/dev/null && sr_info "Staging admin ensured (admin/admin)" || sr_warn "admin seed skipped"
}

# ---------------------------------------------------------------------------
# deploy — overlay the pushed source, rebuild the venv + frontend on-box
# (exercising the REAL build rails Windows dev can't), migrate, and restart.
# A failure BEFORE the restart leaves the previously-deployed staging instance
# running: build/migrate all happen first; the frontend builds into a temp dir
# and is swapped in only on success, so a broken build never replaces the served
# bundle.
# ---------------------------------------------------------------------------
sr_deploy() {
    sr_load_config
    sr_guard_target "$STAGE_DIR" "$SR_LIVE_DIR" "$STAGE_MODE" "$SR_SRC" \
        || sr_die "Refused to deploy: unsafe STAGE_DIR."
    [ -f "$SR_ENV_FILE" ] || sr_die "Not bootstrapped ($SR_ENV_FILE missing) — run bootstrap first."

    printf '\n  %s%sServerKit staging · deploy%s  %s(%s @ %s)%s\n\n' \
        "$BLD" "$C_LINK" "$RST" "$C_FOG" "${STAGE_PROFILE:-staging}" "${STAGE_COMMIT:-?}" "$RST"

    sr_source_update

    # 1) Overlay the pushed source onto the staging dir, preserving the generated
    #    .env, the instance DB, the venv and the current dist (the exclusion list
    #    guards them from --delete).
    sr_step "Overlaying pushed source onto $STAGE_DIR"
    command -v rsync >/dev/null 2>&1 || sr_die "rsync required on the box for deploy."
    local excludes=()
    while IFS= read -r line; do excludes+=("$line"); done < <(stage_rsync_exclude_args)
    rsync -a --delete "${excludes[@]}" "$SR_SRC/" "$STAGE_DIR/" \
        || sr_die "source overlay failed — previous staging deploy still running."

    # 2) Python deps: rebuild the venv if absent, else install requirements into
    #    the existing one (fast, picks up requirements.txt changes).
    if [ -x "$SR_VENV/bin/python" ]; then
        sr_step "Updating Python dependencies"
        ( # shellcheck source=/dev/null
          source "$SR_VENV/bin/activate"
          pip install -q -r "$STAGE_DIR/backend/requirements.txt" \
            && pip install -q gunicorn ) \
            || sr_die "pip install failed — previous staging deploy still running."
    else
        sr_step "Building the Python virtualenv"
        rebuild_virtualenv "$SR_VENV" || sr_die "venv build failed — previous staging deploy still running."
    fi

    # 3) Frontend: build on-box (the real build rails) into a temp dir, then swap
    #    — a failed build never replaces the served bundle.
    sr_step "Building the frontend on-box (npm ci && npm run build)"
    command -v npm >/dev/null 2>&1 || sr_die "npm required on the box for deploy."
    (
        cd "$STAGE_DIR/frontend"
        npm ci --prefer-offline --no-audit --no-fund
        rm -rf dist.staging-new
        NODE_OPTIONS="--max-old-space-size=1024" npm run build -- --outDir dist.staging-new
    ) || sr_die "frontend build failed — previous staging deploy still running (bundle untouched)."
    rm -rf "$STAGE_DIR/frontend/dist"
    mv "$STAGE_DIR/frontend/dist.staging-new" "$STAGE_DIR/frontend/dist"

    # 4) Migrate the staging DB (its own SQLite file; never the live panel's).
    migrate_database "$STAGE_DIR" || sr_die "migration failed — previous staging deploy still running."

    # 4b) Seed the staging admin/admin so verify's login check is deterministic.
    sr_seed_admin

    # 5) Restart the staging unit and wait for the health endpoint. Only now is
    #    the running instance replaced.
    sr_step "Restarting $SR_UNIT and waiting for health"
    sr_restart_unit || sr_warn "could not restart $SR_UNIT via systemctl"
    if sr_wait_health 60; then
        sr_write_marker
        sr_good "Staging deployed and healthy on 127.0.0.1:$STAGE_PORT (${STAGE_COMMIT:-?})"
        sr_info "Next: stage-remote.sh verify"
    else
        sr_die "Staging did not become healthy within 60s. Logs: journalctl -u $SR_UNIT -n 100"
    fi
}

# ---------------------------------------------------------------------------
# verify — the layered, machine-readable check suite (Decision 5).
# ---------------------------------------------------------------------------
# Checks run in order; each appends a "name=state" line to SR_CHECKS and a table
# row. A `fail` state flips SR_FAIL; `skip` is honest (never counts as a pass).
# The run ends with one `VERDICT {json}` line and a stable exit code (0 pass,
# 1 fail). On failure a diagnostic bundle is collected for `logs` to surface.
SR_CHECKS=""
SR_FAIL=0
sr_check() {
    local name="$1" state="$2" note="${3:-}"
    SR_CHECKS="$SR_CHECKS$name=$state"$'\n'
    local icon
    case "$state" in
        pass) icon="$C_OK✔$RST" ;;
        skip) icon="$C_WARN∼$RST" ;;
        *)    icon="$C_ERR✘$RST"; SR_FAIL=1 ;;
    esac
    printf '  %s %-16s %s%s\n' "$icon" "$name" "$state" "${note:+  ($note)}"
}

# curl helper against the staging backend (loopback).
sr_curl() { curl -s --max-time 8 "$@"; }

sr_verify() {
    local full="${1:-0}"
    sr_load_config
    [ -f "$SR_ENV_FILE" ] || sr_die "Not bootstrapped ($SR_ENV_FILE missing)."

    printf '\n  %s%sServerKit staging · verify%s  %s(%s @ %s, %s)%s\n\n' \
        "$BLD" "$C_LINK" "$RST" "$C_FOG" "${STAGE_PROFILE:-staging}" \
        "${STAGE_COMMIT:-?}" "${STAGE_PAYLOAD_MODE:-head}" "$RST"

    local base="http://127.0.0.1:$STAGE_PORT"

    # 1) health endpoint + staging marker (proves WHICH instance answered).
    local health
    health="$(sr_curl "$base/api/v1/system/health" || true)"
    if printf '%s' "$health" | grep -q '"status":"healthy"' \
       || printf '%s' "$health" | grep -q '"status": "healthy"'; then
        if printf '%s' "$health" | grep -qE '"staging":[[:space:]]*true'; then
            sr_check health pass "staging:true"
        else
            sr_check health fail "healthy but staging flag not set — wrong instance?"
        fi
    else
        sr_check health fail "no healthy response on $base"
    fi

    # 2) login (staging admin/admin — seeded at deploy).
    local login
    login="$(sr_curl -X POST -H 'Content-Type: application/json' \
        -d '{"email":"admin","password":"admin"}' "$base/api/v1/auth/login" || true)"
    if printf '%s' "$login" | grep -q '"access_token"'; then
        sr_check login pass
    else
        sr_check login fail "admin/admin login did not return a token"
    fi

    # 3) migration head matches the deployed tree.
    sr_check_migration_head

    # 4) frontend serves (index + one hashed asset).
    sr_check_frontend "$base"

    # 5) Linux-reality probes Windows dev cannot run.
    sr_check_nginx
    sr_check_systemd
    sr_check_journald

    # 6) docker scratch-container round-trip (gated on docker presence).
    sr_check_docker

    # 7) agent long-poll e2e against the deployed code (gated on pytest present).
    sr_check_agent_poll

    # --full: the whole on-box pytest suite.
    if [ "$(sr_bool "$full")" = "1" ]; then
        sr_check_pytest_full
    fi

    # Emit the verdict + collect a bundle on failure.
    printf '\n'
    local checks_json pass=1
    checks_json="$(printf '%s' "$SR_CHECKS" | stage_checks_to_json)"
    [ "$SR_FAIL" = "0" ] || pass=0
    stage_format_verdict "${STAGE_PROFILE:-staging}" "${STAGE_COMMIT:-unknown}" \
        "${STAGE_PAYLOAD_MODE:-head}" "$checks_json" "$pass"
    if [ "$pass" = "0" ]; then
        sr_collect_bundle
        return 1
    fi
    return 0
}

sr_check_migration_head() {
    if [ ! -x "$SR_VENV/bin/python" ]; then
        sr_check migration skip "no venv"
        return
    fi
    # Revision IDs are the descriptive slugs ServerKit uses (e.g.
    # ``074_cf_ops_changes``), not Alembic's default hex hashes — so take the
    # first whitespace-delimited token of the first non-empty line (strips the
    # trailing `` (head)``). ``|| true`` keeps an empty result from tripping
    # ``set -e`` and skipping the verdict.
    local cur head
    cur="$( cd "$STAGE_DIR/backend" && \
        DATABASE_URL="sqlite:///$SR_DB" FLASK_ENV=production \
        "$SR_VENV/bin/flask" db current 2>/dev/null | awk 'NF{print $1; exit}' || true )"
    head="$( cd "$STAGE_DIR/backend" && \
        DATABASE_URL="sqlite:///$SR_DB" FLASK_ENV=production \
        "$SR_VENV/bin/flask" db heads 2>/dev/null | awk 'NF{print $1; exit}' || true )"
    if [ -n "$cur" ] && [ "$cur" = "$head" ]; then
        sr_check migration pass "$cur"
    else
        sr_check migration fail "current=$cur head=$head"
    fi
}

sr_check_frontend() {
    local base="$1" index asset
    index="$(sr_curl "$base/" || true)"
    if ! printf '%s' "$index" | grep -qi '<div id="root"'; then
        sr_check frontend fail "index.html not served"
        return
    fi
    # Pull one hashed asset reference out of the SPA shell and fetch it.
    asset="$(printf '%s' "$index" | grep -oE '/assets/[^"'"'"']+\.(js|css)' | head -1)"
    if [ -z "$asset" ]; then
        sr_check frontend fail "no hashed /assets/ reference in index.html"
        return
    fi
    if sr_curl -o /dev/null -w '%{http_code}' "$base$asset" | grep -q '200'; then
        sr_check frontend pass "$asset"
    else
        sr_check frontend fail "hashed asset $asset did not serve 200"
    fi
}

sr_check_nginx() {
    if ! command -v nginx >/dev/null 2>&1; then
        sr_check nginx-config skip "nginx absent"
        return
    fi
    if nginx -t >/dev/null 2>&1; then
        sr_check nginx-config pass
    else
        sr_check nginx-config fail "nginx -t failed"
    fi
}

sr_check_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        sr_check systemd-unit skip "systemctl absent"
        return
    fi
    if systemctl is-active --quiet "$SR_UNIT" 2>/dev/null; then
        sr_check systemd-unit pass "$SR_UNIT active"
    else
        sr_check systemd-unit fail "$SR_UNIT not active"
    fi
}

sr_check_journald() {
    if ! command -v journalctl >/dev/null 2>&1; then
        sr_check journald skip "journalctl absent"
        return
    fi
    if journalctl -u "$SR_UNIT" -n 1 --no-pager >/dev/null 2>&1; then
        sr_check journald pass
    else
        sr_check journald fail "cannot read the staging unit journal"
    fi
}

# Docker: a real container lifecycle (run → running? → remove) using an image
# already on the box, so no network pull is needed. Absent docker / no local
# image is an honest skip — never a silent pass.
sr_check_docker() {
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        sr_check docker skip "docker not available"
        return
    fi
    local img probe="serverkit-staging-probe"
    img="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -v '<none>' | head -1)"
    if [ -z "$img" ]; then
        sr_check docker skip "no local image to round-trip"
        return
    fi
    docker rm -f "$probe" >/dev/null 2>&1 || true
    if docker run -d --name "$probe" --entrypoint sleep "$img" 30 >/dev/null 2>&1 \
       && [ "$(docker inspect -f '{{.State.Running}}' "$probe" 2>/dev/null)" = "true" ]; then
        docker rm -f "$probe" >/dev/null 2>&1 || true
        sr_check docker pass "round-trip via $img"
    else
        docker rm -f "$probe" >/dev/null 2>&1 || true
        sr_check docker fail "scratch container did not come up"
    fi
}

# Agent long-poll e2e: run the existing test against the DEPLOYED code in the
# staging venv. Gated on pytest being importable (skip reported otherwise).
sr_check_agent_poll() {
    local test_file="$STAGE_DIR/backend/tests/test_agent_poll_e2e.py"
    if [ ! -x "$SR_VENV/bin/python" ] || ! "$SR_VENV/bin/python" -c 'import pytest' >/dev/null 2>&1; then
        sr_check agent-poll skip "pytest not installed in staging venv"
        return
    fi
    [ -f "$test_file" ] || { sr_check agent-poll skip "test file absent"; return; }
    if ( cd "$STAGE_DIR/backend" && FLASK_ENV=testing "$SR_VENV/bin/python" -m pytest -q "$test_file" ) >/dev/null 2>&1; then
        sr_check agent-poll pass
    else
        sr_check agent-poll fail "agent long-poll e2e failed against deployed code"
    fi
}

sr_check_pytest_full() {
    if [ ! -x "$SR_VENV/bin/python" ] || ! "$SR_VENV/bin/python" -c 'import pytest' >/dev/null 2>&1; then
        sr_check pytest skip "pytest not installed in staging venv"
        return
    fi
    sr_step "Running the full on-box pytest suite (--full) — this can take a while"
    if ( cd "$STAGE_DIR/backend" && FLASK_ENV=testing "$SR_VENV/bin/python" -m pytest -q ) >/dev/null 2>&1; then
        sr_check pytest pass
    else
        sr_check pytest fail "on-box pytest suite failed"
    fi
}

# Collect a fetchable failure bundle (the vm-install post-mortem pattern):
# journal + unit status + nginx test + a redacted .env, tarred to a fixed path.
sr_bundle_path() { printf '%s.stage-bundle.tar.gz' "$STAGE_DIR"; }
sr_collect_bundle() {
    local tmp; tmp="$(mktemp -d)"
    { command -v journalctl >/dev/null 2>&1 && journalctl -u "$SR_UNIT" -n 300 --no-pager; } \
        > "$tmp/journal.log" 2>&1 || true
    { command -v systemctl >/dev/null 2>&1 && systemctl status "$SR_UNIT" --no-pager -l; } \
        > "$tmp/unit-status.txt" 2>&1 || true
    { command -v nginx >/dev/null 2>&1 && nginx -t; } > "$tmp/nginx-t.txt" 2>&1 || true
    # Redact secrets from the .env copy.
    sed -E 's/^(SECRET_KEY|JWT_SECRET_KEY|SERVERKIT_ENCRYPTION_KEY)=.*/\1=<redacted>/' \
        "$SR_ENV_FILE" > "$tmp/env.redacted" 2>/dev/null || true
    tar czf "$(sr_bundle_path)" -C "$tmp" . 2>/dev/null || true
    rm -rf "$tmp"
    sr_warn "Failure bundle: $(sr_bundle_path)  (fetch with: stage.sh logs <profile>)"
}

# ---------------------------------------------------------------------------
# status — deployed commit + running state (drift is judged locally by stage.sh).
# ---------------------------------------------------------------------------
sr_status() {
    sr_load_config
    local deployed="unknown" active="unknown" healthy="down"
    [ -f "$(sr_marker_file)" ] && deployed="$(sr_env_get STAGE_DEPLOYED_COMMIT "$(sr_marker_file)")"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl is-active --quiet "$SR_UNIT" 2>/dev/null && active="active" || active="inactive"
    fi
    curl -sf --max-time 5 "$SR_HEALTH_URL" >/dev/null 2>&1 && healthy="ok"
    # Machine-readable lines stage.sh parses, plus a human summary.
    printf 'STAGE_DEPLOYED_COMMIT=%s\n' "${deployed:-unknown}"
    printf 'STAGE_UNIT_ACTIVE=%s\n' "$active"
    printf 'STAGE_HEALTH=%s\n' "$healthy"
    sr_info "staging $SR_UNIT: deployed=${deployed:-unknown} unit=$active health=$healthy"
}

# ---------------------------------------------------------------------------
# logs — surface the last failure bundle + recent journal.
# ---------------------------------------------------------------------------
sr_logs() {
    sr_load_config
    local bundle; bundle="$(sr_bundle_path)"
    if [ -f "$bundle" ]; then
        printf 'STAGE_BUNDLE=%s\n' "$bundle"
        sr_info "Failure bundle present: $bundle"
    else
        sr_info "No failure bundle on disk (last verify passed or none run yet)."
    fi
    if command -v journalctl >/dev/null 2>&1; then
        printf '\n----- journalctl -u %s (last 80) -----\n' "$SR_UNIT"
        journalctl -u "$SR_UNIT" -n 80 --no-pager 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# destroy — remove the staging instance completely (Decision 6). Disposable.
# ---------------------------------------------------------------------------
sr_destroy() {
    sr_load_config
    # Never let a mis-set profile destroy the live install.
    sr_guard_target "$STAGE_DIR" "$SR_LIVE_DIR" "$STAGE_MODE" "$SR_SRC" \
        || sr_die "Refused to destroy: STAGE_DIR resolves to the live install."

    printf '\n  %s%sServerKit staging · destroy%s  %s(%s)%s\n\n' \
        "$BLD" "$C_LINK" "$RST" "$C_FOG" "${STAGE_PROFILE:-staging}" "$RST"

    if command -v systemctl >/dev/null 2>&1; then
        sr_step "Stopping + removing $SR_UNIT"
        systemctl stop "$SR_UNIT" 2>/dev/null || true
        systemctl disable "$SR_UNIT" 2>/dev/null || true
        rm -f "$SR_SYSTEMD_DIR/$SR_UNIT.service"
        systemctl daemon-reload 2>/dev/null || true
    fi

    # Close an --expose firewall rule if one was opened.
    if [ "$(sr_bool "${STAGE_EXPOSE:-0}")" = "1" ] && [ -f "$SR_SCRIPT_DIR/lib/firewall.sh" ]; then
        # shellcheck source=lib/firewall.sh
        source "$SR_SCRIPT_DIR/lib/firewall.sh"
        local backend; backend="$(firewall_detect 2>/dev/null || echo none)"
        [ "$backend" != "none" ] && firewall_close "$backend" "$STAGE_PORT/tcp" 2>/dev/null || true
    fi

    # Remove the CANONICAL payload dir derived from STAGE_DIR (<STAGE_DIR>.src),
    # never the script's own resolved location — so destroy can never be tricked
    # into deleting the repo/live tree just because it was invoked from there.
    local payload; payload="$(stage_src_dir "$STAGE_DIR")"
    sr_step "Removing $STAGE_DIR, $payload and logs"
    rm -rf "$STAGE_DIR" "$payload" "$SR_LOG_DIR" "$(sr_bundle_path)"
    sr_good "Staging instance destroyed."
}

# ---------------------------------------------------------------------------
# Step router.
# ---------------------------------------------------------------------------
sr_main() {
    local step="${1:-}"; shift || true
    case "$step" in
        bootstrap)
            sr_load_config
            local expose=0
            case "${1:-}" in --expose) expose=1 ;; esac
            sr_bootstrap "$expose"
            ;;
        deploy)
            sr_deploy
            ;;
        verify)
            local vfull=0
            case "${1:-}" in --full) vfull=1 ;; esac
            sr_verify "$vfull"
            ;;
        status)
            sr_status
            ;;
        logs)
            sr_logs
            ;;
        destroy)
            sr_destroy
            ;;
        ""|-h|--help)
            printf 'Usage: stage-remote.sh {bootstrap|deploy|verify|status|logs|destroy} [flags]\n'
            [ -z "$step" ] && exit 1 || exit 0
            ;;
        *)
            sr_die "Unknown step: $step"
            ;;
    esac
}

# Sourcing defines functions and returns before the run block (BASH_SOURCE
# guard) — same discipline as scripts/update.sh + stage.sh.
[ "${BASH_SOURCE[0]}" = "${0}" ] || return 0

sr_main "$@"
