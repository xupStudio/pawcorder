# pawcorder

[English](README.md) · [繁體中文](README.zh-TW.md)

Self-hosted pet camera NVR. Your video stays on your network. No
subscription. Bring your own cloud (Google Drive / Dropbox / S3 /
WebDAV) so the only money you spend after the hardware is the storage
you already pay Google / Microsoft / Dropbox for.

[![CI](https://github.com/xupStudio/pawcorder/actions/workflows/ci.yml/badge.svg)](https://github.com/xupStudio/pawcorder/actions/workflows/ci.yml)

## What you get

### Recording + AI
- **Cameras** — auto-configures 6 brand keys (Reolink + Hikvision +
  Dahua + Amcrest + Axis + Foscam — Amcrest is a Dahua OEM so they
  share the same module) via their native HTTP APIs. UniFi Protect,
  Tapo, Imou, Wyze get inline step-by-step guidance for the one-time
  in-app setup. Everything else falls back to ONVIF Profile S
  auto-discovery. Reolink E-series remains the recommended new-buy.
  Add as many cameras as you have.
- **NVR + AI** — [Frigate](https://frigate.video/) (MIT) auto-picks the
  best detector for your hardware: OpenVINO on Intel iGPU, TensorRT on
  NVIDIA, Edge TPU on Coral, Hailo on Pi-AI-kit, or pure CPU on
  Mac / Windows / anything else.
- **Live view** — embedded HLS / WebRTC players on the dashboard and
  per-camera page; two-way audio (push-to-talk button) where the
  camera supports it.
- **Audio detection** — Frigate's audio model fires events on barks,
  meows, glass-break, smoke alarm, screams. Wired into the same
  notification path as visual detections.

### Pet recognition
- **Per-pet identity** — upload a few photos of each pet, MobileNetV3
  (576-dim ONNX embeddings) recognises them across cameras. Cosine
  threshold 0.78; tentative matches are surfaced separately.
- **Cross-camera timeline** — every pet's day-by-day movement across
  all cameras in one scrollable view.
- **Backfill** — re-run recognition over the last week's events when
  you add a new pet, with live progress bar.
- **Health alerts** — per-pet baselines, fires when activity is
  ≥ 2 σ below the rolling mean OR drops below a hard fraction.

### Browser admin
- **Multi-user with roles** — admin / family / kid. Last admin can't
  be deleted or demoted (otherwise no one could manage the system).
- **Dashboard** — live thumbnails, top moments today, recent events,
  recent logs viewer, container resource usage (CPU / RAM / disk).
- **Zone editor** — canvas-based polygon editor for detection zones
  AND privacy masks. Click to add points, drag to refine.
- **PTZ presets** — name + save the current pan/tilt/zoom; tap to
  jump back. ONVIF-driven, no Reolink-specific bits.
- **Privacy mode** — auto-pauses recording when your phone is on
  the home Wi-Fi (or by manual toggle / schedule).
- **Daily highlights** — automatic 30-second reel of the day's top
  moments via ffmpeg stream-copy (no re-encoding patent risk).
  14-day retention.
- **Time-lapse** — 1 frame/min per camera → 24 h compressed into a
  ~48 s mp4 every night. 30-day retention.
- **Activity heatmap** — per-camera 30-day movement density overlay,
  cached as a translucent PNG.
- **Energy mode** — schedule a low-FPS / motion-only window (e.g. 02–06)
  to cut iGPU power while you sleep.
- **Schema migrations** — boot-time, idempotent, per-file version
  stamps so upgrades don't strand existing data.
- **API keys + Web Push** — bearer-token auth alongside cookie sessions;
  PWA push notifications via VAPID with a Telegram fallback for iOS.

### Storage
- **NAS mount UI** — wire up NFS / SMB from the browser: test mount
  first, install to `/etc/fstab` (idempotent — re-runs replace
  cleanly), mount immediately. SMB password lands in a 0600 creds
  file, never in fstab.
- **Cloud backup** — `rclone` with 8 backends (Drive / Dropbox /
  OneDrive / B2 / S3 / Wasabi / R2 / WebDAV). Optional AES-256-GCM
  encryption with a passphrase you set.
- **Scheduled backups** — daily snapshot of `cameras.yml`, `pets.yml`,
  `users.yml`, etc. into your chosen remote.

### Notifications
- **Telegram** (with snapshot) or **LINE** (text) when a cat / dog
  is detected. No MQTT broker, no extra services.
- **Web Push (PWA)** — desktop / Android browsers via VAPID; iOS
  falls through to Telegram because Safari's push support is partial.
- **Frigate webhook** — sub-second event delivery, replaces the
  polling loop for notifications.

### Anywhere access
- **Mobile** — browser PWA with QR-code shortcut to add to home screen.
- **Tailscale** — one-line install script to expose the admin to
  your tailnet without port-forwarding.
- **Bilingual** — 繁體中文 / English everywhere; Japanese / Korean
  stubs scaffolded for translators to fill in.

### Operator quality-of-life
- **One-command install** — `curl | bash` (see below) or
  `git clone && ./install.sh`.
- **OTA updates** — `make update` pulls the latest images; the admin
  has an "update available" badge driven by GitHub releases.
- **Backup / restore** — encrypted tarball of the entire admin state
  via the System page or `make backup`.
- **Uninstall** — soft reset (wipe app data, keep recordings) or
  full removal, both honest about what they delete.
- **API + integration docs** — `/docs/api` page with curl, Home
  Assistant, iOS Shortcuts examples for every endpoint.

## Versus what you'd otherwise buy

| | Furbo Dog Nanny | Wyze Cam Plus | Apple HKSV | UniFi Protect | **pawcorder** |
| --- | :---: | :---: | :---: | :---: | :---: |
| Hardware (4 cams) | NT$24,000 | NT$3,600 | varies | NT$22,000 | **NT$11,200** |
| Monthly fee | NT$199 | NT$120 | NT$99 (iCloud+) | $0 | **$0** |
| Cloud storage | vendor | vendor | your iCloud | local | **your Drive / S3 / NAS** |
| Pet-specific AI | ✓ | ✗ | ✗ | ✗ | ✓ |
| Multi-camera | extra fee | yes | yes | yes | ✓ |
| Privacy | ✗ | ✗ | ✓ | ✓ | ✓ |
| Setup difficulty | easy | easy | medium | hard | **medium → easy with USB image** |

## Hardware

pawcorder runs on anything that can run Docker. `install.sh` probes
the host on first run and picks the best Frigate detector for your
platform — you don't have to choose:

| Host | Detector picked | Notes |
| --- | --- | --- |
| **Intel x86_64 with iGPU** (N100, NUC, J5005…) | OpenVINO | best perf/$ — recommended |
| **NVIDIA GPU on Linux** | TensorRT | reuses existing gaming PC / homelab |
| **Raspberry Pi 5 + Hailo-8L AI Kit** | Hailo | low-power ARM with AI accelerator |
| **Raspberry Pi 5 + Coral USB** | Edge TPU | cheaper Pi route |
| **AMD x86_64**, NAS (Synology / QNAP x86) | CPU | works, no hardware accel |
| **Mac (Apple Silicon or Intel)** | CPU | dev / testing — Docker Desktop runs Linux in a VM, no iGPU passthrough; OK for 1–2 cameras |
| **Windows + Docker Desktop** | CPU | same caveats as Mac |

You can override the auto-pick on the **/hardware** admin page anytime.

### Recommended starter kit (台灣價格)

For a clean-slate buy, we suggest an **Intel N100** mini PC because it
gives OpenVINO-grade AI detection at idle ~10 W for under NT$7,000 —
the best performance-per-dollar for 24/7 NVR duty. But pawcorder is
**not** N100-only — if you already have a Pi 5, Synology x86, AMD
homelab, or even a spare laptop, point `install.sh` at it and skip
buying new hardware.

| Item | Notes | NT$ |
| --- | --- | ---: |
| Host (one of):                    |                                                   |             |
| &nbsp;&nbsp;Intel N100 mini PC    | Beelink / GMKtec / MINISFORUM (8GB / 256GB)        | 5,500–6,500 |
| &nbsp;&nbsp;Raspberry Pi 5 + Hailo-8L | Compact ARM with AI accelerator                  | ~6,000      |
| &nbsp;&nbsp;Existing NAS / homelab | x86 Synology, TrueNAS, Proxmox VM, etc.            | 0           |
| Reolink E1 Outdoor PoE × N        | 4MP / 2K, PoE, full PTZ + zoom; works indoors too  | 2,500–3,200 each |
| TP-Link TL-SG1005P PoE switch     | 5-port, 4× PoE 802.3af                             | 1,500       |
| Cat6 patch cable per camera       | Length depends on placement                        | 100–200 each |
| Your existing NAS                 | TrueNAS / OMV / Synology / DIY                     | —           |

- **1 camera (N100 path):** ≈ NT$10,000 once-off, then **$0 / month**.
- **4 cameras (N100 path):** ≈ NT$19,000 once-off, then **$0 / month**.
- **Already have a Pi 5 / NAS / homelab?** Just the cameras + PoE
  switch: NT$3,500 / 11,500 — even cheaper.

(The same 4-camera setup with Furbo: ~NT$24,000 + NT$199/month × 4 ≈
NT$48,000 over 3 years.)

## Install in one command

On any freshly-installed Linux box (Ubuntu 24.04 / Debian 12 / Fedora /
Arch), macOS, or Windows + WSL2:

```sh
curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
```

That's it. The bootstrap script clones to `/opt/pawcorder` (or
`$HOME/pawcorder` on macOS), hands off to `install.sh`, which detects
your platform (OS + arch + accelerator), installs Docker (Linux:
`get.docker.com`; macOS: `brew install --cask docker` then waits for
Docker Desktop to start; WSL2: Docker Engine inside the distro),
generates secrets, picks the best Frigate detector for the host,
brings up the admin panel, and prints its URL + admin password.

Don't trust `curl | bash`? Read the source first:

```sh
curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh -o bootstrap.sh
less bootstrap.sh   # ← review before running
bash bootstrap.sh
```

Or do it the old-fashioned way:

```sh
git clone https://github.com/xupStudio/pawcorder.git
cd pawcorder
./install.sh
```

Custom install location (defaults to `/opt/pawcorder`):

```sh
PAWCORDER_DIR=$HOME/pawcorder curl -fsSL https://raw.githubusercontent.com/xupStudio/pawcorder/main/scripts/bootstrap.sh | bash
```

### One-step install with the pre-baked USB image

For non-technical buyers, build the bootable USB ISO once:

```sh
cd boot-image/
./build.sh
```

dd the resulting `output/pawcorder-ubuntu-24.04.iso` to a USB stick.
Plug into your target box (any x86_64 — N100, NUC, old laptop). ~10
minutes later the host announces itself on the LAN as `pawcorder.local`
and the admin panel is ready at `http://pawcorder.local:8080`. See
[boot-image/README.md](boot-image/README.md).

## Hardware setup walkthrough

### 1. Pick a host

Install **Ubuntu Server 24.04 LTS** (or use the pre-baked ISO above)
on whatever you're using as the pawcorder host — Intel mini PC, Pi 5,
NAS x86 box, etc. Set a DHCP reservation for the host's IP in your
router so it doesn't move around.

### 2. PoE switch + camera cabling

Plug the TP-Link TL-SG1005P into a wall outlet. Connect its uplink
port to your home router. Connect each Reolink E1 Outdoor PoE camera
to a PoE port with a Cat6 cable — the camera powers on automatically.

### 3. Reolink camera one-time setup (Reolink mobile app)

1. Add the camera in the Reolink app.
2. Set a strong **admin password** (write it down).
3. **Important:** in **Settings → Display → Encoding**, switch the main
   stream to **H.264** (not H.265).
4. Set a DHCP reservation in your router.

The pawcorder admin panel turns RTSP on automatically via the Reolink
HTTP API after you finish the setup wizard.

### 4. Finish in the browser

Open `http://<host-ip>:8080` (or `http://pawcorder.local:8080` if you
used the boot image), log in with the printed password, and walk
through the 5-step wizard:

1. **Cameras** — scan your subnet, add each camera. Connection type
   (Wi-Fi vs Wired) is auto-detected via the Reolink API.
2. **Storage** — point Frigate at your NAS mount path.
3. **Detection** — pick a sensitivity preset. Toggle which species to
   track (cat / dog / person).
4. **Admin password** — replace the random one.
5. **Finish.** Frigate restarts. Open the live view.

## Day-to-day operations

```sh
make ps           # what's running
make logs         # combined logs
make frigate-logs # only Frigate
make restart      # restart the stack
make update       # pull new images and recreate
make password     # print the admin password from .env
make test         # run tests + shellcheck
```

Same things are available in the admin panel: live status on the
Dashboard, restart Frigate from System, view recent logs in-browser.

## Architecture

```
+----------+ +----------+ +----------+ +----------+
|  cam 1   | |  cam 2   | |  cam 3   | |  cam 4   |   PoE-powered Reolink
+----+-----+ +----+-----+ +----+-----+ +----+-----+
     |            |            |            |
     +------+-----+------+-----+------+-----+
            |            |            |
            v            v            v
            +-------- PoE switch -----+
                         |
                         | LAN
                         v
                +-----------------+      HLS / WebRTC    +-------+
                |  Frigate (host) | -------------------> | Phone |
                |  (auto-picked   |                      | / Web |
                |   detector)     |                      +-------+
                +-----------------+
                  |              |
                  | NFS / SMB    | rclone
                  v              v
          +-------------+   +--------------+
          |    NAS      |   |  Your cloud  |
          | (full keep) |   |  (events    |
          +-------------+   |   only)      |
                            +--------------+

+-------------------+
| pawcorder admin   |  <- you, in a browser
| (FastAPI + UI)    |     manages cameras / detection / cloud /
+-------------------+     notifications / hardware, restarts Frigate
```

## Project layout

```
pawcorder/
├─ install.sh                # one-command host bootstrap
├─ docker-compose.yml        # admin + frigate (no hardware-specific bits)
├─ docker-compose.linux-igpu.yml    # adds /dev/dri device for Intel
├─ docker-compose.linux-nvidia.yml  # adds nvidia runtime
├─ Makefile                  # day-to-day ops + tests
├─ admin/                    # FastAPI admin panel
│  ├─ Dockerfile             # bundles rclone + ffmpeg + nmap + Python
│  ├─ requirements.txt
│  └─ app/
│     ├─ main.py             # FastAPI routes
│     ├─ auth.py             # session-cookie auth
│     ├─ config_store.py     # .env IO + Jinja2 render of frigate config
│     ├─ cameras_store.py    # cameras.yml CRUD
│     ├─ camera_api.py       # Reolink HTTP API + ffprobe
│     ├─ network_scan.py     # nmap-based RTSP discovery
│     ├─ docker_ops.py       # restart Frigate via socket
│     ├─ platform_detect.py  # OS/arch/accelerator detection
│     ├─ telegram.py         # Telegram bot poller
│     ├─ line.py             # LINE Messaging API client
│     ├─ cloud.py            # rclone wrapper + cloud uploader
│     ├─ i18n.py             # zh-TW + en translation table
│     ├─ static/             # PWA manifest + service worker + icon
│     └─ templates/          # Jinja2 + Tailwind + Alpine UI
├─ config/
│  ├─ frigate.template.yml   # Jinja2 source for Frigate config
│  ├─ cameras.yml            # gitignored — managed by admin
│  ├─ rclone/rclone.conf     # gitignored — managed by admin
│  └─ config.yml             # gitignored — rendered for Frigate
├─ boot-image/               # Packer + cloud-init for USB ISO
├─ scripts/
│  ├─ lib.sh                 # shared bash helpers + platform detection
│  ├─ install-tailscale.sh
│  └─ mount-nas.sh
└─ .github/workflows/ci.yml  # pytest, shellcheck, packer validate
```

## Testing

```sh
make test
```

~525 tests cover camera CRUD + validation, config round-trip with
escapes, Frigate template rendering across detector / species combos,
Reolink link-classifier, network-scan input validation, i18n key
coverage (every key has both `en` and `zh-TW`), session-cookie + bearer
auth, RBAC, multi-user lifecycle, NAS mount fstab idempotency,
recognition-backfill single-flight, time-lapse retention, schema
migrations, webhook dedup, and full FastAPI route smoke tests via
`TestClient` with all external deps (Docker, Reolink HTTP, ffprobe,
Telegram, LINE, rclone, nmap, ONNX) stubbed. CI also lints bash with
shellcheck and validates the Packer template + docker compose syntax.

## License

[MIT](LICENSE).

Built on top of these excellent projects (all permissive licenses):
- [Frigate](https://frigate.video/) (MIT) — NVR + AI engine
- [go2rtc](https://github.com/AlexxIT/go2rtc) (MIT) — RTSP restream
- [FastAPI](https://fastapi.tiangolo.com/) (MIT) — admin panel server
- [rclone](https://rclone.org/) (MIT) — cloud uploader
- [Tailwind CSS](https://tailwindcss.com/) (MIT) + [Alpine.js](https://alpinejs.dev/) (MIT) — UI
- [HashiCorp Packer](https://www.packer.io/) (MPL 2.0) — boot image build
- [Inter](https://rsms.me/inter/) (OFL) — UI font

## Contributing

Bug reports and PRs welcome. Please run `make test` before submitting.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.
