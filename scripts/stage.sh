#!/usr/bin/env bash
#
# ServerKit staging testbed — validate the LOCAL checkout on a real box, one
# command, no prompts, no credentials in the repo or the chat.
#
#   bash scripts/stage.sh push   <profile> [--dirty] [--expose] [--dry-run]
#   bash scripts/stage.sh deploy <profile> [--dry-run]
#   bash scripts/stage.sh verify <profile> [--full] [--dry-run]
#   bash scripts/stage.sh status <profile>
#   bash scripts/stage.sh logs   <profile>
#   bash scripts/stage.sh destroy <profile>
#
# This is the LOCAL half. It resolves a git-ignored target profile
# (deploy/targets/<profile>.env), builds a payload from the local checkout
# (committed HEAD by default, --dirty for the working tree), ships it to the box
# over plain SSH config, and then drives the BOX half (scripts/stage-remote.sh)
# command-by-command over ssh. Every box-side step is a self-contained
# `ssh <alias> bash <remote>/stage-remote.sh <step>` — so an operator in any
# shell, or an agent driving through an SSH channel with per-command approval,
# can run the exact same steps with no local half required.
#
# Design invariants (see docs/plans/37_STAGING_TESTBED_PLAN.md):
#   * NEVER prompt. A missing profile value is a hard error naming the key.
#   * NEVER store hosts/keys/passwords in the repo. STAGE_HOST is an ~/.ssh
#     alias; key auth comes from there.
#   * The live panel on the box is untouchable in the default parallel mode.
#
# Source-able: sourcing this file (test harness) defines every function and
# returns before the run block, exactly like scripts/update.sh.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Locate ourselves + the shared pure-helper lib.
# ---------------------------------------------------------------------------
STAGE_SELF="${BASH_SOURCE[0]}"
STAGE_SCRIPT_DIR="$(cd "$(dirname "$STAGE_SELF")" && pwd)"
STAGE_REPO_ROOT="$(cd "$STAGE_SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/stage-common.sh
source "$STAGE_SCRIPT_DIR/lib/stage-common.sh"

# ---------------------------------------------------------------------------
# Terminal styling (mirrors update.sh; degrades cleanly with no TTY/NO_COLOR).
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    ESC=$'\033'; RST="${ESC}[0m"; BLD="${ESC}[1m"
    C_OK="${ESC}[38;5;42m"; C_WARN="${ESC}[38;5;220m"
    C_ERR="${ESC}[38;5;203m"; C_LINK="${ESC}[38;5;81m"; C_FOG="${ESC}[38;5;244m"
else
    RST=''; BLD=''; C_OK=''; C_WARN=''; C_ERR=''; C_LINK=''; C_FOG=''
