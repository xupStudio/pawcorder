#!/usr/bin/env bash
# pawcorder remote bootstrap
#
# Usage:  curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
#   or:   curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash -s -- --branch dev
#
# This is the thin shim a fresh box runs to clone the repo and hand
# off to install.sh. We keep it boring on purpose — no fancy progress
# bars, no recursive curl-bash chains, just:
#   1. sanity-check the host (Linux, has git, has curl)
#   2. pick an install dir (default /opt/pawcorder, override via $PAWCORDER_DIR)
#   3. git clone / git pull
#   4. exec ./install.sh
#
# Read the script before piping into bash. The full source is at
#   https://github.com/xupStudio/pawcorder/blob/main/scripts/bootstrap.sh

set -euo pipefail

REPO_URL="${PAWCORDER_REPO:-https://github.com/xupStudio/pawcorder.git}"
BRANCH="main"
INSTALL_DIR_DEFAULT="/opt/pawcorder"
INSTALL_DIR="${PAWCORDER_DIR:-$INSTALL_DIR_DEFAULT}"
INSTALL_DIR_USER_OVERRIDE=0
if [[ -n "${PAWCORDER_DIR:-}" ]]; then
  INSTALL_DIR_USER_OVERRIDE=1
fi

# ---- arg parsing -------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)   BRANCH="$2"; shift 2 ;;
    --dir)      INSTALL_DIR="$2"; INSTALL_DIR_USER_OVERRIDE=1; shift 2 ;;
    --repo)     REPO_URL="$2"; shift 2 ;;
    -h|--help)
      cat <<'HELP'
pawcorder bootstrap installer

Options:
  --branch <name>  git branch to clone (default: main)
  --dir <path>     install directory  (default: /opt/pawcorder on Linux,
                                       $HOME/pawcorder on macOS)
  --repo <url>     repository URL     (default: github.com/xupStudio/pawcorder)
  -h, --help       this help

Environment overrides: PAWCORDER_DIR, PAWCORDER_REPO

Examples:
  curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
  curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash -s -- --branch dev
  PAWCORDER_DIR=$HOME/pawcorder curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
HELP
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# macOS default: use $HOME/pawcorder rather than /opt/pawcorder (avoids sudo
# and matches the platform convention). Only applies when the user did not
# explicitly override --dir or PAWCORDER_DIR.
if [[ "$INSTALL_DIR_USER_OVERRIDE" -eq 0 && "$(uname -s)" == "Darwin" ]]; then
  INSTALL_DIR="$HOME/pawcorder"
fi

# ---- helpers -----------------------------------------------------------
red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n'    "$*"; }

die() { red "✗ $*"; exit 1; }

run_as_root() {
  # Re-exec with sudo if we need root for /opt/ — but skip when the
  # user already pointed --dir at a path they own (e.g. ~/pawcorder).
  # On macOS we never re-exec under sudo: the default install dir is
  # $HOME/pawcorder, and Homebrew/Docker Desktop must run as the user.
  if [[ "$(uname -s)" == "Darwin" ]]; then
    return
  fi
  local need_sudo=false
  if [[ "$INSTALL_DIR" == /opt/* || "$INSTALL_DIR" == /usr/* ]]; then
    need_sudo=true
  fi
  if $need_sudo && [[ "$EUID" -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
      die "$INSTALL_DIR needs root, and 'sudo' is not installed. Either install sudo or pass --dir \$HOME/pawcorder."
    fi
    bold "▶ Re-running with sudo (need root to write $INSTALL_DIR)"
    exec sudo --preserve-env=PAWCORDER_DIR,PAWCORDER_REPO bash -c "$(declare -f); BRANCH='$BRANCH' INSTALL_DIR='$INSTALL_DIR' REPO_URL='$REPO_URL' do_install"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1 — please install it first"
}

ensure_supported_os() {
  case "$(uname -s)" in
    Linux|Darwin) ;;
    MINGW*|CYGWIN*|MSYS*) die "Native Windows shell detected — re-run inside WSL2: from PowerShell, 'wsl --install' then run this script from your Linux distro." ;;
    *) die "unsupported OS: $(uname -s) — pawcorder runs on Linux or macOS. On Windows run inside WSL2; on a fresh box use a Pi 5 or any x86_64 Linux. Mac is fine for 1-2 cameras while evaluating (CPU mode only)." ;;
  esac
}

# ---- main install body (called inside or outside sudo) -----------------
do_install() {
  bold "▶ pawcorder bootstrap"
  echo  "  repo:    $REPO_URL"
  echo  "  branch:  $BRANCH"
  echo  "  dir:     $INSTALL_DIR"
  echo

  ensure_supported_os
  require_cmd git
  require_cmd curl

  # Fetch (or update) the repo. Plain git — keeps the bootstrap dep-free.
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    bold "▶ Updating existing checkout"
    git -C "$INSTALL_DIR" fetch --depth=1 origin "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
  else
    bold "▶ Cloning $REPO_URL ($BRANCH)"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi

  # Hand off to the full installer. install.sh handles everything else
  # (Docker, ffmpeg, .env generation, secret generation, docker compose up).
  bold "▶ Running the full installer"
  cd "$INSTALL_DIR"
  bash ./install.sh

  green ""
  green "✓ pawcorder is up. Check the URL + admin password printed above."
  green "  Source: $INSTALL_DIR"
  green "  Update later: cd $INSTALL_DIR && git pull && make update"
}

# Direct invocation: maybe re-exec under sudo, then run.
run_as_root
do_install
