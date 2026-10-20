# shellcheck shell=bash
#
# scripts/lib/firewall.sh — sourceable firewall abstraction.
#
# Detects the active host firewall and opens/closes the ports ServerKit needs
# (80/443, optionally 5000). Fresh Rocky/Alma/RHEL/Fedora boxes ship firewalld
# active by default, which silently blocks 80/443 — making a perfectly good
# install look "broken" because the panel is unreachable. This helper fixes
# that automatically and, crucially, is reversible: callers persist exactly
# what was opened (see scripts/lib/state.sh) so uninstall can undo only those
# rules and never touch a user's own firewall config.
#
# Contract:
#   firewall_detect                      → echoes: firewalld|ufw|nftables|iptables|none
#   firewall_open  <backend> <port...>   → open ports (each "80/tcp")
#   firewall_close <backend> <port...>   → close the same ports
#
# Honors:
#   FW_DRY_RUN=1          print intended commands instead of running them
#   FIREWALL_BACKEND=...  force the detected backend (used by unit tests)
#
# Everything is best-effort: a firewall we can't drive cleanly degrades to a
# warning rather than aborting the install.

# Run a privileged firewall command, or just print it under dry-run. Kept tiny
# so the dry-run output is exactly the command that would otherwise execute —
# which is what the installer unit tests assert against.
_fw_run() {
    if [ "${FW_DRY_RUN:-0}" = "1" ]; then
        printf '  [firewall] would run: %s\n' "$*"
        return 0
    fi
    # Discard the tool's own chatter ("success", rule-exists noise) here so
    # callers don't redirect — that redirection would also swallow the dry-run
    # line above, which the installer unit tests assert on.
    "$@" >/dev/null 2>&1
}

# Is inbound traffic already filtered by default on this host?
#
# Distinct from firewall_detect() on purpose, and the difference is load-bearing.
# firewall_detect answers "which backend do I open a port THROUGH" — it happily
# returns `nftables` when the nft binary merely exists and can list an EMPTY
# ruleset, which is true of a stock Ubuntu/Debian cloud image. Using it as an
# "is a firewall already protecting this box" test therefore reports every
# modern Linux as protected, and a default-deny bootstrap gated on it never
# runs (found on a real Ubuntu 24.04 VM, where install logged "via nftables"
# and the bootstrap said nothing at all).
#
# This asks the question that actually matters, and the only one the bootstrap
# cares about: does the host already DENY inbound by default? Presence of rules
# is not the test — Docker installs plenty of nft/iptables chains and protects
# nothing — a default-deny policy on the input hook is.
#
# Returns 0 when inbound is default-denied, 1 otherwise (including when it
# cannot tell: an unprotected box is the safer assumption for a caller whose
# response is to *offer* protection, and every caller re-checks before acting).
firewall_inbound_default_deny() {
    # firewalld: running means a zone policy is in force; its default zones all
    # reject unsolicited inbound.
    if command -v firewall-cmd >/dev/null 2>&1; then
        case "$(firewall-cmd --state 2>/dev/null || true)" in
            *running*) return 0 ;;
        esac
    fi

    # ufw: active AND its default incoming policy is deny/reject. `ufw status
    # verbose` prints e.g. "Default: deny (incoming), allow (outgoing)".
    if command -v ufw >/dev/null 2>&1; then
        local ufw_out
        ufw_out="$(ufw status verbose 2>/dev/null || true)"
        case "$ufw_out" in
            *[Ss]tatus:*[Ii]nactive*) : ;;
            *[Ss]tatus:*[Aa]ctive*)
                case "$ufw_out" in
                    *[Dd]efault:*deny\ \(incoming\)*|*[Dd]efault:*reject\ \(incoming\)*)
                        return 0 ;;
                esac
                ;;
        esac
    fi

    # nftables: a base chain hooked to input whose policy is drop or reject.
    # An empty ruleset, or Docker's nat/filter chains, match neither.
    if command -v nft >/dev/null 2>&1; then
        local nft_out
        nft_out="$(nft -a list ruleset 2>/dev/null || true)"
        if printf '%s' "$nft_out" \
            | tr '\n' ' ' \
            | grep -qE 'hook[[:space:]]+input[^}]*policy[[:space:]]+(drop|reject)'; then
            return 0
        fi
    fi

    # iptables/ip6tables: an INPUT policy that is not ACCEPT.
    if command -v iptables >/dev/null 2>&1; then
        case "$(iptables -S INPUT 2>/dev/null | head -1 || true)" in
            *'-P INPUT DROP'*|*'-P INPUT REJECT'*) return 0 ;;
        esac
    fi

    return 1
}

