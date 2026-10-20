#!/bin/bash
#
# ServerKit release builder.
#
# Produces a portable tarball that installs without compiling the frontend or
# building a Python venv on the target. The release contains:
#   - /opt/serverkit source tree
#   - /opt/serverkit/frontend/dist pre-built bundle
#   - /opt/serverkit/venv pre-built Python virtual environment
#
# The venv is built locally and then made relocatable by rewriting absolute
# paths so it works at /opt/serverkit/venv on the target machine.
#
#   bash scripts/build-release.sh
#   VERSION=v1.7.0 bash scripts/build-release.sh
#
set -euo pipefail

BUILD_DIR="/tmp/serverkit-release-build"
TARGET_VENV_PATH="/opt/serverkit/venv"

# ---------------------------------------------------------------------------
# Terminal styling (violet ServerKit identity, degrades to plain text)
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    ESC=$'\033'
    RST="${ESC}[0m"; BLD="${ESC}[1m"
    paint() { printf '%s[38;2;%d;%d;%dm' "$ESC" "$1" "$2" "$3"; }
else
    RST=''; BLD=''
    paint() { :; }
fi

HUE_OK="$(paint 52 211 153)"; HUE_ERR="$(paint 248 113 113)"
HUE_LINK="$(paint 103 232 249)"

good() { printf '  %s✔%s %s\n' "$HUE_OK"   "$RST" "$1"; }
halt() { printf '  %s✘%s %s\n' "$HUE_ERR"  "$RST" "$1" >&2; exit 1; }
step() { printf '  %s❯%s %s\n' "$HUE_LINK" "$RST" "$1"; }

# ---------------------------------------------------------------------------
# Resolve version + architecture into an output filename
# ---------------------------------------------------------------------------
if [ -n "${VERSION:-}" ]; then
    RELEASE_TAG="$VERSION"
elif [ -f "VERSION" ]; then
    RELEASE_TAG="v$(cat VERSION | tr -d '\n\r ')"
else
    halt "Cannot determine version. Set VERSION or create a VERSION file."
fi

case "${BUILD_ARCH:-$(uname -m)}" in
    x86_64|amd64)  DL_ARCH="amd64" ;;
    aarch64|arm64) DL_ARCH="arm64" ;;
    *)             halt "Unsupported architecture: ${BUILD_ARCH:-$(uname -m)}" ;;
esac

OUTPUT="serverkit-${RELEASE_TAG}-linux-${DL_ARCH}.tar.gz"
CHECKSUMS="checksums.txt"
step "Building release ${RELEASE_TAG} for ${DL_ARCH}"

# ---------------------------------------------------------------------------
# Toolchain check
# ---------------------------------------------------------------------------
command -v node &>/dev/null || halt "Node.js is required."
python3 -c 'import venv' 2>/dev/null || halt "Python venv module is required."

# ---------------------------------------------------------------------------
# Stage a clean copy of the repository
# ---------------------------------------------------------------------------
step "Preparing the build directory..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

step "Copying the source tree..."
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Copy the source tree with tar, which handles broken symlinks and avoids
# the long rm -rf timeouts that cp + rm can hit on Windows/Git Bash.
cd "$REPO_ROOT"
tar -cf - \
    --exclude='./.git' \
    --exclude='./node_modules' \
    --exclude='./venv' \
    --exclude='./.venv' \
    --exclude='./.venv-wsl' \
    --exclude='./backend/venv' \
    --exclude='./backend/.venv' \
    --exclude='./backend/.venv-wsl' \
    --exclude='./__pycache__' \
    --exclude='./.pytest_cache' \
    --exclude='./instance' \
    --exclude='./dist' \
    --exclude='./backups' \
    --exclude='./backend/dev-data/backups' \
    --exclude='./backend/instance/backups' \
    --exclude='./scripts/test/output' \
    --exclude='./docs/screenshots' \
    --exclude='./frontend/dist' \
    --exclude='./.codex-screenshots' \
    --exclude='./template-icons' \
    --no-wildcards-match-slash \
    --exclude='./*.png' \
    --exclude='./*.jpg' \
    --exclude='./*.jpeg' \
    --exclude='./dev/*.png' \
    --exclude='*.log' \
    --exclude='*.tmp' \
    --exclude='*.pyc' \
    . | tar -C "$BUILD_DIR" -xf -

# ---------------------------------------------------------------------------
# Build the frontend bundle
# ---------------------------------------------------------------------------
step "Building the frontend..."
cd "$BUILD_DIR/frontend"
npm ci --prefer-offline 2>&1 | tail -5
NODE_OPTIONS="--max-old-space-size=1024" npm run build 2>&1 | tail -10

# ---------------------------------------------------------------------------
# Build the Python virtual environment
# ---------------------------------------------------------------------------
step "Building Python virtual environment..."
VENV_BUILD_DIR="$BUILD_DIR/venv"
rm -rf "$VENV_BUILD_DIR"
# --copies embeds the Python interpreter so the venv does not depend on the
# target machine having the interpreter at the exact same system path.
python3 -m venv --copies "$VENV_BUILD_DIR"
# shellcheck source=/dev/null
source "$VENV_BUILD_DIR/bin/activate"
pip install --upgrade pip --quiet
pip install -r "$BUILD_DIR/backend/requirements.txt" --quiet
pip install gunicorn --quiet

