#!/usr/bin/env bash
# Install Tailscale on the host so you can reach pawcorder from anywhere
# without exposing ports to the public internet.

set -euo pipefail

PAWCORDER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "$PAWCORDER_DIR/scripts/lib.sh"

ensure_linux
detect_distro
ensure_root_or_sudo

if command -v tailscale >/dev/null 2>&1; then
  log_ok "Tailscale already installed"
else
  log_info "Installing Tailscale via the official installer…"
  curl -fsSL https://tailscale.com/install.sh | $SUDO sh
fi

$SUDO systemctl enable --now tailscaled || true

log_section "Bring Tailscale up"
echo "Run the following to authenticate. A login URL will be printed."
echo "  $SUDO tailscale up"
echo
echo "After auth, your pawcorder host becomes reachable at: <hostname>.<tailnet>.ts.net"
echo "On your phone, install the Tailscale app and sign into the same account."
echo
echo "Then open: http://<hostname>.<tailnet>.ts.net:5000  (Frigate UI)"
echo "      and: http://<hostname>.<tailnet>.ts.net:8080  (admin panel)"
