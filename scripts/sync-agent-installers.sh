#!/usr/bin/env bash
#
# Re-vendor the agent installers from their canonical home.
#
# The agent has its own repository, and its installers are canonical there. The
# panel still needs them on its own disk -- it serves them at
# GET /api/v1/servers/install.sh|.ps1 -- so this repo carries a vendored copy
# under scripts/. That is two copies of one file, which is exactly the shape
# that rots: they drifted silently once, and the panel spent that whole period
# serving an installer whose download URL pointed at a tag scheme that no longer
# existed anywhere (issue #101).
#
# So: edit the agent repo, run this, commit the result. Never patch
# scripts/install.sh directly. scripts/test/check-installer-drift.sh fails if
# anyone does, and ServerKit's nightly CI runs it.
#
# Usage:
#   bash scripts/sync-agent-installers.sh              # sibling checkout, else GitHub
#   bash scripts/sync-agent-installers.sh <path|url>   # explicit source
#
#   AGENT_REF=v1.2.0 bash scripts/sync-agent-installers.sh   # pin a ref when fetching

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_REPO="${SERVERKIT_AGENT_REPO:-jhd3197/serverkit-agent}"
AGENT_REF="${AGENT_REF:-}"
SOURCE="${1:-}"

INSTALLERS=(install.sh install.ps1)

# Default source: a sibling checkout if there is one (so this works offline and
# before the agent change is pushed), otherwise the published branch.
# Ask the repo which branch is its default rather than hardcoding one — see the
# same helper in scripts/test/check-installer-drift.sh. AGENT_REF pins it.
resolve_agent_ref() {
    if [ -n "$AGENT_REF" ]; then printf '%s' "$AGENT_REF"; return 0; fi

    local declared ref
    declared="$(curl -fsSL "https://api.github.com/repos/${AGENT_REPO}" 2>/dev/null \
        | grep -o '"default_branch": *"[^"]*"' | head -1 | sed -e 's/.*: *"//' -e 's/"$//')"
    if [ -n "$declared" ]; then printf '%s' "$declared"; return 0; fi

    for ref in main master; do
        if curl -fsSL -o /dev/null \
                "https://raw.githubusercontent.com/${AGENT_REPO}/${ref}/install.sh" 2>/dev/null; then
            printf '%s' "$ref"; return 0
        fi
    done
    printf 'main'
}

if [ -z "$SOURCE" ]; then
    if [ -d "${REPO_ROOT}/../serverkit-agent" ]; then
        SOURCE="$(cd "${REPO_ROOT}/../serverkit-agent" && pwd)"
    else
        SOURCE="https://raw.githubusercontent.com/${AGENT_REPO}/$(resolve_agent_ref)"
    fi
fi

printf '\nvendoring agent installers from: %s\n\n' "$SOURCE"

changed=0
for f in "${INSTALLERS[@]}"; do
    dest="${REPO_ROOT}/scripts/${f}"
    tmp="$(mktemp)"

    if [ -d "$SOURCE" ]; then
        if [ ! -f "${SOURCE}/${f}" ]; then
            printf '  \033[31m✘\033[0m %s not found in %s\n' "$f" "$SOURCE"
            rm -f "$tmp"; exit 1
        fi
        cp "${SOURCE}/${f}" "$tmp"
    else
        if ! curl -fsSL "${SOURCE}/${f}" -o "$tmp"; then
            printf '  \033[31m✘\033[0m could not fetch %s/%s\n' "$SOURCE" "$f"
            rm -f "$tmp"; exit 1
        fi
    fi

    # Normalise to LF and let .gitattributes apply this repo's EOL policy on
    # checkout (*.ps1 is eol=crlf here, LF in the agent repo). The drift check
    # compares EOL-insensitively for the same reason.
    tr -d '\r' < "$tmp" > "${tmp}.lf" && mv "${tmp}.lf" "$tmp"

    if [ -f "$dest" ] && diff -q <(tr -d '\r' < "$dest") "$tmp" >/dev/null 2>&1; then
        printf '  \033[32m=\033[0m %s already up to date\n' "$f"
        rm -f "$tmp"
        continue
    fi

    cp "$tmp" "$dest"
    rm -f "$tmp"
    [ "${f##*.}" = "sh" ] && chmod +x "$dest"
    printf '  \033[33m↓\033[0m %s updated\n' "$f"
    changed=$((changed + 1))
done

printf '\n%d file(s) updated\n' "$changed"
if [ "$changed" -gt 0 ]; then
    printf 'Review and commit scripts/install.sh / scripts/install.ps1.\n'
    printf 'The panel serves these; run scripts/test/smoke-docker-image.sh before releasing.\n\n'
else
    printf '\n'
fi
