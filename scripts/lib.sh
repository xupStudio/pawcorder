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
    return
  fi
  # macOS: the default install path is $HOME/pawcorder (per bootstrap.sh)
  # and Homebrew/Docker-Desktop reject sudo, so we never run anything as
  # root on Darwin. Users running under their own UID is the supported
  # path. If someone explicitly sets PAWCORDER_DIR to /opt/* on macOS
  # they should re-run install.sh under `sudo bash install.sh` themselves.
  if [[ "$(_pawcorder_uname_kernel)" == "Darwin" ]]; then
    SUDO=""
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    die "Run this script as root, or install sudo first."
  fi
}

# Resolve the host's LAN IPv4 in a way that works across iproute2 (Linux),
# iproute2mac (macOS), and WSL2. WSL2 in default bridged mode runs on a
# virtual subnet (e.g. 172.20.x.x) that's invisible from the LAN — but
# Windows auto-forwards localhost:8080 from WSL2 to the host, so for
# Windows users the practical access URL is http://localhost:8080.
_pawcorder_lan_ip() {
  local kernel ip
  kernel="$(_pawcorder_uname_kernel)"
  if [[ "$kernel" == "Darwin" ]]; then
    # Default-route interface → its IPv4. Falls back across en0/en1.
    local iface
    iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
    if [[ -n "$iface" ]]; then
      ip="$(ipconfig getifaddr "$iface" 2>/dev/null)"
    fi
    if [[ -z "$ip" ]]; then
      for iface in en0 en1 en2; do
        ip="$(ipconfig getifaddr "$iface" 2>/dev/null)"
        [[ -n "$ip" ]] && break
      done
    fi
  elif _is_wsl2; then
    # WSL2 → use localhost; Windows port-forwards it to the WSL2 instance.
    # We don't return the WSL eth0 (172.20.x.x) because that subnet isn't
    # reachable from the user's phone or other LAN devices.
    ip="localhost"
  else
    ip="$(ip -4 -o addr show scope global 2>/dev/null \
          | awk '{print $4}' | cut -d/ -f1 | head -n1)"
  fi
  printf '%s' "${ip:-127.0.0.1}"
}

# ---- package installation ----------------------------------------------

apt_update_done=0
ensure_packages() {
  # Darwin: install via Homebrew. Picks up the Linux package list and
  # translates names that differ on macOS (e.g. iproute2 → iproute2mac,
  # ca-certificates ships with the system). This branch is what makes
  # `bootstrap.sh` actually install nmap / ffmpeg on a fresh Mac — the
  # admin's /api/scan and Frigate both need those binaries.
  if [[ "$(_pawcorder_uname_kernel)" == "Darwin" ]]; then
    _ensure_brew_packages "$@"
    return
  fi
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

# Make `brew` runnable in this shell. Re-sources Homebrew's shellenv when
# brew exists on disk but isn't on PATH (curl|bash subshells often start
# without /opt/homebrew/bin or /usr/local/bin), and runs the Homebrew
# installer if brew is missing entirely. Returns 0 on success, dies if it
# can't get a working brew.
_ensure_brew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  if [[ ! -x /opt/homebrew/bin/brew && ! -x /usr/local/bin/brew ]]; then
    if [[ ! -t 0 ]]; then
      die "Homebrew is required on macOS. The installer needs an interactive terminal (sudo prompt). Re-run install.sh from a terminal — not piped via curl|bash."
    fi
    log_info "Installing Homebrew (one-time, will prompt for your password)…"
    NONINTERACTIVE=1 /bin/bash -c \
      "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  command -v brew >/dev/null 2>&1 || die "Homebrew installation failed — check the output above."
}

# Map a Linux package name to its macOS / Homebrew equivalent. Echoes the
# brew formula name, or the empty string if the package is built into
# macOS and shouldn't be installed at all.
_pkg_macos_alias() {
  case "$1" in
    ca-certificates) echo "" ;;       # macOS keychain bundles this
    iproute2)        echo "iproute2mac" ;;  # provides `ip` command
    *)               echo "$1" ;;     # nmap/ffmpeg/openssl/curl share names
  esac
}