fi
good() { printf '  %s✔%s %s\n' "$C_OK"   "$RST" "$1"; }
warn() { printf '  %s▴%s %s\n' "$C_WARN" "$RST" "$1" >&2; }
step() { printf '  %s❯%s %s\n' "$C_LINK" "$RST" "$1"; }
info() { printf '  %s•%s %s\n' "$C_FOG"  "$RST" "$1"; }
stage_die() { printf '  %s✘%s %s\n' "$C_ERR" "$RST" "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Defaults / state populated by argument + profile parsing.
# ---------------------------------------------------------------------------
STAGE_CMD=""
STAGE_PROFILE=""
STAGE_DIRTY=0
STAGE_DRY_RUN=0
STAGE_FULL=0
STAGE_EXPOSE=0

# Set by stage_load_profile.
STAGE_HOST=""; STAGE_DIR=""; STAGE_PORT=""; STAGE_MODE=""
STAGE_SRC=""; STAGE_REMOTE=""

usage() {
    cat <<'EOF'
ServerKit staging testbed

Usage:
  stage.sh <command> <profile> [options]

Commands:
  push     Build a payload from the local checkout and ship it to the box.
  deploy   Bootstrap (idempotent) + build + migrate + restart the staging unit.
  verify   Run the layered check suite; prints one machine-readable VERDICT line.
  status   Report the deployed commit vs local HEAD (drift warning).
  logs     Fetch the last failure bundle / recent staging logs from the box.
  destroy  Remove the staging instance completely (dir, unit, firewall rule).

Options:
  --dirty      push: ship the WORKING TREE (uncommitted changes) instead of HEAD.
  --expose     push/bootstrap: open a firewall rule so staging is reachable
               off-box (loud warning; default is loopback-only).
  --full       verify: additionally run pytest on the box.
  --dry-run    Print every command that would run; change nothing.
  -h, --help   This help.

Profiles live in deploy/targets/<profile>.env (git-ignored). Copy
deploy/targets/example.env to start. STAGE_HOST is an ~/.ssh/config alias — no
credentials are ever stored here or passed on the command line.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing. `<command> <profile>` are positional; flags may appear
# anywhere. NEVER prompts — an unknown flag or missing profile is a hard error.
# ---------------------------------------------------------------------------
parse_args() {
    local a
    for a in "$@"; do
        case "$a" in
            --dirty)   STAGE_DIRTY=1 ;;
            --dry-run) STAGE_DRY_RUN=1 ;;
            --full)    STAGE_FULL=1 ;;
            --expose)  STAGE_EXPOSE=1 ;;
            -h|--help) usage; exit 0 ;;
            -*)        stage_die "Unknown option: $a (see: stage.sh --help)" ;;
            *)
                if [ -z "$STAGE_CMD" ]; then
                    STAGE_CMD="$a"
                elif [ -z "$STAGE_PROFILE" ]; then
                    STAGE_PROFILE="$a"
                else
                    stage_die "Unexpected argument: $a"
                fi
                ;;
        esac
    done
}

stage_profile_path() {
    printf '%s/deploy/targets/%s.env' "$STAGE_REPO_ROOT" "$1"
}

# Load + validate a target profile. Hard-errors (never prompts) on a missing
# file or a missing required key, naming exactly what to fix. Sets the STAGE_*
# globals and the derived remote paths.
stage_load_profile() {
    local name="$1" path
    [ -n "$name" ] || stage_die "No profile given. Usage: stage.sh $STAGE_CMD <profile>"
    path="$(stage_profile_path "$name")"
    [ -f "$path" ] || stage_die "Profile not found: $path
  Create it from the template:  cp deploy/targets/example.env $path"

    # Reset then source. The profile is plain KEY=value shell; it lives outside
    # the repo, is written by the operator, and carries no secrets.
    STAGE_HOST=""; STAGE_DIR=""; STAGE_PORT=""; STAGE_MODE=""
    # shellcheck source=/dev/null
    set -a; source "$path"; set +a

    [ -n "$STAGE_HOST" ] || stage_die "Profile $name is missing STAGE_HOST (an ~/.ssh/config alias)."
    [ -n "$STAGE_DIR" ]  || stage_die "Profile $name is missing STAGE_DIR (staging install dir on the box)."
    [ -n "$STAGE_PORT" ] || stage_die "Profile $name is missing STAGE_PORT (loopback port for staging)."
    STAGE_MODE="${STAGE_MODE:-parallel}"
    case "$STAGE_MODE" in
        parallel|replace) ;;
        *) stage_die "Profile $name has an invalid STAGE_MODE='$STAGE_MODE' (parallel|replace)." ;;
    esac

    STAGE_SRC="$(stage_src_dir "$STAGE_DIR")"
    STAGE_REMOTE="$STAGE_SRC/scripts/stage-remote.sh"
}

# ---------------------------------------------------------------------------
# Command execution. Under --dry-run every mutating/remote command is printed
# instead of run, so a full flow can be inspected with zero side effects.
# ---------------------------------------------------------------------------
run_local() {
    if [ "$STAGE_DRY_RUN" = "1" ]; then
        info "[dry-run] local: $*"
        return 0
    fi
    "$@"
}