# Detect the active firewall. Order matters: a box can have several of these
# binaries installed but only one actually managing the input chain, so we pick
# the highest-level manager that reports itself active.
#
# NOTE: this answers "which backend should I open a port through", NOT "is this
# box protected" — it returns a backend for a binary that exists even with no
# rules at all. For the latter use firewall_inbound_default_deny() above.
firewall_detect() {
    if [ -n "${FIREWALL_BACKEND:-}" ]; then
        printf '%s\n' "$FIREWALL_BACKEND"
        return 0
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        printf 'firewalld\n'; return 0
    fi
    if command -v ufw >/dev/null 2>&1; then
        # Capture-then-case rather than `ufw status | grep -q`: grep -q exits
        # at the first match, ufw can then die of SIGPIPE, and under a caller's
        # pipefail the pipeline goes non-zero — misdetecting an active ufw.
        local ufw_out
        ufw_out="$(ufw status 2>/dev/null || true)"
        case "$ufw_out" in
            *[Ss]tatus:*[Ii]nactive*) : ;;  # "inactive" contains "active" — rule it out first
            *[Ss]tatus:*[Aa]ctive*)   printf 'ufw\n'; return 0 ;;
        esac
    fi
    if command -v nft >/dev/null 2>&1 && nft list ruleset >/dev/null 2>&1; then
        printf 'nftables\n'; return 0
    fi
    if command -v iptables >/dev/null 2>&1; then
        printf 'iptables\n'; return 0
    fi
    printf 'none\n'
}

# Split "80/tcp" into port + proto (proto defaults to tcp).
_fw_port()  { printf '%s' "${1%%/*}"; }
_fw_proto() { case "$1" in */*) printf '%s' "${1##*/}" ;; *) printf 'tcp' ;; esac; }

firewall_open() {
    # No backend argument (a zero-arg call under a `set -u` caller) is a
    # no-op, not an unbound-variable abort.
    local backend="${1:-}"
    [ -n "$backend" ] || return 0
    shift
    local spec port proto
    case "$backend" in
        firewalld)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                _fw_run firewall-cmd --permanent "--add-port=${port}/${proto}" || true
            done
            _fw_run firewall-cmd --reload || true
            ;;
        ufw)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                _fw_run ufw allow "${port}/${proto}" || true
            done
            ;;
        iptables)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                # -C tests for an existing rule so we stay idempotent; -I only if absent.
                _fw_run iptables -C INPUT -p "$proto" --dport "$port" -j ACCEPT || \
                    _fw_run iptables -I INPUT -p "$proto" --dport "$port" -j ACCEPT || true
            done
            ;;
        nftables)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                # Best-effort: requires an inet filter/input chain to exist.
                _fw_run nft add rule inet filter input "$proto" dport "$port" accept || true
            done
            ;;
        none|*)
            return 0
            ;;
    esac
}

# Succeeds when the active firewall already has ANY allow rule for <port> —
# open to everyone or restricted to some sources (e.g. Cloudflare-only web
# ports). Callers that "ensure" a port is open use it to leave an operator's
# deliberate, narrower rules alone instead of widening them to the world.
# Read-only; capture-then-match so a SIGPIPE under pipefail can't misreport.
firewall_port_has_rule() {
    local backend="${1:-}" port="${2:-}" out=""
    [ -n "$backend" ] && [ -n "$port" ] || return 1
    case "$backend" in
        ufw)
            out="$(ufw status 2>/dev/null || true)"
            # The first column ("To") holds the port: "80/tcp", "80,443/tcp", "443".
            printf '%s\n' "$out" | awk '{print $1}' | grep -Eq "(^|,)${port}(,|/|$)"
            ;;
        iptables)
            out="$(iptables -S INPUT 2>/dev/null || true)"
            printf '%s\n' "$out" | grep -E -- '-j ACCEPT' | \
                grep -Eq -- "--dport ${port}( |$)|--dports ([0-9:,]*,)?${port}(,| |$)"
            ;;
        firewalld)
            out="$(firewall-cmd --list-all 2>/dev/null || true)"
            printf '%s\n' "$out" | grep -Eq "(^|[ =\"])${port}/(tcp|udp)|port=\"${port}\""
            ;;
        nftables)
            out="$(nft list ruleset 2>/dev/null || true)"
            printf '%s\n' "$out" | grep -E 'accept' | grep -Eq "dport (\{[^}]*\b${port}\b|${port}\b)"
            ;;
        *)
            return 1
            ;;
    esac
}

firewall_close() {
    # Same zero-arg guard as firewall_open.
    local backend="${1:-}"
    [ -n "$backend" ] || return 0
    shift
    local spec port proto
    case "$backend" in
        firewalld)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                _fw_run firewall-cmd --permanent "--remove-port=${port}/${proto}" || true
            done
            _fw_run firewall-cmd --reload || true
            ;;
        ufw)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                _fw_run ufw delete allow "${port}/${proto}" || true
            done
            ;;
        iptables)
            for spec in "$@"; do
                port="$(_fw_port "$spec")"; proto="$(_fw_proto "$spec")"
                _fw_run iptables -D INPUT -p "$proto" --dport "$port" -j ACCEPT || true
            done
            ;;
        nftables|none|*)
            # nftables rule deletion needs the rule handle; we can't reliably
            # find it after the fact, so leave it to the operator.
            return 0
            ;;
    esac
}

# Warn-and-document fallback text for when no firewall could be driven.
firewall_manual_hint() {
    local ports="${1:-}"
    printf 'Open these ports manually so the panel is reachable: %s\n' "$ports"
}
