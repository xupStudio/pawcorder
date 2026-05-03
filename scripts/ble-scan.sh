#!/usr/bin/env bash
# Pawcorder host-side BLE scanner.
#
# Why this script exists:
# bleak inside the admin Docker container fails with `[Errno 2]` on
# macOS because Docker Desktop runs Linux in a VM with no access to
# CoreBluetooth, and even on Linux the container's net namespace
# doesn't reach the host's BLE adapter without privileged setup.
# So we run bleak on the *host* every 30s — installed in a dedicated
# venv at $PAWCORDER_DIR/.host-helpers-venv to avoid touching system
# Python — and dump the advertisements to .ble_scan.json. The admin
# reads that file from the bind-mounted /data volume, the same way it
# reads .wifi_scan.json and .arp_scan.json.
#
# Output schema (atomic write — the reader sees old or new, never half):
#
#   {
#     "schema": 1,
#     "generated_at": <unix-seconds>,
#     "platform": "macos" | "linux",
#     "tool": "bleak" | "none",
#     "devices": [ { address, rssi, name, service_uuids, manufacturer_ids }, ... ],
#     "error": null | "<short reason>"
#   }

set -euo pipefail

PAWCORDER_DIR="${PAWCORDER_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT_PATH="$PAWCORDER_DIR/.ble_scan.json"
TMP_PATH="$OUT_PATH.tmp.$$"
VENV_PY="$PAWCORDER_DIR/.host-helpers-venv/bin/python"

case "$(uname -s)" in
  Darwin) _PLATFORM=macos ;;
  Linux)  _PLATFORM=linux ;;
  *)
    cat > "$TMP_PATH" <<EOF
{"schema":1,"generated_at":$(date +%s),"platform":"other","tool":"none","devices":[],"error":"unsupported_platform"}
EOF
    mv "$TMP_PATH" "$OUT_PATH"
    exit 0
    ;;
esac

emit_empty() {
  local reason="$1"
  cat > "$TMP_PATH" <<EOF
{"schema":1,"generated_at":$(date +%s),"platform":"$_PLATFORM","tool":"none","devices":[],"error":"$reason"}
EOF
  mv "$TMP_PATH" "$OUT_PATH"
}

if [[ ! -x "$VENV_PY" ]]; then
  emit_empty "no_venv"
  exit 0
fi

# Run a 6-second BLE scan and emit normalised JSON. We accept the
# argv[1] output path so the Python child writes directly to the temp
# file (no stdin parsing complications, and large advert payloads stay
# off the shell pipeline).
"$VENV_PY" - "$TMP_PATH" "$_PLATFORM" <<'PYEOF' || { emit_empty "bleak_failed"; exit 0; }
import asyncio, json, sys, time
from pathlib import Path

OUT, PLATFORM = sys.argv[1], sys.argv[2]

try:
    from bleak import BleakScanner
except ImportError:
    Path(OUT).write_text(json.dumps({
        "schema": 1, "generated_at": int(time.time()),
        "platform": PLATFORM, "tool": "none",
        "devices": [], "error": "no_bleak",
    }))
    sys.exit(0)


async def main() -> None:
    found: dict[str, dict] = {}

    def on_advert(device, advert) -> None:
        addr = device.address
        rssi = getattr(advert, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", 0)
        # `(advert.local_name or device.name)` — bleak >= 0.21 stripped
        # `device.name` from the discovery callback on some platforms.
        name = (getattr(advert, "local_name", None) or device.name or "")
        services = list(getattr(advert, "service_uuids", None) or [])
        manuf_ids = list((getattr(advert, "manufacturer_data", None) or {}).keys())
        prev = found.get(addr)
        if prev and prev["rssi"] >= rssi:
            return
        found[addr] = {
            "address": addr,
            "rssi": int(rssi or 0),
            "name": name,
            "service_uuids": services,
            "manufacturer_ids": [int(m) for m in manuf_ids],
        }

    scanner = BleakScanner(detection_callback=on_advert)
    try:
        await scanner.start()
        await asyncio.sleep(6.0)
        await scanner.stop()
    except Exception as exc:  # noqa: BLE001
        Path(OUT).write_text(json.dumps({
            "schema": 1, "generated_at": int(time.time()),
            "platform": PLATFORM, "tool": "bleak",
            "devices": list(found.values()),
            "error": f"scan_error: {exc.__class__.__name__}",
        }))
        return

    Path(OUT).write_text(json.dumps({
        "schema": 1, "generated_at": int(time.time()),
        "platform": PLATFORM, "tool": "bleak",
        "devices": sorted(found.values(), key=lambda d: -d["rssi"]),
        "error": None if found else "no_devices",
    }))

asyncio.run(main())
PYEOF

mv "$TMP_PATH" "$OUT_PATH"
chmod 0644 "$OUT_PATH" 2>/dev/null || true