_ensure_brew_packages() {
  _ensure_brew
  local pkg formula
  for pkg in "$@"; do
    formula="$(_pkg_macos_alias "$pkg")"
    [[ -z "$formula" ]] && { log_ok "$pkg ships with macOS — skipped"; continue; }
    if brew list --formula "$formula" >/dev/null 2>&1; then
      log_ok "$formula already installed"
    else
      log_info "Installing $formula via Homebrew…"
      brew install "$formula" || log_warn "Failed to install $formula — continuing."
    fi
  done
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
  # Pre-flight: WSL2 needs systemd before Docker can run as a service.
  # If systemd is missing we (a) write the opt-in to /etc/wsl.conf so
  # the *next* WSL2 boot has it, (b) tell the user to run `wsl --shutdown`
  # from Windows PowerShell once, then re-run this installer. Doing it
  # before installing Docker means the user doesn't end up with a
  # half-done Docker on disk.
  if ! _wsl2_has_systemd; then
    _wsl2_write_systemd_conf
    cat <<EOF

  WSL2 doesn't have systemd enabled yet. Pawcorder needs it for Docker.

  We just wrote '[boot] systemd=true' to /etc/wsl.conf for you.

  ${C_BOLD}One manual step remains${C_RESET}: from Windows PowerShell, run
        ${C_CYAN}wsl --shutdown${C_RESET}
  Then re-open your WSL2 terminal and re-run:
        ${C_CYAN}bash $PAWCORDER_DIR/install.sh${C_RESET}

  Nothing else to undo — install state is saved, the second run is cheap.
EOF
    exit 0
  fi
  _ensure_docker_linux
  # _ensure_docker_linux uses `systemctl enable --now docker || true` which
  # fails silently on WSL2 without systemd. Verify the daemon is actually
  # running so install.sh's docker-compose call doesn't blow up cryptically.
  if ! docker info >/dev/null 2>&1; then
    die "Docker installed but daemon isn't running. Try: sudo service docker start"
  fi
}

# True iff we appear to be running under PID-1 systemd. WSL2 with the
# '[boot] systemd=true' flag boots Ubuntu under systemd; without the flag
# PID 1 is /init (Microsoft's WSL init), and systemctl can't talk to a
# manager because there isn't one.
_wsl2_has_systemd() {
  [[ -d /run/systemd/system ]]
}

_wsl2_write_systemd_conf() {
  local conf=/etc/wsl.conf
  if [[ -f "$conf" ]] && grep -q '^\s*systemd\s*=\s*true' "$conf"; then
    return 0
  fi
  log_info "Writing '[boot] systemd=true' to $conf (sudo)…"
  $SUDO bash -c "
    if [[ ! -f $conf ]]; then
      printf '[boot]\nsystemd=true\n' > $conf
      exit 0
    fi
    # File exists: append [boot] section if missing, add systemd=true under it.
    if ! grep -q '^\[boot\]' $conf; then
      printf '\n[boot]\nsystemd=true\n' >> $conf
    else
      # [boot] section exists; insert systemd=true right after it if not already there.
      sed -i '/^\[boot\]/a systemd=true' $conf
    fi
  "
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
  # head closes the pipe after $len bytes, which gives tr SIGPIPE (exit 141).
  # Under `set -o pipefail` that fails the pipeline and aborts install.sh
  # silently. Pre-read a bounded chunk so nothing receives SIGPIPE.
  local len="${1:-20}"
  local pool
  pool="$(LC_ALL=C tr -dc 'A-Za-z0-9' < <(head -c "$((len * 8))" /dev/urandom))"
  printf '%s' "${pool:0:len}"
}

