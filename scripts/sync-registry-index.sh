#!/usr/bin/env bash
#
# Re-vendor the bundled extension registry index from its canonical home.
#
# backend/app/data/registry_index.json is the Marketplace's offline fallback and
# what the test suite reads. The canonical copy lives in the serverkit-extensions
# repo. That is two copies of one document, which is the shape that rots: the
# registry gained `serverkit-localkit` and the bundled mirror was never updated,
# so an offline panel would have offered a different extension set than an online
# one -- and it stayed that way until CI complained.
#
# So: merge the registry PR, run this, commit the result. Never hand-edit the
# entries in the bundled file. tests/test_registry_bundled_index.py fails if the
# two disagree on what any entry pins.
#
# Usage:
#   bash scripts/sync-registry-index.sh              # sibling checkout, else GitHub
#   bash scripts/sync-registry-index.sh <path|url>   # explicit source
#
#   REGISTRY_REF=v3 bash scripts/sync-registry-index.sh   # pin a ref when fetching

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY_REPO="${SERVERKIT_REGISTRY_REPO:-jhd3197/serverkit-extensions}"
REGISTRY_REF="${REGISTRY_REF:-}"
SOURCE="${1:-}"

DEST="${REPO_ROOT}/backend/app/data/registry_index.json"

if ! command -v python3 >/dev/null 2>&1; then
    printf '\033[31m✘\033[0m python3 is required (this is a maintainer tool, not an installer path)\n' >&2
    exit 1
fi

# Ask the repo which branch is its default rather than hardcoding one -- the
# agent repo was renamed master -> main and every hardcoded name became a 404
# that read as "cannot reach the canonical copy". Same helper as
# scripts/sync-agent-installers.sh.
resolve_registry_ref() {
    if [ -n "$REGISTRY_REF" ]; then printf '%s' "$REGISTRY_REF"; return 0; fi

    local declared ref
    declared="$(curl -fsSL "https://api.github.com/repos/${REGISTRY_REPO}" 2>/dev/null \
        | grep -o '"default_branch": *"[^"]*"' | head -1 | sed -e 's/.*: *"//' -e 's/"$//')"
    if [ -n "$declared" ]; then printf '%s' "$declared"; return 0; fi

    for ref in main master; do
        if curl -fsSL -o /dev/null \
                "https://raw.githubusercontent.com/${REGISTRY_REPO}/${ref}/index.json" 2>/dev/null; then
            printf '%s' "$ref"; return 0
        fi
    done
    printf 'main'
}

# Default source: a sibling checkout if there is one (so this works offline and
# before the registry PR is merged), otherwise the published branch.
#
# NOTE: deliberately the RAW index, never https://serverkit.ai/ext/index.json.
# The panel fetches through that proxy at runtime and the proxy rewrites every
# relative `logo` path to an absolute serverkit.ai URL. Vendoring the proxied
# copy would bake those absolute URLs into the fallback, and the bundled loader
# resolves relative paths against the registry URL itself -- so the proxied copy
# is the wrong document to mirror even though it looks equivalent. The pinned
# tuple the tests compare (version/source/sha256) is identical either way, so
# nothing would have caught it.
if [ -z "$SOURCE" ]; then
    if [ -f "${REPO_ROOT}/../serverkit-extensions/index.json" ]; then
        SOURCE="$(cd "${REPO_ROOT}/../serverkit-extensions" && pwd)/index.json"
    else
        SOURCE="https://raw.githubusercontent.com/${REGISTRY_REPO}/$(resolve_registry_ref)/index.json"
    fi
fi

printf '\nvendoring registry index from: %s\n\n' "$SOURCE"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [ -f "$SOURCE" ]; then
    cp "$SOURCE" "$TMP"
elif [ -d "$SOURCE" ]; then
    if [ ! -f "${SOURCE}/index.json" ]; then
        printf '  \033[31m✘\033[0m index.json not found in %s\n' "$SOURCE"
        exit 1
    fi
    cp "${SOURCE}/index.json" "$TMP"
else
    if ! curl -fsSL "$SOURCE" -o "$TMP"; then
        printf '  \033[31m✘\033[0m could not fetch %s\n' "$SOURCE"
        exit 1
    fi
fi

python3 - "$TMP" "$DEST" <<'PY'
import json
import sys

canonical_path, dest_path = sys.argv[1], sys.argv[2]

try:
    with open(canonical_path, encoding='utf-8') as f:
        canonical = json.load(f)
except (OSError, ValueError) as exc:
    print(f'  \033[31m✘\033[0m canonical index is not readable JSON: {exc}')
    sys.exit(1)

if not isinstance(canonical.get('extensions'), list) or not canonical['extensions']:
    print('  \033[31m✘\033[0m canonical index has no extensions — refusing to blank the fallback')
    sys.exit(1)

with open(dest_path, encoding='utf-8') as f:
    before = f.read()
current = json.loads(before)

def pinned(index):
    return {
        e['slug']: (
            e.get('version'), e.get('source'), e.get('sha256'),
            e.get('signature'), e.get('publisher_key_id'),
        )
        for e in index['extensions']
    }

ours, theirs = pinned(current), pinned(canonical)
added = sorted(set(theirs) - set(ours))
removed = sorted(set(ours) - set(theirs))
repinned = sorted(s for s in set(ours) & set(theirs) if ours[s] != theirs[s])

# Take the canonical document wholesale, but keep the local `note`: it explains
# to the next reader what this file is and that it must not be hand-edited, and
# it does not exist upstream. Everything else -- including entry order -- comes
# from canonical, because a mirror that reorders is a mirror that diffs forever.
merged = dict(canonical)
if 'note' in current:
    merged = {
        'schema_version': canonical.get('schema_version', current.get('schema_version')),
        'updated': canonical.get('updated', current.get('updated')),
        'note': current['note'],
        'extensions': canonical['extensions'],
    }

# indent=2 + ensure_ascii=True + trailing newline round-trips the existing file
# byte-for-byte, so an in-sync run produces an empty diff rather than a reformat.
after = json.dumps(merged, indent=2, ensure_ascii=True) + '\n'

if after == before:
    print('  \033[32m=\033[0m registry_index.json already up to date')
    sys.exit(0)

with open(dest_path, 'w', encoding='utf-8', newline='') as f:
    f.write(after)

print('  \033[33m↓\033[0m registry_index.json updated')
for slug in added:
    print(f'      + {slug} {theirs[slug][0]}')
for slug in removed:
    print(f'      - {slug} {ours[slug][0]}')
for slug in repinned:
    print(f'      ~ {slug}: {ours[slug][0]} -> {theirs[slug][0]}')
if not (added or removed or repinned):
    print('      (metadata only — no entry added, removed, or repinned)')
sys.exit(10)
PY
rc=$?

printf '\n'
if [ "$rc" -eq 10 ]; then
    printf 'Review and commit backend/app/data/registry_index.json.\n'
    printf 'Bundled (bundled: true) entries must match builtin-extensions/<slug>/plugin.json;\n'
    printf 'run: cd backend && python -m pytest tests/test_registry_bundled_index.py\n\n'
    exit 0
fi

exit "$rc"
