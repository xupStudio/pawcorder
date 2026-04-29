# shellcheck shell=bash
# Shared helpers for pawcorder install/maintenance scripts.

set -euo pipefail

# ---- pretty output ------------------------------------------------------

if [[ -t 1 ]]; then
  C_RESET="$(printf '\033[0m')"
  C_BOLD="$(printf '\033[1m')"
  C_GREEN="$(printf '\033[32m')"
  C_YELLOW="$(printf '\033[33m')"
  C_RED="$(printf '\033[31m')"
  C_CYAN="$(printf '\033[36m')"
else
  C_RESET="" C_BOLD="" C_GREEN="" C_YELLOW="" C_RED="" C_CYAN=""
fi

log_section() { printf "\n%s==> %s%s\n" "$C_BOLD$C_CYAN" "$1" "$C_RESET"; }
log_info()    { printf "%s::%s %s\n" "$C_CYAN" "$C_RESET" "$*"; }
log_ok()      { printf "%sok%s %s\n"  "$C_GREEN" "$C_RESET" "$*"; }
log_warn()    { printf "%s!! %s%s\n" "$C_YELLOW" "$*" "$C_RESET"; }
log_error()   { printf "%s!!%s %s\n" "$C_RED" "$C_RESET" "$*" >&2; }
die()         { log_error "$*"; exit 1; }

# ---- environment probes ------------------------------------------------

_pawcorder_uname_kernel() {
  uname -s
}

ensure_linux() {
  local kernel
  kernel="$(_pawcorder_uname_kernel)"
  case "$kernel" in
    Linux|Darwin) ;;
    *) die "This script needs Linux or macOS (your kernel: $kernel). On Windows, run it inside WSL2." ;;
  esac
}

OS_KERNEL=""
OS_ARCH=""
HAS_INTEL_IGPU=0
HAS_NVIDIA_GPU=0
HAS_CORAL_USB=0
HAS_HAILO=0
RECOMMENDED_DETECTOR="cpu"
RECOMMENDED_COMPOSE_FILES="docker-compose.yml"

detect_platform() {
  OS_KERNEL="$(uname -s)"
  OS_ARCH="$(uname -m)"
  log_info "Kernel: $OS_KERNEL  Arch: $OS_ARCH"

  if [[ "$OS_KERNEL" != "Linux" ]]; then
    log_info "Non-Linux host — skipping accelerator probes (Docker Desktop runs in a VM)"
    return
  fi

  if [[ -e /dev/dri/renderD128 ]]; then
    HAS_INTEL_IGPU=1
    log_ok "Detected Intel iGPU at /dev/dri/renderD128"
  fi
  if command -v nvidia-smi >/dev/null 2>&1; then
    HAS_NVIDIA_GPU=1
    log_ok "Detected NVIDIA GPU (nvidia-smi present)"
  fi
  if command -v lsusb >/dev/null 2>&1 && lsusb 2>/dev/null | grep -qE "1a6e:089a|18d1:9302"; then
    HAS_CORAL_USB=1
    log_ok "Detected Google Coral USB"
  fi
  if [[ -e /dev/hailo0 ]]; then
    HAS_HAILO=1
    log_ok "Detected Hailo accelerator at /dev/hailo0"
  fi

  # Pick the best detector — order matches platform_detect.recommended_detector
  if [[ $HAS_HAILO -eq 1 ]]; then
    RECOMMENDED_DETECTOR="hailo8l"
  elif [[ $HAS_NVIDIA_GPU -eq 1 ]]; then
    RECOMMENDED_DETECTOR="tensorrt"
    RECOMMENDED_COMPOSE_FILES="docker-compose.yml:docker-compose.linux-nvidia.yml"
  elif [[ $HAS_CORAL_USB -eq 1 ]]; then
    RECOMMENDED_DETECTOR="edgetpu"
  elif [[ $HAS_INTEL_IGPU -eq 1 ]]; then
    RECOMMENDED_DETECTOR="openvino"
    RECOMMENDED_COMPOSE_FILES="docker-compose.yml:docker-compose.linux-igpu.yml"
  fi
  log_info "Recommended Frigate detector: $RECOMMENDED_DETECTOR"
}

