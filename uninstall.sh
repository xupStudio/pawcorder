#!/usr/bin/env bash
# uninstall.sh — remove pawcorder from this host.
#
# Three modes, each clearly labeled. Each mode prints exactly what it
# will touch BEFORE doing anything; you have to type 'yes' to commit.
#
# Usage:
#   ./uninstall.sh            # interactive — pick a mode
#   ./uninstall.sh --soft     # stop containers + remove images
#   ./uninstall.sh --full     # soft + remove the pawcorder/ directory
#   ./uninstall.sh --nuke     # full + delete recordings at STORAGE_PATH

set -euo pipefail

PAWCORDER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# ANSI colors — only when stdout is a TTY.
if [ -t 1 ]; then
  RED='\033[31m'; YELLOW='\033[33m'; CYAN='\033[36m'; BOLD='\033[1m'; RESET='\033[0m'
else
  RED=''; YELLOW=''; CYAN=''; BOLD=''; RESET=''
fi

log()    { printf '%s\n' "$1"; }
title()  { printf "${BOLD}%s${RESET}\n" "$1"; }
warn()   { printf "${YELLOW}%s${RESET}\n" "$1"; }
danger() { printf "${RED}%s${RESET}\n" "$1"; }
info()   { printf "${CYAN}%s${RESET}\n" "$1"; }

# Read STORAGE_PATH from .env if it exists; default otherwise.
read_storage_path() {
  if [ -f "$PAWCORDER_DIR/.env" ]; then
    local v
    v=$(grep -E '^STORAGE_PATH=' "$PAWCORDER_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
    if [ -n "$v" ]; then
      printf '%s' "$v"
      return
    fi
  fi
  printf '%s' "/mnt/pawcorder"
}

confirm() {
  local prompt="$1"
  local answer
  printf "%s [type 'yes' to continue]: " "$prompt"
  read -r answer
  [ "$answer" = "yes" ]
}

stop_containers() {
  if command -v docker >/dev/null 2>&1; then
    info "Stopping containers (docker compose down)…"
    (cd "$PAWCORDER_DIR" && docker compose down "$@") || warn "compose down failed (containers may not be running)"
  else
    warn "Docker not found — skipping container teardown."
  fi
}

remove_images() {
  if command -v docker >/dev/null 2>&1; then
    info "Removing pawcorder Docker images…"
    docker rmi pawcorder/admin:local 2>/dev/null || true
    docker rmi ghcr.io/blakeblackshear/frigate:stable 2>/dev/null || true
    docker rmi containrrr/watchtower:1.7.1 2>/dev/null || true
  fi
}

remove_project_dir() {
  local parent
  parent="$(dirname "$PAWCORDER_DIR")"
  warn "Removing project directory: $PAWCORDER_DIR"
  # Use rm in the parent so the cwd we're running from isn't the doomed dir.
  (cd "$parent" && rm -rf "$(basename "$PAWCORDER_DIR")")
}

remove_recordings() {
  local storage
  storage="$(read_storage_path)"
  if [ -d "$storage" ]; then
    danger "Removing recordings at: $storage"
    rm -rf -- "$storage"
  else
    info "No recordings directory at $storage — skipping."
  fi
}

mode_soft() {
  title "Soft uninstall — stop containers + remove images"
  log "Will stop and remove these containers + images:"
  log "  - pawcorder-admin (image: pawcorder/admin:local)"
  log "  - pawcorder-frigate (image: ghcr.io/blakeblackshear/frigate:stable)"
  log "  - pawcorder-watchtower (image: containrrr/watchtower:1.7.1)"
  log
  log "${BOLD}Will keep:${RESET}"
  log "  - $PAWCORDER_DIR (your settings, including .env with admin password)"
  log "  - your recordings at $(read_storage_path)"
  log
  confirm "Proceed?" || { log "Aborted."; exit 1; }
  stop_containers
  remove_images
  log
  info "Done. Your settings and recordings are intact."
  log "To bring pawcorder back: cd $PAWCORDER_DIR && ./install.sh"
}

mode_full() {
  title "Full uninstall — Soft + remove the pawcorder folder"
  log "Will stop and remove containers + images (same as --soft) AND:"
  log "  - delete the entire $PAWCORDER_DIR directory"
  log "  - your settings (.env, cameras.yml, pet photos, OAuth tokens) are GONE"
  log
  log "${BOLD}Will keep:${RESET}"
  log "  - your recordings at $(read_storage_path)"
  log
  warn "Settings cannot be recovered after this. Run 'make backup' from"
  warn "the admin panel first if you might come back."
  log
  confirm "Proceed with full uninstall?" || { log "Aborted."; exit 1; }
  stop_containers -v
  remove_images
  remove_project_dir
  log
  info "pawcorder removed. Your recordings are still at $(read_storage_path)."
}

mode_nuke() {
  local storage
  storage="$(read_storage_path)"
  title "Nuclear uninstall — Full + delete recordings"
  danger "This deletes EVERYTHING pawcorder ever wrote on this host:"
  log "  - all containers and images"
  log "  - the entire $PAWCORDER_DIR directory (settings, OAuth tokens, pet photos)"
  log "  - all recordings at $storage"
  if [ -d "$storage" ]; then
    local size
    size=$(du -sh "$storage" 2>/dev/null | cut -f1 || printf '?')
    danger "  ⚠  recordings dir size: $size"
  fi
  log
  danger "This cannot be undone."
  log
  confirm "Type 'yes' to delete EVERYTHING" || { log "Aborted."; exit 1; }
  confirm "Are you absolutely sure?" || { log "Aborted."; exit 1; }
  stop_containers -v
  remove_images
  remove_project_dir
  remove_recordings
  log
  info "pawcorder fully removed. Nothing left on this host."
}

usage() {
  cat <<EOF
Usage: $0 [--soft|--full|--nuke]

  --soft   Stop containers and remove images. Keep settings + recordings.
  --full   Soft + remove the $PAWCORDER_DIR directory. Keep recordings.
  --nuke   Full + delete recordings. Cannot be undone.

  No flag prompts you to choose interactively.
EOF
}

main() {
  local mode="${1:-}"
  if [ -z "$mode" ]; then
    title "pawcorder uninstall"
    log "  1) soft  — stop + remove images, keep everything else"
    log "  2) full  — also remove $PAWCORDER_DIR (settings gone)"
    log "  3) nuke  — also delete recordings at $(read_storage_path)"
    log "  q) quit"
    log
    printf "Choose [1/2/3/q]: "
    read -r choice
    case "$choice" in
      1) mode_soft ;;
      2) mode_full ;;
      3) mode_nuke ;;
      *) log "Aborted."; exit 0 ;;
    esac
    return
  fi
  case "$mode" in
    --soft) mode_soft ;;
    --full) mode_full ;;
    --nuke) mode_nuke ;;
    -h|--help) usage ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
