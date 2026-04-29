#!/usr/bin/env bash
# Interactively add a NAS share to /etc/fstab and mount it at the
# pawcorder STORAGE_PATH. Supports SMB (cifs) and NFS.

set -euo pipefail

PAWCORDER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "$PAWCORDER_DIR/scripts/lib.sh"

ensure_linux
detect_distro
ensure_root_or_sudo

source_env_value STORAGE_PATH
mount_point="${ENV_VALUE:-/mnt/pawcorder}"

read -rp "Mount point [$mount_point]: " input_mount
mount_point="${input_mount:-$mount_point}"

read -rp "NAS protocol (smb/nfs) [smb]: " proto
proto="${proto:-smb}"

case "$proto" in
  smb|cifs)
    ensure_packages cifs-utils
    read -rp "NAS IP/hostname: " nas_host
    read -rp "Share name (e.g. pawcorder): " share
    read -rp "Username: " smb_user
    read -rsp "Password: " smb_pw; echo
    cred_file="/etc/pawcorder-nas.cred"
    $SUDO bash -c "umask 077 && cat > '$cred_file' <<EOF
username=$smb_user
password=$smb_pw
EOF"
    $SUDO mkdir -p "$mount_point"
    fstab_line="//$nas_host/$share $mount_point cifs credentials=$cred_file,uid=$(id -u),gid=$(id -g),vers=3.0,nofail,_netdev 0 0"
    ;;
  nfs)
    ensure_packages nfs-common
    read -rp "NAS IP/hostname: " nas_host
    read -rp "Export path (e.g. /mnt/pool/pawcorder): " export_path
    $SUDO mkdir -p "$mount_point"
    fstab_line="$nas_host:$export_path $mount_point nfs defaults,nofail,_netdev 0 0"
    ;;
  *)
    die "Unsupported protocol: $proto"
    ;;
esac

if grep -qF " $mount_point " /etc/fstab 2>/dev/null; then
  log_warn "$mount_point already in /etc/fstab — skipping update."
else
  echo "$fstab_line" | $SUDO tee -a /etc/fstab >/dev/null
  log_ok "Added /etc/fstab entry"
fi

$SUDO mount -a
log_ok "Mounted. Verify with: df -h $mount_point"

log_info "Updating .env STORAGE_PATH=$mount_point"
if [[ -f "$PAWCORDER_DIR/.env" ]]; then
  if grep -q '^STORAGE_PATH=' "$PAWCORDER_DIR/.env"; then
    sed -i.bak -E "s|^STORAGE_PATH=.*$|STORAGE_PATH=\"$mount_point\"|" "$PAWCORDER_DIR/.env"
  else
    echo "STORAGE_PATH=\"$mount_point\"" >> "$PAWCORDER_DIR/.env"
  fi
fi

log_info "Run \`make down && make up\` to apply the new storage path to Frigate."