DISTRO=""
DISTRO_FAMILY=""
detect_distro() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO="${ID:-unknown}"
  else
    DISTRO="unknown"
  fi
  case "$DISTRO" in
    ubuntu|debian|raspbian|linuxmint|pop) DISTRO_FAMILY="debian" ;;
    fedora|rhel|centos|rocky|almalinux)   DISTRO_FAMILY="rhel"   ;;
    arch|manjaro)                          DISTRO_FAMILY="arch"   ;;
    *)                                     DISTRO_FAMILY="unknown" ;;
  esac
  log_info "Detected distro: ${DISTRO} (${DISTRO_FAMILY})"
}

SUDO=""
ensure_root_or_sudo() {
  if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    die "Run this script as root, or install sudo first."
  fi
}

# ---- package installation ----------------------------------------------

apt_update_done=0
ensure_packages() {
  case "$DISTRO_FAMILY" in
    debian)
      if [[ $apt_update_done -eq 0 ]]; then
        $SUDO apt-get update -y
        apt_update_done=1
      fi
      $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
      ;;
    rhel)
      $SUDO dnf install -y "$@"
      ;;
    arch)
      $SUDO pacman -Sy --noconfirm --needed "$@"
      ;;
    *)
      log_warn "Unknown distro family. Please install manually: $*"
      ;;
  esac
}

# ---- docker -------------------------------------------------------------

# shellcheck disable=SC2034  # used by callers that source this file (install.sh)
DOCKER_COMPOSE=""

ensure_docker() {
  local kernel
  kernel="$(_pawcorder_uname_kernel)"
  case "$kernel" in
    Darwin) _ensure_docker_macos ;;
    Linux)
      if _is_wsl1; then
        die "WSL1 detected — Docker Engine requires WSL2. From Windows PowerShell run: wsl --set-version <distro> 2"
      elif _is_wsl2; then
        _ensure_docker_wsl2
      else
        _ensure_docker_linux
      fi
      ;;
    *) die "Cannot install Docker automatically on $kernel — install it manually and re-run." ;;
  esac
}

# Docker Desktop installer for macOS. Installs Homebrew if needed, then
# `brew install --cask docker`, launches Docker Desktop, and polls until
# `docker info` succeeds (covers the user clicking through the first-run
# license / privileged-helper dialog).
_ensure_docker_macos() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
     && docker info >/dev/null 2>&1; then
    # shellcheck disable=SC2034  # consumed by install.sh after sourcing
    DOCKER_COMPOSE="docker compose"
    log_ok "Docker Desktop + compose already installed and running"
    return
  fi

  # Bring brew onto this shell's PATH. curl|bash subshells often start
  # without /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel) on
  # PATH even when Homebrew is installed, so we always re-source shellenv
  # below. The official Homebrew installer is only run when brew is missing
  # from disk entirely.
  if ! command -v brew >/dev/null 2>&1; then
    if [[ ! -x /opt/homebrew/bin/brew && ! -x /usr/local/bin/brew ]]; then
      if [[ ! -t 0 ]]; then
        die "Homebrew installer needs an interactive terminal (sudo prompt). Re-run install.sh directly from a terminal — not piped via curl|bash."
      fi
      log_info "Homebrew is required to install Docker Desktop on macOS."
      log_warn "The Homebrew installer will prompt for your password (sudo) to write under /opt/homebrew or /usr/local."
      # Mirrors Homebrew's documented install pattern; checksum is implicit
      # in TLS to githubusercontent.com.
      NONINTERACTIVE=1 /bin/bash -c \
        "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    # brew on disk but not on PATH — eval shellenv (PATH/MANPATH exports only).
    if [[ -x /opt/homebrew/bin/brew ]]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi

  if [[ -d /Applications/Docker.app ]]; then
    log_ok "Docker Desktop already present in /Applications"
  else
    log_info "Installing Docker Desktop via Homebrew (brew install --cask docker)…"
    brew install --cask docker
  fi

  if [[ ! -d /Applications/Docker.app ]]; then
    die "/Applications/Docker.app not found after install. Check 'brew install --cask docker' output above."
  fi

  log_info "Launching Docker Desktop…"
  open -ga Docker || log_warn "Could not invoke 'open -ga Docker'. Launch /Applications/Docker.app manually."

  local timeout="${PAWCORDER_DOCKER_BOOT_TIMEOUT:-300}"
  local interval=5
  log_info "Waiting up to ${timeout}s for Docker Desktop to become ready (you may need to click through the first-run license)…"
  local waited=0
  while (( waited < timeout )); do
    if docker info >/dev/null 2>&1; then
      # shellcheck disable=SC2034  # consumed by install.sh after sourcing
      DOCKER_COMPOSE="docker compose"
      log_ok "Docker Desktop is up after ${waited}s"
      return
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done

  die "Docker Desktop didn't start within ${timeout}s — launch /Applications/Docker.app, accept the license, then re-run install.sh. (Override the wait with PAWCORDER_DOCKER_BOOT_TIMEOUT=600.)"
}