# True iff something is already listening on the given TCP port on this host.
# Has to work on both Linux (no lsof guaranteed) and macOS (no ss). We try
# lsof first (present on macOS by default and most Linux distros), fall
# back to a /dev/tcp connect from bash. If neither works we return "free"
# so we don't false-fail the install — docker compose will surface a real
# bind error if it really is taken.
_pawcorder_port_in_use() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
    return 1
  fi
  if (echo > "/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Pick the first free port at $start..$start+50. If everything in that
# window is in use, fall back to $start so docker compose surfaces the
# original error rather than us silently ranging into someone else's app.
_pawcorder_pick_port() {
  local start="$1" port limit
  port="$start"
  limit="$((start + 50))"
  while (( port < limit )); do
    if ! _pawcorder_port_in_use "$port"; then
      printf '%s' "$port"
      return 0
    fi
    port=$((port + 1))
  done
  printf '%s' "$start"
}

# Probe the four host ports docker-compose maps and emit the picked
# values. If a port is busy, the next free port in a 50-port window is
# used instead. Logs a plain-language note for any port we had to move
# (real-user UX: someone with macOS AirPlay Receiver shouldn't have to
# know what a port conflict is).
PICKED_ADMIN_PORT=""
PICKED_FRIGATE_PORT=""
PICKED_RTSP_PORT=""
PICKED_WEBRTC_PORT=""
pick_host_ports() {
  PICKED_ADMIN_PORT="$(_pawcorder_pick_port 8080)"
  PICKED_FRIGATE_PORT="$(_pawcorder_pick_port 5000)"
  PICKED_RTSP_PORT="$(_pawcorder_pick_port 8554)"
  PICKED_WEBRTC_PORT="$(_pawcorder_pick_port 8555)"
  _pawcorder_log_port_swap "Admin panel" 8080 "$PICKED_ADMIN_PORT"
  _pawcorder_log_port_swap "Frigate UI"  5000 "$PICKED_FRIGATE_PORT"
  _pawcorder_log_port_swap "Camera RTSP" 8554 "$PICKED_RTSP_PORT"
  _pawcorder_log_port_swap "Live audio"  8555 "$PICKED_WEBRTC_PORT"
}

_pawcorder_log_port_swap() {
  local label="$1" want="$2" got="$3"
  [[ "$want" == "$got" ]] && return 0
  log_warn "$label port $want is in use on this machine — using $got instead."
}

# Keys we expect in .env. Used to backfill an .env generated by an older
# installer that pre-dates the host-port probe. Order matches the heredoc
# in ensure_env_file so appended lines stay grouped.
_PAWCORDER_ENV_PORT_KEYS=(ADMIN_HOST_PORT FRIGATE_HOST_PORT RTSP_HOST_PORT WEBRTC_HOST_PORT)

# Append any missing port keys to an existing .env. We don't rewrite the
# whole file (preserves the user's hand-edits + comments). pick_host_ports
# must have populated PICKED_* before this is called.
ensure_env_keys_present() {
  local env_path="$PAWCORDER_DIR/.env"
  [[ -f "$env_path" ]] || return 0
  local appended=0
  for key in "${_PAWCORDER_ENV_PORT_KEYS[@]}"; do
    if ! grep -qE "^[[:space:]]*${key}=" "$env_path"; then
      if (( appended == 0 )); then
        printf '\n# Host port mappings (added by installer; auto-picked to avoid\n# conflicts with whatever else was already listening on the box).\n' >> "$env_path"
        appended=1
      fi
      local val
      case "$key" in
        ADMIN_HOST_PORT)   val="$PICKED_ADMIN_PORT"   ;;
        FRIGATE_HOST_PORT) val="$PICKED_FRIGATE_PORT" ;;
        RTSP_HOST_PORT)    val="$PICKED_RTSP_PORT"    ;;
        WEBRTC_HOST_PORT)  val="$PICKED_WEBRTC_PORT"  ;;
      esac
      printf '%s="%s"\n' "$key" "$val" >> "$env_path"
    fi
  done
  if (( appended == 1 )); then
    log_ok "Added host-port keys to existing .env"
  fi
}