# Make the venv relocatable: rewrite absolute paths in activate scripts and
# shebangs so the venv works at $TARGET_VENV_PATH on the target server.
make_venv_relocatable() {
    local venv_dir="$1"
    local current_path
    current_path="$(cd "$venv_dir" && pwd)"

    for f in "$venv_dir"/bin/activate*; do
        [ -f "$f" ] || continue
        sed -i "s|$current_path|$TARGET_VENV_PATH|g" "$f"
    done

    for f in "$venv_dir"/bin/*; do
        [ -f "$f" ] || continue
        if head -n1 "$f" | grep -q "^#!"; then
            sed -i "1s|$current_path|$TARGET_VENV_PATH|g" "$f"
        fi
    done

    # pyvenv.cfg command line is cosmetic, but keep it consistent.
    if [ -f "$venv_dir/pyvenv.cfg" ]; then
        sed -i "s|$current_path|$TARGET_VENV_PATH|g" "$venv_dir/pyvenv.cfg"
    fi
}

make_venv_relocatable "$VENV_BUILD_DIR"
good "Virtual environment built and relocated to $TARGET_VENV_PATH"

# ---------------------------------------------------------------------------
# Strip development artifacts from the staged tree
# ---------------------------------------------------------------------------
step "Cleaning release artifacts..."
rm -rf "$BUILD_DIR/frontend/node_modules"
find "$BUILD_DIR" -type d -name __pycache__    -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name .pytest_cache  -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type f -name '*.pyc' -delete 2>/dev/null || true
# Never strip anything under frontend/: dist/assets holds the images Vite
# emitted (extension icons, etc.) that the panel 404s without, and src/assets
# holds the ones the JS imports — delete those and the shipped tree can no
# longer rebuild its own frontend (`serverkit doctor --fix` on 1.9.x died on
# 18 unresolved png imports, issue #127). The tar excludes above already keep
# docs/dev screenshots out; this pass only catches stragglers elsewhere.
# (Use ! -path, not -prune: -delete implies -depth, which disables -prune.)
find "$BUILD_DIR" -type f \( -name '*.png' -o -name '*.jpeg' -o -name '*.jpg' \) \
    ! -path "$BUILD_DIR/frontend/*" -delete 2>/dev/null || true

# Runtime directory the app expects to exist.
mkdir -p "$BUILD_DIR/backend/instance"

# ---------------------------------------------------------------------------
# Verify the bundle: every /assets/<file> the built JS/HTML references must
# exist on disk. A missing hashed asset (e.g. an over-broad cleanup deleting
# dist images) is a silent runtime 404 — fail the build here instead.
# ---------------------------------------------------------------------------
step "Verifying bundle asset references..."
missing_assets=0
while IFS= read -r asset; do
    if [ ! -f "$BUILD_DIR/frontend/dist/$asset" ]; then
        printf '    missing: %s\n' "$asset" >&2
        missing_assets=1
    fi
done < <(grep -rhoE 'assets/[A-Za-z0-9_.-]+\.(png|jpe?g|gif|webp|avif|svg|woff2?|ttf|otf|mp4|webm)' \
         "$BUILD_DIR/frontend/dist/index.html" "$BUILD_DIR/frontend/dist/assets" 2>/dev/null | sort -u)
[ "$missing_assets" = "0" ] || halt "Bundle references assets missing from dist/ (see above)."
good "Bundle asset references verified"

# ---------------------------------------------------------------------------
# Pack the tarball with an /opt/serverkit prefix and generate checksums
# ---------------------------------------------------------------------------
step "Creating the tarball..."
DEST_DIR="$REPO_ROOT"

cd "$(dirname "$BUILD_DIR")"
# Image slimming happens in the cleanup step above (which preserves
# frontend/dist); do NOT re-exclude *.png/*.jpg here — that would strip the
# built bundle's hashed image assets out of the tarball.
tar czf "${DEST_DIR}/${OUTPUT}" \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='instance' \
    --exclude='/backups' \
    --exclude='/backend/instance/backups' \
    --exclude='/backend/dev-data/backups' \
    --exclude='/scripts/test/output' \
    --exclude='*.log' \
    --exclude='*.tmp' \
    --exclude='*.pyc' \
    --transform 's|^serverkit-release-build|opt/serverkit|' \
    serverkit-release-build

cd "$DEST_DIR"
rm -rf "$BUILD_DIR"

sha256sum "$OUTPUT" > "$CHECKSUMS"

good "Release built: ${OUTPUT}"
good "Checksums: ${CHECKSUMS}"
ls -lh "${OUTPUT}"
cat "$CHECKSUMS"

printf '\n'
printf 'Upload these files to GitHub Releases: %s and %s\n' "$OUTPUT" "$CHECKSUMS"
printf 'Install with:\n'
printf '  curl -fsSL https://serverkit.ai/install.sh | INSTALL_FROM_RELEASE=1 bash\n\n'
