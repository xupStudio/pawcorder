#!/usr/bin/env bash
# Pawcorder host-side scanner.
#
# Why this script exists, in one paragraph:
# The admin runs in a Docker container, which can't see the host's Wi-Fi
# interface or ARP cache — on macOS Docker Desktop runs Linux in a VM,
# and on Linux the bridge network has no wlan device and a separate net
# namespace. Wireless onboarding needs both: SoftAP discovery wants the
# nearby-SSID list, and the post-pairing arrival watcher wants the
# host's ARP table to spot a brand-new camera the moment it joins the
# LAN. We solve both by running this script on the *host* every 30s
# (launchd on Mac, systemd timer on Linux). It writes two atomic JSON
# snapshots the admin reads from a shared volume:
#
#   $PAWCORDER_DIR/.wifi_scan.json   ← visible SSIDs
#   $PAWCORDER_DIR/.arp_scan.json    ← MAC → IP map of the LAN
#
# Inside the container both files are at /data/.{wifi,arp}_scan.json.
#
# Output schemas (always written atomically — admin sees either the
# full previous snapshot or the full new one, never a half-written file):
#
#   wifi: { "schema":1, "generated_at":<unix>, "platform":"...",
#           "tool":"system_profiler"|"nmcli"|"iw"|"none",
#           "networks":[ {ssid,bssid,signal_dbm,channel}, ... ],
#           "error":null|"<reason>" }
#   arp:  { "schema":1, "generated_at":<unix>, "platform":"...",
#           "tool":"arp"|"ip-neigh"|"none",
#           "neighbors":[ {mac,ip}, ... ],
#           "error":null|"<reason>" }
#
# When the host has no Wi-Fi interface at all (wired-only Linux server),
# we still emit a valid wifi file with networks=[] and
# error="no_wifi_iface" so the admin can show the user a meaningful
# explanation rather than a generic "couldn't scan".

set -euo pipefail

PAWCORDER_DIR="${PAWCORDER_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT_PATH="$PAWCORDER_DIR/.wifi_scan.json"
TMP_PATH="$OUT_PATH.tmp.$$"
ARP_OUT_PATH="$PAWCORDER_DIR/.arp_scan.json"
ARP_TMP_PATH="$ARP_OUT_PATH.tmp.$$"

now() { date +%s; }

emit_empty() {
  local reason="$1" tool="$2"
  cat > "$TMP_PATH" <<EOF
{"schema":1,"generated_at":$(now),"platform":"$_PLATFORM","tool":"$tool","networks":[],"error":"$reason"}
EOF
  mv "$TMP_PATH" "$OUT_PATH"
}

emit_arp_empty() {
  local reason="$1" tool="$2"
  cat > "$ARP_TMP_PATH" <<EOF
{"schema":1,"generated_at":$(now),"platform":"$_PLATFORM","tool":"$tool","neighbors":[],"error":"$reason"}
EOF
  mv "$ARP_TMP_PATH" "$ARP_OUT_PATH"
}

# ---- platform-specific scanners ---------------------------------------

_PLATFORM=""
case "$(uname -s)" in
  Darwin) _PLATFORM="macos" ;;
  Linux)  _PLATFORM="linux" ;;
  *)
    _PLATFORM="other"
    emit_empty "unsupported_platform" "none"
    exit 0
    ;;
esac

# Run a Python one-liner that consumes platform-native output and emits
# our normalised JSON. We use Python rather than jq because (a) Python is
# always available on macOS via the Xcode CLT shim and on Linux via every
# distro's base packages, and (b) parsing system_profiler's nested JSON
# in jq is verbose and brittle.
_PY=python3
if ! command -v "$_PY" >/dev/null 2>&1; then
  if [[ -x /usr/bin/python3 ]]; then _PY=/usr/bin/python3
  elif [[ -x /opt/homebrew/bin/python3 ]]; then _PY=/opt/homebrew/bin/python3
  elif [[ -x /usr/local/bin/python3 ]]; then _PY=/usr/local/bin/python3
  else
    emit_empty "no_python" "none"
    exit 0
  fi
fi

_PY_MACOS_PARSER='
import json, sys, re, time
raw = sys.stdin.read()
data = json.loads(raw) if raw else {}
ifs = (data.get("SPAirPortDataType") or [{}])[0].get("spairport_airport_interfaces") or []
seen = []
def parse_chan(s):
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else 0
def parse_signal(s):
    m = re.match(r"\s*(-?\d+)", s or "")
    return int(m.group(1)) if m else 0
