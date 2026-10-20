#!/usr/bin/env bash
# Run the checks that fail CI without running the whole suite.
#
# Every red backend-CI round on 2026-08-21 was a ratchet, not a broken feature:
# a floor measured on a dev box, a fingerprint baseline left unregenerated after
# a refactor, a ceiling that moved with the code, a generated doc gone stale,
# and a text-grep guard that matched a docstring. None of them need the 4-shard
# suite to catch -- they are AST and text scans over the tree, and they all run
# here in about two minutes.
#
# Usage (from anywhere):
#
#     bash scripts/preflight.sh
#     PYTHON=~/sk-venv/bin/python bash scripts/preflight.sh
#
# The interpreter needs the backend's dependencies (flask, sqlalchemy, pytest).
# On the Windows dev box that means the WSL venv, which also reaches Windows
# node through interop, so one invocation covers both halves:
#
#     wsl -e bash -lc "cd /mnt/c/Users/<you>/Documents/GitHub/ServerKit && \
#         PYTHON=~/sk-venv/bin/python bash scripts/preflight.sh"
#
# Exit code is 0 only when every check that ran passed.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
REPO=$(pwd)

PYTHON=${PYTHON:-python3}
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "preflight: no python interpreter found (set PYTHON=/path/to/python)" >&2
    exit 2
fi

# node, or Windows node.exe reached from WSL. In the latter case every path
# handed to it has to be a Windows path or it resolves against C:\.
NODE=''
NODE_NEEDS_WINPATH=0
if command -v node >/dev/null 2>&1; then
    NODE=node
elif command -v node.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    NODE=node.exe
    NODE_NEEDS_WINPATH=1
elif [ -x /mnt/c/nvm4w/nodejs/node.exe ] && command -v wslpath >/dev/null 2>&1; then
    NODE=/mnt/c/nvm4w/nodejs/node.exe
    NODE_NEEDS_WINPATH=1
fi

node_path() {
    if [ "$NODE_NEEDS_WINPATH" -eq 1 ]; then
        wslpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

FAILED=''
SKIPPED=''

note_fail() { FAILED="${FAILED}${1}"$'\n'; }
note_skip() { SKIPPED="${SKIPPED}${1}"$'\n'; }
banner() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Test-count ratchet -- the floor CI compares against.
# ---------------------------------------------------------------------------
banner 'test-count ratchet'
if ! (cd "$REPO/backend" && "$PYTHON" tests/check_test_count.py); then
    note_fail 'test-count ratchet'
fi

# ---------------------------------------------------------------------------
# 1b. Undefined names (same check as the CI ratchet job).
# ---------------------------------------------------------------------------
banner 'undefined names'
if command -v ruff >/dev/null 2>&1 || "$PYTHON" -m ruff --version >/dev/null 2>&1; then
    ruff_cmd='ruff'
    command -v ruff >/dev/null 2>&1 || ruff_cmd="$PYTHON -m ruff"
    # shellcheck disable=SC2086
    if ! (cd "$REPO/backend" && $ruff_cmd check --select F821,F823 app tests); then
        note_fail 'undefined names'
    fi
else
    echo 'ruff not installed (pip install ruff==0.14.14) -- CI runs this check' >&2
    note_skip 'undefined names (ruff not installed)'
fi

# ---------------------------------------------------------------------------
# 2. Backend ratchets, censuses, and guards.
#
# Matched by filename rather than listed, so a new ratchet is covered the day it
# lands instead of the day someone remembers to add it here.
# ---------------------------------------------------------------------------
banner 'backend ratchets and guards'
guard_files=$(cd "$REPO/backend" && ls tests/test_*ratchet*.py tests/test_*census*.py \
    tests/test_*guard*.py tests/test_*boundar*.py tests/test_*inventory*.py \
    tests/test_*drift*.py tests/test_*spec*.py 2>/dev/null | tr '\n' ' ')
if [ -n "$guard_files" ]; then
    # shellcheck disable=SC2086
    if ! (cd "$REPO/backend" && PYTHONPATH=. FLASK_ENV=testing "$PYTHON" -m pytest \
            $guard_files -q -p no:cacheprovider -p no:warnings); then
        note_fail 'backend ratchets and guards'
    fi
else
    echo 'no matching test files'
    note_skip 'backend ratchets and guards (no matching test files)'
fi

# ---------------------------------------------------------------------------
# 3. Frontend boundary check + the generated inventory that reads it.
#
# test_migration_inventory.py SKIPS when node is missing, which is how the
# inventory reached CI stale: the check that would have caught it never ran.
# ---------------------------------------------------------------------------
if [ -n "$NODE" ]; then
    banner 'frontend boundaries'
    if ! (cd "$REPO/frontend" && "$NODE" "$(node_path "$REPO/frontend/scripts/check-frontend-boundaries.mjs")"); then
        note_fail 'frontend boundaries'
    fi

    banner 'builtin frontend drift'
    # Extensions CI red on 2026-08-30: a builtin-extensions/ manifest changed
    # without re-running the sync, and nothing local caught it. Same contract
    # as the inventory below: a fixable failure is fixed, then flagged so the
    # diff gets reviewed and committed.
    if (cd "$REPO" && "$NODE" "$(node_path "$REPO/scripts/sync-builtin-frontends.mjs")" --check >/dev/null 2>&1); then
        echo 'pre-bundled copies match builtin-extensions/ sources'
    elif (cd "$REPO" && "$NODE" "$(node_path "$REPO/scripts/sync-builtin-frontends.mjs")"); then
        echo 'pre-bundled copies were stale and have been re-synced -- review and commit the diff' >&2
        note_fail 'builtin frontend drift (re-synced; commit the diff)'
    else
        note_fail 'builtin frontend drift (sync script failed)'
    fi

    banner 'migration inventory is regenerated'
    # Compared against a snapshot of the file, NOT against HEAD: an uncommitted
    # but correct regeneration is a pass, and only a number that actually moved
    # is a failure.
    doc="$REPO/docs/MIGRATION_INVENTORY.md"
    before=$(mktemp)
    cp "$doc" "$before" 2>/dev/null
    if ! "$PYTHON" "$REPO/scripts/generate-migration-inventory.py" >/dev/null; then
        note_fail 'migration inventory (generator failed)'
    elif cmp -s "$doc" "$before"; then
        echo 'docs/MIGRATION_INVENTORY.md matches the measured state'
    else
        echo 'docs/MIGRATION_INVENTORY.md was stale and has been regenerated -- review and commit the diff' >&2
        note_fail 'migration inventory (regenerated; commit the diff)'
    fi
    rm -f "$before"
else
    note_skip 'frontend boundaries + builtin frontend drift + migration inventory (no node on PATH)'
fi

# ---------------------------------------------------------------------------
banner 'preflight summary'
[ -n "$SKIPPED" ] && printf '%s' "$SKIPPED" | sed 's/^/  SKIPPED  /'
if [ -z "$FAILED" ]; then
    echo '  PASS     everything CI checks outside the sharded suite'
    exit 0
fi
printf '%s' "$FAILED" | sed 's/^/  FAILED   /'
exit 1