# Run a command on the box over the profile's SSH alias.
stage_ssh() {
    local remote_cmd="$1"
    if [ "$STAGE_DRY_RUN" = "1" ]; then
        info "[dry-run] ssh $STAGE_HOST: $remote_cmd"
        return 0
    fi
    ssh "$STAGE_HOST" "$remote_cmd"
}

# The payload mode label recorded in the verdict + .stage.env.
stage_payload_mode() {
    [ "$STAGE_DIRTY" = "1" ] && printf 'dirty' || printf 'head'
}

# The commit the payload represents. Working-tree uploads are marked so a green
# verify is never mis-attributed to a clean commit.
stage_commit() {
    local head
    head="$(git -C "$STAGE_REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if [ "$STAGE_DIRTY" = "1" ]; then
        printf '%s-dirty' "$head"
    else
        printf '%s' "$head"
    fi
}

# ---------------------------------------------------------------------------
# push — build the payload and ship it, plus the box-side control config.
# ---------------------------------------------------------------------------
cmd_push() {
    local mode commit
    mode="$(stage_payload_mode)"
    commit="$(stage_commit)"

    printf '\n  %s%sServerKit staging · push%s  %s(%s → %s, %s @ %s)%s\n\n' \
        "$BLD" "$C_LINK" "$RST" "$C_FOG" "$STAGE_PROFILE" "$STAGE_HOST" "$mode" "$commit" "$RST"

    if [ "$STAGE_MODE" = "replace" ]; then
        warn "STAGE_MODE=replace — this target deploys OVER $STAGE_DIR. Use only on a scratch box/VM."
    fi

    # Reset the remote source dir, then land the payload.
    step "Preparing remote source dir $STAGE_SRC"
    stage_ssh "rm -rf '$STAGE_SRC' && mkdir -p '$STAGE_SRC'"

    if [ "$STAGE_DIRTY" = "1" ]; then
        step "Uploading working tree (rsync, exclusion list applied)"
        local excludes=()
        while IFS= read -r line; do excludes+=("$line"); done < <(stage_rsync_exclude_args)
        if [ "$STAGE_DRY_RUN" = "1" ]; then
            info "[dry-run] local: rsync -az --delete ${excludes[*]} $STAGE_REPO_ROOT/ $STAGE_HOST:$STAGE_SRC/"
        else
            command -v rsync >/dev/null 2>&1 || stage_die "--dirty needs rsync locally (not found)."
            rsync -az --delete "${excludes[@]}" "$STAGE_REPO_ROOT/" "$STAGE_HOST:$STAGE_SRC/"
        fi
    else
        step "Uploading committed HEAD (git archive)"
        if [ "$STAGE_DRY_RUN" = "1" ]; then
            info "[dry-run] local: git archive HEAD | gzip | ssh $STAGE_HOST tar xzf - -C $STAGE_SRC"
        else
            git -C "$STAGE_REPO_ROOT" archive --format=tar HEAD \
                | gzip -c \
                | ssh "$STAGE_HOST" "tar xzf - -C '$STAGE_SRC'"
        fi
    fi

    # Write the box-side control config next to the uploaded tree. stage-remote
    # sources this (../.stage.env relative to itself) so every later step
    # (bootstrap/deploy/verify/status/destroy) is a bare `ssh <alias>
    # stage-remote.sh <step>` needing no local half.
    step "Writing box-side control config ($STAGE_SRC/.stage.env)"
    local cfg
    cfg="$(cat <<EOF
# Generated by scripts/stage.sh push — DO NOT EDIT (regenerated every push).
STAGE_DIR='$STAGE_DIR'
STAGE_PORT='$STAGE_PORT'
STAGE_MODE='$STAGE_MODE'
STAGE_EXPOSE='$STAGE_EXPOSE'
STAGE_PROFILE='$STAGE_PROFILE'
STAGE_COMMIT='$commit'
STAGE_PAYLOAD_MODE='$mode'
EOF
)"
    if [ "$STAGE_DRY_RUN" = "1" ]; then
        info "[dry-run] ssh $STAGE_HOST: write .stage.env ($mode, $commit)"
    else
        printf '%s\n' "$cfg" | ssh "$STAGE_HOST" "cat > '$STAGE_SRC/.stage.env'"
        stage_ssh "chmod +x '$STAGE_REMOTE' 2>/dev/null || true"
    fi

    good "Payload on the box at $STAGE_SRC"
    info "Next: stage.sh deploy $STAGE_PROFILE   (then: verify)"
}