for iface in ifs:
    for net in iface.get("spairport_airport_other_local_wireless_networks") or []:
        seen.append({
            "ssid":   net.get("_name", ""),
            "bssid":  "",
            "signal_dbm": parse_signal(net.get("spairport_signal_noise", "")),
            "channel":    parse_chan(net.get("spairport_network_channel", "")),
        })
    cur = iface.get("spairport_current_network_information") or {}
    if cur.get("_name"):
        seen.append({
            "ssid":   cur["_name"], "bssid": "",
            "signal_dbm": parse_signal(cur.get("spairport_signal_noise", "")),
            "channel":    parse_chan(cur.get("spairport_network_channel", "")),
        })
json.dump({
    "schema": 1, "generated_at": int(time.time()), "platform": "macos",
    "tool": "system_profiler", "networks": seen,
    "error": None if seen else "no_networks_seen",
}, sys.stdout)
'

scan_macos() {
  # system_profiler is the only Sequoia-compatible scanner — `airport -s`
  # is gutted (exits 0 with no output) since macOS 14.4 and the
  # underlying CoreWLAN APIs now require a sandboxed app.
  local raw
  if ! raw="$(system_profiler SPAirPortDataType -json 2>/dev/null)"; then
    emit_empty "system_profiler_failed" "system_profiler"
    return 0
  fi
  # `python3 -c CODE` keeps stdin clear for the pipe — the heredoc form
  # `python3 - <<EOF` would consume stdin itself and starve the parser.
  printf '%s' "$raw" | "$_PY" -c "$_PY_MACOS_PARSER" > "$TMP_PATH"
  mv "$TMP_PATH" "$OUT_PATH"
}

_PY_NMCLI_PARSER='
import json, sys, time
seen = []
for line in sys.stdin.read().splitlines():
    # nmcli escapes ":" inside fields as "\:"; restore after splitting.
    s = line.replace(r"\:", "\x00")
    parts = s.split(":")
    if len(parts) < 4:
        continue
    chan = parts[-1]; signal = parts[-2]
    bssid = ":".join(parts[-8:-2])
    ssid = ":".join(parts[:-8]).replace("\x00", ":")
    if not ssid:
        continue
    try: sig = int(signal) if signal else 0
    except ValueError: sig = 0
    dbm = -30 - int((100 - sig) * 0.7)
    try: ch = int(chan) if chan else 0
    except ValueError: ch = 0
    seen.append({"ssid": ssid, "bssid": bssid, "signal_dbm": dbm, "channel": ch})
json.dump({
    "schema": 1, "generated_at": int(time.time()), "platform": "linux",
    "tool": "nmcli", "networks": seen,
    "error": None if seen else "no_networks_seen",
}, sys.stdout)
'

scan_linux_nmcli() {
  local raw
  if ! raw="$(nmcli -t -f SSID,BSSID,SIGNAL,CHAN device wifi list --rescan yes 2>/dev/null)"; then
    return 1
  fi
  printf '%s' "$raw" | "$_PY" -c "$_PY_NMCLI_PARSER" > "$TMP_PATH"
  mv "$TMP_PATH" "$OUT_PATH"
  return 0
}

_PY_IW_PARSER='
import json, sys, re, time
seen = []
cur = None
for line in sys.stdin.read().splitlines():
    m = re.match(r"^BSS\s+([0-9a-f:]{17})", line, re.I)
    if m:
        if cur and cur["ssid"]:
            seen.append(cur)
        cur = {"ssid": "", "bssid": m.group(1), "signal_dbm": 0, "channel": 0}
        continue
    if cur is None:
        continue
    m = re.match(r"^\s+SSID:\s*(.*)$", line)
    if m:
        cur["ssid"] = m.group(1); continue
    m = re.match(r"^\s+signal:\s*(-?\d+\.?\d*)\s*dBm", line)
    if m:
        try: cur["signal_dbm"] = int(float(m.group(1)))
        except ValueError: pass
        continue
    m = re.match(r"^\s+DS Parameter set: channel\s+(\d+)", line)
    if m:
        try: cur["channel"] = int(m.group(1))
        except ValueError: pass
if cur and cur["ssid"]:
    seen.append(cur)
json.dump({
    "schema": 1, "generated_at": int(time.time()), "platform": "linux",
    "tool": "iw", "networks": seen,
    "error": None if seen else "no_networks_seen",
}, sys.stdout)
'

