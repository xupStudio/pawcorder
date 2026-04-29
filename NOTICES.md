# Open-source acknowledgements

pawcorder builds on a lot of excellent open-source work. Every
component below is permissively licensed (MIT / BSD / Apache 2.0 /
MPL 2.0 / OFL) or used as a separate binary via subprocess (LGPL).
Nothing in this list infects pawcorder's own MIT licensing.

If you redistribute pawcorder commercially, the obligations are:

- **Keep the LICENSE files** of any dependencies you bundle (they're
  in their respective Docker layers).
- **For ffmpeg specifically**: it's used as a separate binary called
  via `subprocess`. We never link to libavcodec / libavformat from
  Python. Distributing pawcorder + ffmpeg in the same container is
  "mere aggregation" under LGPL/GPL — your own application code is
  not affected.
- **No `--enable-nonfree` ffmpeg builds** are ever shipped.
- **Patent-encumbered codecs** (H.264 decoding, AAC) are passed
  through to the underlying chip / OS codec; pawcorder itself does
  no encoding. The single-camera highlight reel feature uses
  `ffmpeg -c copy` (stream copy) — no decode, no encode, no patent
  exposure beyond what your camera vendor already pays for.

## Runtime dependencies

| Component | Version | License | Used for | Upstream |
| --- | --- | --- | --- | --- |
| Frigate | stable | MIT | NVR + on-device AI detection | https://github.com/blakeblackshear/frigate |
| go2rtc | bundled with Frigate | MIT | RTSP multiplexing, WebRTC, two-way audio | https://github.com/AlexxIT/go2rtc |
| ffmpeg | system package (LGPL build) | LGPL 2.1+ | Snapshot fetching, highlight reel stream-copy | https://ffmpeg.org/ |
| rclone | 1.68.2 | MIT | Cloud backup multi-backend uploader | https://rclone.org/ |
| onnxruntime | 1.19.2 | MIT | Pet recognition embedding inference | https://onnxruntime.ai/ |
| MobileNetV3-Small (ImageNet weights) | 100.lamb_in1k | Apache 2.0 | Embedding feature extractor | https://huggingface.co/timm/mobilenetv3_small_100.lamb_in1k |
| numpy | 2.1.2 | BSD-3-Clause | Embedding math | https://numpy.org/ |
| Pillow | 10.4.0 | MIT-CMU | Image preprocessing | https://python-pillow.org/ |
| FastAPI | 0.115.0 | MIT | Admin web framework | https://fastapi.tiangolo.com/ |
| uvicorn | 0.32.0 | BSD-3-Clause | ASGI server | https://www.uvicorn.org/ |
| Jinja2 | 3.1.4 | BSD-3-Clause | Template rendering | https://jinja.palletsprojects.com/ |
| python-multipart | 0.0.12 | Apache 2.0 | Multipart form parsing | https://github.com/Kludex/python-multipart |
| itsdangerous | 2.2.0 | BSD-3-Clause | Session cookie signing | https://itsdangerous.palletsprojects.com/ |
| httpx | 0.27.2 | BSD-3-Clause | Async HTTP client | https://www.python-httpx.org/ |
| docker (sdk) | 7.1.0 | Apache 2.0 | Talk to host Docker daemon | https://github.com/docker/docker-py |
| pyyaml | 6.0.2 | MIT | cameras.yml + pets.yml + frigate config | https://pyyaml.org/ |
| qrcode (python lib) | 7.4.2 | BSD-3-Clause | Mobile QR codes | https://github.com/lincolnloop/python-qrcode |
| distro | 1.9.0 | Apache 2.0 | OS detection | https://github.com/python-distro/distro |
| py-cpuinfo | 9.0.0 | MIT | CPU detection for OpenVINO/TensorRT auto-pick | https://github.com/workhorsy/py-cpuinfo |
| Watchtower | 1.7.1 | Apache 2.0 | Container auto-update | https://containrrr.dev/watchtower/ |
| Tailscale (optional) | upstream | BSD-3-Clause | Remote access mesh VPN | https://tailscale.com/ |
| Avahi (optional, system) | upstream | LGPL 2.1+ | mDNS for `pawcorder.local` | https://www.avahi.org/ |
| Tailwind CSS | 3.x via CDN | MIT | Admin UI styling | https://tailwindcss.com/ |
| Alpine.js | 3.x via CDN | MIT | Admin UI reactivity | https://alpinejs.dev/ |
| Inter font | OFL | OFL 1.1 | Admin UI typography | https://rsms.me/inter/ |
| HashiCorp Packer (build-time only) | latest | MPL 2.0 | Boot ISO assembly | https://www.packer.io/ |
| Ubuntu Server (boot image) | 24.04 LTS | dual GPL/Apache mix | OS for the prebuilt USB image | https://ubuntu.com/ |
| cloud-init (boot image) | upstream | dual GPL/Apache | First-boot automation | https://cloudinit.readthedocs.io/ |

## Test-only dependencies

These don't ship to production and don't affect distribution:

| Component | License |
| --- | --- |
| pytest, pytest-asyncio, pytest-cov | MIT |
| coverage | Apache 2.0 |

## Compliance summary for commercial use

- ✅ **You can charge for pawcorder** — MIT lets you sell modified or unmodified copies.
- ✅ **You can keep your own additions proprietary** — MIT doesn't require source release.
- ✅ **You can ship as a hardware bundle** — none of the deps require source distribution beyond their own LICENSE files in the bundle.
- ⚠ **You must include LICENSE / NOTICE files** for any redistributed binaries (Docker images already include these in their respective layers).
- ⚠ **If you fork ffmpeg and add patent-encumbered codecs** (libfdk-aac, etc.), you take on those obligations. Stick with the system package (Debian / Ubuntu's LGPL build) and you're fine.
- ❌ **Don't enable ffmpeg's `--enable-nonfree` flag** in any custom build. Output binary becomes non-redistributable.