ensure_env_file() {
  pick_host_ports
  if [[ -f "$PAWCORDER_DIR/.env" ]]; then
    log_ok ".env already present, leaving as-is"
    ensure_env_keys_present
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
ADMIN_HOST_PORT="$PICKED_ADMIN_PORT"
FRIGATE_HOST_PORT="$PICKED_FRIGATE_PORT"
RTSP_HOST_PORT="$PICKED_RTSP_PORT"
WEBRTC_HOST_PORT="$PICKED_WEBRTC_PORT"
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

# How often the host-side Wi-Fi scanner refreshes its snapshot. Faster
# means fresher SoftAP detection during onboarding; slower saves CPU.
# 30s is the sweet spot (cameras take 30–60s to enter SoftAP mode after
# a reset, so the first scan after the user resets the camera lands
# within one cycle).
PAWCORDER_WIFI_SCAN_INTERVAL_DEFAULT=30

# Install the host-side Wi-Fi scanner under the platform's job manager.
# - macOS:   launchd LaunchAgent at ~/Library/LaunchAgents/
# - Linux:   systemd .service + .timer under /etc/systemd/system/
# Either path writes $PAWCORDER_DIR/.wifi_scan.json which the admin
# container reads through the bind-mounted /data volume. We never run
# the scanner *inside* the admin container — Docker on macOS can't see
# the host Wi-Fi card, and a containerised nmcli/iw can't reach the
# host's NetworkManager socket on Linux either.
ensure_wifi_scan_helper() {
  local kernel
  kernel="$(_pawcorder_uname_kernel)"
  case "$kernel" in
    Darwin) _ensure_wifi_scan_macos ;;
    Linux)  _ensure_wifi_scan_linux ;;
    *) log_warn "Wi-Fi scanner helper not installed (unsupported kernel: $kernel). SoftAP camera onboarding will be unavailable." ;;
  esac
}

# Render an __INTERVAL__/__PAWCORDER_DIR__ launchd plist template into
# the user's LaunchAgents dir, then bootout+bootstrap so the change is
# idempotent across re-runs of install.sh.
_pawcorder_install_launchd_plist() {
  local label="$1" template="$2" interval="$3"
  local agents_dir="$HOME/Library/LaunchAgents"
  local plist_path="$agents_dir/$label.plist"
  if [[ ! -f "$template" ]]; then
    log_warn "$label plist template missing at $template — skipping"
    return 1
  fi
  mkdir -p "$agents_dir" "$PAWCORDER_DIR/storage"
  python3 -c "
import sys
src = open(sys.argv[1]).read()
out = (src
       .replace('__PAWCORDER_DIR__', sys.argv[2])
       .replace('__INTERVAL__', sys.argv[3]))
open(sys.argv[4], 'w').write(out)
" "$template" "$PAWCORDER_DIR" "$interval" "$plist_path"
  chmod 0644 "$plist_path"
  local uid
  uid="$(id -u)"
  launchctl bootout "gui/$uid" "$plist_path" 2>/dev/null || true
  if launchctl bootstrap "gui/$uid" "$plist_path" 2>/dev/null; then
    return 0
  fi
  return 1
}

# Build the dedicated venv the BLE host helper uses. We don't touch
# system Python (PEP 668 blocks pip there on macOS Sonoma+). Idempotent
# — re-running install.sh just re-confirms the venv and bleak version.
_ensure_host_helpers_venv_macos() {
  local venv="$PAWCORDER_DIR/.host-helpers-venv"
  if [[ -x "$venv/bin/python" ]] && "$venv/bin/python" -c "import bleak" 2>/dev/null; then
    log_ok "Host helpers venv ready (bleak installed)"
    return 0
  fi
  log_info "Creating host helpers venv at $venv (bleak ~7 MB)…"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet bleak
  log_ok "Installed bleak in $venv"
  log_info "First BLE scan will prompt for Bluetooth permission — click Allow."
}