scan_linux_iw() {
  # If there is no wireless interface we are on a wired server — emit
  # empty with the explicit reason rather than letting admin think we
  # crashed.
  local iface
  iface="$(iw dev 2>/dev/null | awk '/Interface/ {print $2; exit}')"
  if [[ -z "$iface" ]]; then
    emit_empty "no_wifi_iface" "iw"
    return 0
  fi
  local raw
  if ! raw="$(iw dev "$iface" scan 2>/dev/null)"; then
    emit_empty "iw_scan_failed" "iw"
    return 0
  fi
  printf '%s' "$raw" | "$_PY" -c "$_PY_IW_PARSER" > "$TMP_PATH"
  mv "$TMP_PATH" "$OUT_PATH"
}

# ---- ARP table scanner -------------------------------------------------
#
# We snapshot the host's ARP cache so the admin's arrival watcher can see
# a freshly-paired camera join the LAN. macOS uses BSD `arp -an`; Linux
# uses `ip neigh` if available, falling back to /proc/net/arp.

_PY_ARP_PARSER='
import json, re, sys, time
src = sys.argv[1]
text = sys.stdin.read()
out = []
seen = set()
def add(mac, ip):
    mac = mac.lower().strip()
    ip = ip.strip()
    if not mac or mac == "00:00:00:00:00:00" or mac in ("(incomplete)", "incomplete"):
        return
    if not re.match(r"^[0-9a-f:]{11,17}$", mac):
        return
    # macOS `arp -an` may emit single-digit hex octets (e.g. "0:1c:2:..."):
    # normalise to canonical 17-char form.
    parts = mac.split(":")
    if any(len(p) > 2 for p in parts):
        return
    mac = ":".join(p.zfill(2) for p in parts)
    if not ip or len(parts) != 6:
        return
    if mac in seen:
        return
    seen.add(mac)
    out.append({"mac": mac, "ip": ip})
if src == "macos":
    # macOS: "? (192.168.1.1) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]"
    for m in re.finditer(r"\(([\d.]+)\)\s+at\s+([0-9a-fA-F:]{11,17})", text):
        add(m.group(2), m.group(1))
elif src == "ip-neigh":
    # Linux: "192.168.1.1 dev wlan0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
    for line in text.splitlines():
        m = re.match(r"^([\d.]+)\s+.*lladdr\s+([0-9a-fA-F:]{11,17})", line)
        if m:
            add(m.group(2), m.group(1))
elif src == "proc":
    # /proc/net/arp: "IP HW_TYPE FLAGS HW_ADDRESS MASK DEVICE"
    lines = text.splitlines()[1:]
    for line in lines:
        parts = line.split()
        if len(parts) >= 4:
            add(parts[3], parts[0])
json.dump({
    "schema": 1, "generated_at": int(time.time()),
    "platform": sys.argv[2], "tool": sys.argv[3],
    "neighbors": out,
    "error": None if out else "no_neighbors",
}, sys.stdout)
'

arp_scan_macos() {
  local raw
  if ! raw="$(arp -an 2>/dev/null)"; then
    emit_arp_empty "arp_failed" "arp"
    return 0
  fi
  printf '%s' "$raw" | "$_PY" -c "$_PY_ARP_PARSER" macos macos arp > "$ARP_TMP_PATH"
  mv "$ARP_TMP_PATH" "$ARP_OUT_PATH"
}

arp_scan_linux() {
  local raw tool=""
  if command -v ip >/dev/null 2>&1; then
    if raw="$(ip neigh 2>/dev/null)"; then tool="ip-neigh"; fi
  fi
  if [[ -z "$tool" && -r /proc/net/arp ]]; then
    raw="$(cat /proc/net/arp 2>/dev/null || true)"
    tool="proc"
  fi
  if [[ -z "$tool" ]]; then
    emit_arp_empty "no_arp_tool" "none"
    return 0
  fi
  printf '%s' "$raw" | "$_PY" -c "$_PY_ARP_PARSER" "$tool" linux "$tool" > "$ARP_TMP_PATH"
  mv "$ARP_TMP_PATH" "$ARP_OUT_PATH"
}

# ---- dispatch ---------------------------------------------------------

case "$_PLATFORM" in
  macos)
    scan_macos
    arp_scan_macos
    ;;
  linux)
    if command -v nmcli >/dev/null 2>&1 && scan_linux_nmcli; then
      :
    elif command -v iw >/dev/null 2>&1; then
      scan_linux_iw
    else
      emit_empty "no_scan_tool" "none"
    fi
    arp_scan_linux
    ;;
esac

# Tighten perms — the files may contain SSIDs / MACs the user considers
# private.
chmod 0644 "$OUT_PATH" "$ARP_OUT_PATH" 2>/dev/null || true