# ---------------------------------------------------------------------------
# Box-side pass-throughs. Each is the SAME command an operator/agent can run
# directly:  ssh <alias> bash <STAGE_SRC>/scripts/stage-remote.sh <step>
# ---------------------------------------------------------------------------
remote_step() {
    local step_name="$1"; shift
    local extra="$*"
    local cmd="bash '$STAGE_REMOTE' $step_name"
    [ -n "$extra" ] && cmd="$cmd $extra"
    stage_ssh "$cmd"
}

cmd_bootstrap() {
    step "bootstrap on $STAGE_HOST"
    local flags=""
    [ "$STAGE_EXPOSE" = "1" ] && flags="--expose"
    remote_step bootstrap "$flags"
}

cmd_deploy() {
    step "deploy on $STAGE_HOST"
    # Bootstrap is idempotent (converge) — always run it first so `deploy` alone
    # is enough after a push.
    local flags=""
    [ "$STAGE_EXPOSE" = "1" ] && flags="--expose"
    remote_step bootstrap "$flags"
    remote_step deploy
}

cmd_verify() {
    step "verify on $STAGE_HOST"
    local flags=""
    [ "$STAGE_FULL" = "1" ] && flags="--full"
    # Relay the box's exit code so CI / an agent can gate on it directly.
    remote_step verify "$flags"
}

cmd_logs() {
    step "logs from $STAGE_HOST"
    remote_step logs
}

cmd_destroy() {
    step "destroy on $STAGE_HOST"
    remote_step destroy
    good "Staging instance removed on $STAGE_HOST"
}

# status is special: the drift comparison (deployed commit vs local HEAD) is
# done here, on the local side, where the local checkout is visible.
cmd_status() {
    step "status of $STAGE_HOST"
    local local_head
    local_head="$(git -C "$STAGE_REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if [ "$STAGE_DRY_RUN" = "1" ]; then
        info "[dry-run] ssh $STAGE_HOST: bash $STAGE_REMOTE status"
        return 0
    fi
    local out deployed
    out="$(remote_step status || true)"
    printf '%s\n' "$out"
    deployed="$(printf '%s\n' "$out" | sed -n 's/^STAGE_DEPLOYED_COMMIT=//p' | head -1)"
    info "Local HEAD: $local_head"
    if [ -n "$deployed" ] && [ "$deployed" != "$local_head" ]; then
        warn "DRIFT: staging is at [$deployed] but local HEAD is [$local_head] — re-push before trusting a verdict."
    elif [ -n "$deployed" ]; then
        good "Staging matches local HEAD ($deployed)"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
stage_main() {
    parse_args "$@"
    [ -n "$STAGE_CMD" ] || { usage; exit 1; }
    case "$STAGE_CMD" in
        help|-h|--help) usage; exit 0 ;;
    esac
    stage_load_profile "$STAGE_PROFILE"
    case "$STAGE_CMD" in
        push)      cmd_push ;;
        bootstrap) cmd_bootstrap ;;
        deploy)    cmd_deploy ;;
        verify)    cmd_verify ;;
        status)    cmd_status ;;
        logs)      cmd_logs ;;
        destroy)   cmd_destroy ;;
        *)         stage_die "Unknown command: $STAGE_CMD (see: stage.sh --help)" ;;
    esac
}

# Sourcing this file (test harness) defines every function and returns here,
# before the run block — same discipline as scripts/update.sh.
[ "${BASH_SOURCE[0]}" = "${0}" ] || return 0

stage_main "$@"
