"""Detect host platform + accelerators so we can pick a Frigate detector.

We use:
  - stdlib `platform` (OS, arch)
  - `distro` (Linux distro name when applicable, Apache 2.0)
  - `py-cpuinfo` (CPU vendor / brand cross-platform, MIT)
  - simple file/path checks for accelerators (no extra deps)

The output is purely informational + used by `recommended_detector()`
to pick a sensible default for Frigate. The user can always override
through the admin panel.
"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict, dataclass
from typing import Iterable

import distro

try:
    import cpuinfo  # py-cpuinfo
except Exception:  # noqa: BLE001 - optional, fall back gracefully
    cpuinfo = None  # type: ignore[assignment]


# ---- data classes ------------------------------------------------------

@dataclass
class Accelerator:
    kind: str          # 'intel-igpu' | 'nvidia-gpu' | 'coral-usb' | 'hailo' | 'apple-silicon'
    detail: str        # human-readable ("Intel iGPU at /dev/dri/renderD128")


@dataclass
class PlatformInfo:
    os: str            # 'Linux' | 'Darwin' | 'Windows'
    distro_id: str     # 'ubuntu' / 'debian' / '' (empty on non-Linux)
    distro_version: str
    arch: str          # 'x86_64' | 'arm64' | 'aarch64'
    cpu_vendor: str    # 'GenuineIntel' | 'AuthenticAMD' | 'Apple'
    cpu_brand: str
    accelerators: list[Accelerator]
    docker_available: bool
    is_dev_host: bool  # macOS / Windows are typically dev hosts, not 24/7 NVR

    def to_dict(self) -> dict:
        d = asdict(self)
        d["accelerators"] = [asdict(a) for a in self.accelerators]
        return d


# ---- detection helpers -------------------------------------------------

_VENDOR_NORMAL = {
    "GenuineIntel": "Intel",
    "AuthenticAMD": "AMD",
    "Apple": "Apple",
    "ARM": "ARM",
}


def _cpu_vendor_and_brand() -> tuple[str, str]:
    """Return (raw vendor id, human-readable brand)."""
    if cpuinfo is None:
        return ("unknown", platform.processor() or "unknown")
    try:
        info = cpuinfo.get_cpu_info()
    except Exception:  # noqa: BLE001
        return ("unknown", platform.processor() or "unknown")
    vendor = info.get("vendor_id_raw") or info.get("vendor_id") or "unknown"
    brand = info.get("brand_raw") or info.get("brand") or platform.processor() or "unknown"
    return vendor, brand


def _detect_intel_igpu() -> Accelerator | None:
    # The Linux kernel exposes Intel iGPU as /dev/dri/renderD128 (or 129+
    # for additional devices). Existence is a strong-enough signal for
    # OpenVINO to be useful.
    for n in (128, 129, 130):
        path = f"/dev/dri/renderD{n}"
        if os.path.exists(path):
            return Accelerator(kind="intel-igpu", detail=f"Intel iGPU at {path}")
    return None


def _detect_nvidia_gpu() -> Accelerator | None:
    if shutil.which("nvidia-smi"):
        return Accelerator(kind="nvidia-gpu", detail="NVIDIA GPU (nvidia-smi present)")
    return None


def _detect_coral_usb() -> Accelerator | None:
    # Google's Coral USB shows up as Bus 001 Device NNN: ID 1a6e:089a (Global Unichip)
    # or 18d1:9302 (Google Inc.). We don't read libusb to keep deps light;
    # the user can manually override in the admin panel if our heuristic misses.
    if shutil.which("lsusb"):
        try:
            import subprocess
            out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=2).stdout
            if "1a6e:089a" in out or "18d1:9302" in out:
                return Accelerator(kind="coral-usb", detail="Google Coral USB Accelerator")
        except Exception:  # noqa: BLE001
            pass
    return None


def _detect_hailo() -> Accelerator | None:
    # Hailo-8 / Hailo-8L PCIe shows up as /dev/hailo0 once the driver loads.
    for n in range(4):
        if os.path.exists(f"/dev/hailo{n}"):
            return Accelerator(kind="hailo", detail=f"Hailo accelerator at /dev/hailo{n}")
    return None


def _detect_apple_silicon(arch: str, cpu_vendor: str) -> Accelerator | None:
    if platform.system() == "Darwin" and (arch == "arm64" or "Apple" in cpu_vendor):
        return Accelerator(
            kind="apple-silicon",
            detail="Apple Silicon (no Frigate detector currently uses ANE; CPU only)",
        )
    return None


def _detect_accelerators(arch: str, cpu_vendor: str) -> list[Accelerator]:
    out: list[Accelerator] = []
    for fn in (_detect_intel_igpu, _detect_nvidia_gpu, _detect_coral_usb, _detect_hailo):
        a = fn()
        if a:
            out.append(a)
    apple = _detect_apple_silicon(arch, cpu_vendor)
    if apple:
        out.append(apple)
    return out


def detect() -> PlatformInfo:
    os_name = platform.system()
    arch = platform.machine() or "unknown"
    if arch == "AMD64":
        arch = "x86_64"  # Windows reports AMD64 for Intel/AMD x86_64.
    cpu_vendor, cpu_brand = _cpu_vendor_and_brand()

    distro_id = ""
    distro_version = ""
    if os_name == "Linux":
        distro_id = distro.id() or ""
        distro_version = distro.version() or ""

    accels = _detect_accelerators(arch, cpu_vendor)
    is_dev = os_name in ("Darwin", "Windows")

    return PlatformInfo(
        os=os_name,
        distro_id=distro_id,
        distro_version=distro_version,
        arch=arch,
        cpu_vendor=cpu_vendor,
        cpu_brand=cpu_brand,
        accelerators=accels,
        docker_available=bool(shutil.which("docker")),
        is_dev_host=is_dev,
    )


# ---- detector recommendation -------------------------------------------

# Maps to a Frigate detector type. Keep these in sync with
# config/frigate.template.yml's {% if detector_type == ... %} branches.
DETECTOR_OPENVINO = "openvino"
DETECTOR_CPU = "cpu"
DETECTOR_TENSORRT = "tensorrt"
DETECTOR_EDGETPU = "edgetpu"
DETECTOR_HAILO = "hailo8l"

VALID_DETECTORS = (DETECTOR_OPENVINO, DETECTOR_CPU, DETECTOR_TENSORRT, DETECTOR_EDGETPU, DETECTOR_HAILO)


def recommended_detector(info: PlatformInfo | None = None) -> str:
    """Pick the best Frigate detector type the host can run."""
    info = info or detect()
    accel_kinds = {a.kind for a in info.accelerators}
    # Order matters — pick the most accurate / fastest available.
    if "hailo" in accel_kinds and info.os == "Linux":
        return DETECTOR_HAILO
    if "nvidia-gpu" in accel_kinds and info.os == "Linux":
        return DETECTOR_TENSORRT
    if "coral-usb" in accel_kinds:
        return DETECTOR_EDGETPU
    if "intel-igpu" in accel_kinds and info.os == "Linux":
        return DETECTOR_OPENVINO
    return DETECTOR_CPU


def vendor_label(cpu_vendor: str) -> str:
    return _VENDOR_NORMAL.get(cpu_vendor, cpu_vendor)