# True iff /proc/version indicates WSL2 specifically. WSL1 contains
# "Microsoft" but not "WSL2"; we treat WSL1 separately because it has no
# real Linux kernel and can't run Docker Engine.
_is_wsl2() {
  grep -qi 'WSL2' /proc/version 2>/dev/null
}

_is_wsl1() {
  grep -qi microsoft /proc/version 2>/dev/null && ! _is_wsl2
}

# Docker Engine inside WSL2. We deliberately install Docker Engine (not
# Docker Desktop for Windows) because (a) this script is bash and already
# runs there, (b) avoids winget cross-boundary fragility, (c) Frigate's
# hardware accelerator passthrough on Windows is broken regardless.
_ensure_docker_wsl2() {
  log_warn "WSL2 detected — installing Docker Engine inside WSL2 (not Docker Desktop for Windows)."
  log_info "If 'systemctl' fails: from Windows PowerShell run 'wsl --update', add '[boot] systemd=true' to /etc/wsl.conf, then 'wsl --shutdown'."
  _ensure_docker_linux
  # _ensure_docker_linux uses `systemctl enable --now docker || true` which
  # fails silently on WSL2 without systemd. Verify the daemon is actually
  # running so install.sh's docker-compose call doesn't blow up cryptically.
  if ! docker info >/dev/null 2>&1; then
    die "Docker installed but daemon isn't running. WSL2 needs systemd — see the advisory above. Your install is HALF-DONE: Docker packages are on disk and the second run of install.sh will reuse them, so once you enable systemd the re-run is cheap."
  fi
}

_ensure_docker_linux() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    # shellcheck disable=SC2034  # consumed by install.sh after sourcing
    DOCKER_COMPOSE="docker compose"
    log_ok "Docker + compose plugin already installed"
    return
  fi
  log_info "Installing Docker via the official convenience script…"
  if ! command -v curl >/dev/null 2>&1; then
    ensure_packages curl ca-certificates
  fi
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker || true
  if ! docker compose version >/dev/null 2>&1; then
    case "$DISTRO_FAMILY" in
      debian) ensure_packages docker-compose-plugin ;;
      rhel)   ensure_packages docker-compose-plugin ;;
      arch)   ensure_packages docker-compose ;;
    esac
  fi
  # shellcheck disable=SC2034  # consumed by install.sh after sourcing
  DOCKER_COMPOSE="docker compose"
  log_ok "Docker installed"
}

ensure_user_groups() {
  local target="${SUDO_USER:-$USER}"
  [[ -z "$target" || "$target" == "root" ]] && return
  for group in docker video render; do
    if getent group "$group" >/dev/null && ! id -nG "$target" | tr ' ' '\n' | grep -qx "$group"; then
      $SUDO usermod -aG "$group" "$target" || true
      log_info "Added $target to group $group (re-login required for it to take effect)"
    fi
  done
}

# ---- pawcorder bootstrap ------------------------------------------------

