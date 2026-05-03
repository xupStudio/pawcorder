#!/usr/bin/env bash
# pawcorder installer
# - Verifies the host (Linux x86_64 with iGPU expected for OpenVINO)
# - Installs Docker + Compose plugin + ffmpeg + nmap if missing
# - Adds the invoking user to docker / video / render groups
# - Generates random secrets and writes the initial .env
# - Renders the Frigate config from the template
# - Brings up the admin panel and Frigate via docker compose
# - Prints the admin URL and password

set -euo pipefail

PAWCORDER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PAWCORDER_DIR"

# shellcheck source=scripts/lib.sh
source "$PAWCORDER_DIR/scripts/lib.sh"

main() {
  log_section "pawcorder installer"
  ensure_linux
  detect_distro
  detect_platform
  ensure_root_or_sudo

  ensure_packages curl ca-certificates ffmpeg nmap iproute2 openssl
  ensure_docker
  ensure_user_groups

  ensure_storage_dir
  ensure_env_file
  ensure_frigate_config
  ensure_wifi_scan_helper

  log_section "Starting services"
  $DOCKER_COMPOSE up -d --build --pull missing

  print_summary
}

main "$@"