_ensure_wifi_scan_macos() {
  local interval="${PAWCORDER_WIFI_SCAN_INTERVAL:-$PAWCORDER_WIFI_SCAN_INTERVAL_DEFAULT}"
  if _pawcorder_install_launchd_plist \
      "com.pawcorder.wifi-scan" \
      "$PAWCORDER_DIR/scripts/com.pawcorder.wifi-scan.plist.template" \
      "$interval"; then
    log_ok "Installed Wi-Fi scanner LaunchAgent ($interval s interval)"
  else
    log_warn "Wi-Fi scanner LaunchAgent install failed; SoftAP onboarding will be limited."
  fi
  # Kick once now so the first .wifi_scan.json + .arp_scan.json exist
  # before the admin comes up. wifi-scan.sh derives PAWCORDER_DIR from
  # its own location so we don't need to forward it.
  bash "$PAWCORDER_DIR/scripts/wifi-scan.sh" >/dev/null 2>&1 || true

  # BLE on macOS needs the host venv + a second launchd plist because
  # Docker Desktop's Linux VM has no CoreBluetooth access. Failure here
  # leaves Wi-Fi/ARP working — we don't fail-fast.
  if _ensure_host_helpers_venv_macos; then
    if _pawcorder_install_launchd_plist \
        "com.pawcorder.ble-scan" \
        "$PAWCORDER_DIR/scripts/com.pawcorder.ble-scan.plist.template" \
        "$interval"; then
      log_ok "Installed BLE scanner LaunchAgent ($interval s interval)"
      bash "$PAWCORDER_DIR/scripts/ble-scan.sh" >/dev/null 2>&1 || true
    else
      log_warn "BLE scanner LaunchAgent install failed; BLE-paired cameras will be invisible."
    fi
  else
    log_warn "Host helpers venv creation failed; BLE onboarding unavailable."
  fi
}

_ensure_wifi_scan_linux() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log_warn "systemctl not found — skipping Wi-Fi scanner helper. SoftAP onboarding will be unavailable."
    return 0
  fi
  local interval="${PAWCORDER_WIFI_SCAN_INTERVAL:-$PAWCORDER_WIFI_SCAN_INTERVAL_DEFAULT}"
  local svc_template="$PAWCORDER_DIR/scripts/pawcorder-wifi-scan.service.template"
  local timer_template="$PAWCORDER_DIR/scripts/pawcorder-wifi-scan.timer.template"
  local svc_path="/etc/systemd/system/pawcorder-wifi-scan.service"
  local timer_path="/etc/systemd/system/pawcorder-wifi-scan.timer"
  if [[ ! -f "$svc_template" || ! -f "$timer_template" ]]; then
    log_warn "wifi-scan systemd templates missing — skipping helper install"
    return 0
  fi
  $SUDO bash -c "sed 's|__PAWCORDER_DIR__|$PAWCORDER_DIR|g' '$svc_template' > '$svc_path'"
  $SUDO bash -c "sed -e 's|__PAWCORDER_DIR__|$PAWCORDER_DIR|g' -e 's|__INTERVAL__|$interval|g' '$timer_template' > '$timer_path'"
  $SUDO chmod 0644 "$svc_path" "$timer_path"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now pawcorder-wifi-scan.timer 2>/dev/null || \
    log_warn "systemd enable failed; SoftAP onboarding may be limited."
  # First-run kick (same reason as macOS). wifi-scan.sh figures out
  # PAWCORDER_DIR from its own path so we don't need to forward it.
  $SUDO bash "$PAWCORDER_DIR/scripts/wifi-scan.sh" >/dev/null 2>&1 || true
  log_ok "Installed Wi-Fi scanner systemd timer ($interval s interval)"
}

print_summary() {
  local lan_ip admin_pw admin_port frigate_port
  lan_ip="$(_pawcorder_lan_ip)"
  source_env_value ADMIN_PASSWORD
  admin_pw="${GENERATED_ADMIN_PASSWORD:-${ENV_VALUE:-(see .env)}}"
  source_env_value ADMIN_HOST_PORT
  admin_port="${ENV_VALUE:-${PICKED_ADMIN_PORT:-8080}}"
  source_env_value FRIGATE_HOST_PORT
  frigate_port="${ENV_VALUE:-${PICKED_FRIGATE_PORT:-5000}}"

  cat <<EOF

${C_BOLD}${C_GREEN}pawcorder is up.${C_RESET}

  Admin panel:   http://$lan_ip:$admin_port
  Admin pwd:     $admin_pw

After completing the setup wizard:

  Frigate UI:    http://$lan_ip:$frigate_port

To make this reachable from outside your home network, install Tailscale:

  ./scripts/install-tailscale.sh

To mount your NAS at the storage path:

  ./scripts/mount-nas.sh

Logs:  docker compose logs -f
Stop:  docker compose down
EOF
}