ensure_storage_dir() {
  source_env_value STORAGE_PATH
  local path="${ENV_VALUE:-./storage}"
  if [[ "$path" == ./* || "$path" != /* ]]; then
    path="$PAWCORDER_DIR/${path#./}"
  fi
  if [[ ! -d "$path" ]]; then
    $SUDO mkdir -p "$path"
    $SUDO chown "$(id -un):$(id -gn)" "$path" 2>/dev/null || true
    log_ok "Created storage directory: $path"
  fi
}

# Read a single key from .env into ENV_VALUE without sourcing the file.
ENV_VALUE=""
source_env_value() {
  local key="$1"
  ENV_VALUE=""
  [[ -f "$PAWCORDER_DIR/.env" ]] || return 0
  ENV_VALUE="$(awk -F= -v k="$key" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    {
      sub(/^[[:space:]]+/, "", $1); sub(/[[:space:]]+$/, "", $1);
      if ($1 == k) {
        # Re-join everything after the first = to handle values with =.
        $1=""; sub(/^=/, ""); sub(/^[[:space:]]+/, "");
        gsub(/^["\x27]|["\x27]$/, "");
        print; exit
      }
    }' "$PAWCORDER_DIR/.env" || true)"
}

random_hex() { openssl rand -hex "${1:-16}" 2>/dev/null || head -c "$((${1:-16} * 2))" /dev/urandom | xxd -p -c 256; }
random_alnum() {
  local len="${1:-20}"
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$len"
}

ensure_env_file() {
  if [[ -f "$PAWCORDER_DIR/.env" ]]; then
    log_ok ".env already present, leaving as-is"
    return
  fi
  log_info "Generating initial .env with random secrets…"
  local admin_pw frigate_rtsp_pw session_secret
  admin_pw="$(random_alnum 16)"
  frigate_rtsp_pw="$(random_alnum 24)"
  session_secret="$(random_hex 32)"
  cat > "$PAWCORDER_DIR/.env" <<EOF
# pawcorder host-wide configuration. Generated by install.sh.
# Per-camera config lives in config/cameras.yml (managed by admin panel).
STORAGE_PATH="$PAWCORDER_DIR/storage"
FRIGATE_RTSP_PASSWORD="$frigate_rtsp_pw"
TZ="Asia/Taipei"
PET_MIN_SCORE="0.65"
PET_THRESHOLD="0.70"
ADMIN_PASSWORD="$admin_pw"
ADMIN_SESSION_SECRET="$session_secret"
TAILSCALE_HOSTNAME=""
TELEGRAM_ENABLED="0"
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
LINE_ENABLED="0"
LINE_CHANNEL_TOKEN=""
LINE_TARGET_ID=""
ADMIN_LANG="zh-TW"
TRACK_CAT="1"
TRACK_DOG="1"
TRACK_PERSON="1"
DETECTOR_TYPE="${RECOMMENDED_DETECTOR:-cpu}"
COMPOSE_FILE="${RECOMMENDED_COMPOSE_FILES:-docker-compose.yml}"
EOF
  chmod 600 "$PAWCORDER_DIR/.env"
  GENERATED_ADMIN_PASSWORD="$admin_pw"
  log_ok "Wrote .env (mode 0600). Admin password generated."
}

ensure_frigate_config() {
  # Frigate reads /config/config.yml. The admin panel renders this from
  # config/frigate.template.yml + cameras.yml after the user adds at least
  # one camera. Until then, Frigate will fail to start — that's expected.
  mkdir -p "$PAWCORDER_DIR/config"
}

print_summary() {
  local lan_ip admin_pw
  lan_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
  lan_ip="${lan_ip:-127.0.0.1}"
  source_env_value ADMIN_PASSWORD
  admin_pw="${GENERATED_ADMIN_PASSWORD:-${ENV_VALUE:-(see .env)}}"

  cat <<EOF

${C_BOLD}${C_GREEN}pawcorder is up.${C_RESET}

  Admin panel:   http://$lan_ip:8080
  Admin pwd:     $admin_pw

After completing the setup wizard:

  Frigate UI:    http://$lan_ip:5000

To make this reachable from outside your home network, install Tailscale:

  ./scripts/install-tailscale.sh

To mount your NAS at the storage path:

  ./scripts/mount-nas.sh

Logs:  docker compose logs -f
Stop:  docker compose down
EOF
}
