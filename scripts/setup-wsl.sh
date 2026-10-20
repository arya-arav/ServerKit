#!/usr/bin/env bash
# ServerKit WSL setup helper.
#
# One-shot installer for Windows users running ServerKit dev inside
# WSL2 (Ubuntu/Debian). Installs system packages, creates the Python
# venv, installs npm dependencies, prepares .env files, and verifies
# common WSL pitfalls (filesystem location, localhost binding,
# polling-based reloader).
#
# Usage:
#   bash scripts/setup-wsl.sh           # full setup
#   bash scripts/setup-wsl.sh --check   # diagnostics only, no install
#
# After this finishes:
#   ./dev.sh           # start backend + frontend
#   ./dev.sh tunnel    # also expose backend through ngrok

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
DIM='\033[2m'
NC='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
CHECK_ONLY=0

for arg in "$@"; do
    case "$arg" in
        --check|-c) CHECK_ONLY=1 ;;
        --help|-h)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo -e "${RED}Unknown argument:${NC} $arg"; exit 2 ;;
    esac
done

header() { echo ""; echo -e "${CYAN}=== $1 ===${NC}"; }
ok()     { echo -e "  ${GREEN}OK${NC}   $1"; }
warn()   { echo -e "  ${YELLOW}WARN${NC} $1"; }
fail()   { echo -e "  ${RED}FAIL${NC} $1"; }
info()   { echo -e "  ${DIM}$1${NC}"; }

# -----------------------------------------------------------------------------
# Diagnostics
# -----------------------------------------------------------------------------
header "Environment diagnostics"

IN_WSL=0
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
    IN_WSL=1
    ok "Running inside WSL ($(uname -r))"
else
    warn "Not detected as WSL. This script is intended for WSL2 but will still try to run."
fi

if [ "$IN_WSL" -eq 1 ]; then
    case "$PROJECT_ROOT" in
        /mnt/*)
            warn "Repo is on the Windows filesystem ($PROJECT_ROOT)."
            info "File I/O and Vite/Flask reloaders are noticeably slower here."
            info "For best performance clone the repo into the Linux home, e.g.:"
            info "  cd ~ && git clone https://github.com/jhd3197/ServerKit.git"
            ;;
        *)
            ok "Repo is on the Linux filesystem ($PROJECT_ROOT)."
            ;;
    esac
fi

# Distro detection
DISTRO_ID="unknown"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="$ID"
    ok "Distro: $PRETTY_NAME"
fi

if [ "$DISTRO_ID" != "ubuntu" ] && [ "$DISTRO_ID" != "debian" ]; then
    warn "This script targets Ubuntu/Debian. Other distros may need manual adjustments."
fi

# -----------------------------------------------------------------------------
# Required tools
# -----------------------------------------------------------------------------
header "Required tooling"

NEED_APT=()
need() {
    local cmd="$1" pkg="$2"
    if command -v "$cmd" &>/dev/null; then
        ok "$cmd ($(command -v "$cmd"))"
    else
        warn "$cmd not found (needs apt package: $pkg)"
        NEED_APT+=("$pkg")
    fi
}

need python3        python3
need pip3           python3-pip
need node           nodejs
need npm            npm
need git            git
need curl           curl

# venv module
if python3 -c 'import venv' 2>/dev/null; then
    ok "python3-venv module available"
else
    warn "python3-venv module missing"
    NEED_APT+=("python3-venv")
fi

# Node version sanity
if command -v node &>/dev/null; then
    NODE_MAJOR="$(node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')"
    if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 18 ]; then
        warn "Node.js version is $(node -v); ServerKit expects 18+ (20+ recommended)."
        info "Upgrade with NodeSource: https://github.com/nodesource/distributions"
    fi
fi

# -----------------------------------------------------------------------------
# Install missing apt packages
# -----------------------------------------------------------------------------
if [ "${#NEED_APT[@]}" -gt 0 ]; then
    if [ "$CHECK_ONLY" -eq 1 ]; then
        warn "Missing packages (would install): ${NEED_APT[*]}"
    else
        header "Installing missing apt packages"
        info "Running: sudo apt update && sudo apt install -y ${NEED_APT[*]}"
        sudo apt update
        sudo apt install -y "${NEED_APT[@]}"
    fi
fi

# -----------------------------------------------------------------------------
# Backend venv + requirements
# -----------------------------------------------------------------------------
header "Backend (Python venv)"

if [ ! -d "$BACKEND_DIR" ]; then
    fail "backend directory not found at $BACKEND_DIR"
    exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    if [ -d "$BACKEND_DIR/venv" ]; then
        ok "venv exists at backend/venv"
    else
        warn "venv not yet created (run without --check to create it)"
    fi
else
    if [ ! -d "$BACKEND_DIR/venv" ]; then
        info "Creating venv in backend/venv"
        python3 -m venv "$BACKEND_DIR/venv"
    fi
    # shellcheck disable=SC1091
    source "$BACKEND_DIR/venv/bin/activate"
    info "Upgrading pip"
    pip install --quiet --upgrade pip
    info "Installing backend requirements"
    pip install --quiet -r "$BACKEND_DIR/requirements.txt"
    deactivate
    ok "Backend dependencies installed"
fi

# .env
if [ ! -f "$BACKEND_DIR/.env" ] && [ -f "$BACKEND_DIR/.env.example" ]; then
    if [ "$CHECK_ONLY" -eq 1 ]; then
        warn "backend/.env missing (would copy from .env.example)"
    else
        cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
        ok "Created backend/.env from .env.example"
        info "Edit backend/.env to set SECRET_KEY / JWT_SECRET_KEY before any production-like use."
    fi
else
    [ -f "$BACKEND_DIR/.env" ] && ok "backend/.env present"
fi

# -----------------------------------------------------------------------------
# Frontend npm install
# -----------------------------------------------------------------------------
header "Frontend (npm)"

if [ ! -d "$FRONTEND_DIR" ]; then
    fail "frontend directory not found at $FRONTEND_DIR"
    exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    if [ -d "$FRONTEND_DIR/node_modules" ]; then
        ok "node_modules present"
    else
        warn "node_modules missing (would run npm install)"
    fi
else
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        info "Running npm install in frontend/"
        (cd "$FRONTEND_DIR" && npm install)
    else
        info "node_modules already present (skipping npm install)"
    fi
    ok "Frontend dependencies ready"
fi

# -----------------------------------------------------------------------------
# WSL-specific advice
# -----------------------------------------------------------------------------
if [ "$IN_WSL" -eq 1 ]; then
    header "WSL tips"
    info "Backend already binds to 0.0.0.0 and uses a stat-based reloader (good for WSL)."
    info "Vite dev server is reachable from Windows at http://localhost:41921 thanks to WSL2 localhost forwarding."
    info "If hot-reload feels stuck, ensure the repo lives under your Linux home (~) rather than /mnt/c."
    if command -v ngrok &>/dev/null; then
        ok "ngrok is installed (use ./dev.sh tunnel to expose the backend)"
    else
        info "Install ngrok and then ./dev.sh tunnel will expose the backend so remote agents can connect."
    fi
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
header "Done"
if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "Diagnostics complete. Re-run without --check to install anything missing."
else
    echo -e "  ${GREEN}Setup complete.${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. (Optional) edit backend/.env to set unique SECRET_KEY / JWT_SECRET_KEY"
    echo "  2. ./dev.sh           # start backend + frontend"
    echo "  3. ./dev.sh tunnel    # also expose backend via ngrok"
fi
