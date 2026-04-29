"""Tests for platform_detect module."""
from __future__ import annotations

import pytest

from app import platform_detect as pd
from app.platform_detect import (
    Accelerator,
    PlatformInfo,
    DETECTOR_CPU,
    DETECTOR_OPENVINO,
    DETECTOR_TENSORRT,
    DETECTOR_EDGETPU,
    DETECTOR_HAILO,
    recommended_detector,
)


def _info(**overrides) -> PlatformInfo:
    base = dict(
        os="Linux",
        distro_id="ubuntu",
        distro_version="24.04",
        arch="x86_64",
        cpu_vendor="GenuineIntel",
        cpu_brand="Intel(R) N100",
        accelerators=[],
        docker_available=True,
        is_dev_host=False,
    )
    base.update(overrides)
    return PlatformInfo(**base)


def test_detect_returns_platform_info():
    info = pd.detect()
    assert info.os in ("Linux", "Darwin", "Windows")
    assert info.arch != ""
    assert info.cpu_brand != ""


def test_recommended_detector_intel_igpu_on_linux():
    info = _info(accelerators=[Accelerator(kind="intel-igpu", detail="iGPU")])
    assert recommended_detector(info) == DETECTOR_OPENVINO


def test_recommended_detector_intel_igpu_on_mac_falls_back_to_cpu():
    """Even with /dev/dri exists, on Darwin it's a VM, OpenVINO won't reach iGPU."""
    info = _info(os="Darwin", accelerators=[Accelerator(kind="intel-igpu", detail="iGPU")])
    assert recommended_detector(info) == DETECTOR_CPU


def test_recommended_detector_nvidia_gpu_linux():
    info = _info(accelerators=[Accelerator(kind="nvidia-gpu", detail="RTX")])
    assert recommended_detector(info) == DETECTOR_TENSORRT


def test_recommended_detector_coral_usb():
    info = _info(accelerators=[Accelerator(kind="coral-usb", detail="Coral")])
    assert recommended_detector(info) == DETECTOR_EDGETPU


def test_recommended_detector_hailo_linux():
    info = _info(accelerators=[Accelerator(kind="hailo", detail="Hailo-8L")])
    assert recommended_detector(info) == DETECTOR_HAILO


def test_recommended_detector_apple_silicon_falls_back_to_cpu():
    info = _info(
        os="Darwin", arch="arm64", cpu_vendor="Apple",
        accelerators=[Accelerator(kind="apple-silicon", detail="M2")],
        is_dev_host=True,
    )
    assert recommended_detector(info) == DETECTOR_CPU


def test_recommended_detector_no_accelerator_returns_cpu():
    info = _info(accelerators=[])
    assert recommended_detector(info) == DETECTOR_CPU


def test_recommended_detector_picks_best_when_multiple():
    """Hailo > NVIDIA > Coral > iGPU > CPU."""
    info = _info(accelerators=[
        Accelerator(kind="intel-igpu", detail="iGPU"),
        Accelerator(kind="hailo", detail="Hailo"),
        Accelerator(kind="coral-usb", detail="Coral"),
    ])
    assert recommended_detector(info) == DETECTOR_HAILO


def test_to_dict_serializable():
    info = _info(accelerators=[Accelerator(kind="intel-igpu", detail="iGPU at /dev/dri/renderD128")])
    d = info.to_dict()
    import json
    json.dumps(d)  # must round-trip through JSON


def test_intel_igpu_detection_uses_filesystem(tmp_path, monkeypatch):
    """When /dev/dri/renderD128 exists, the detector should pick it up."""
    # We can't create real device files in tests, so just verify the function
    # honours os.path.exists.
    monkeypatch.setattr("app.platform_detect.os.path.exists", lambda p: p == "/dev/dri/renderD128")
    accel = pd._detect_intel_igpu()
    assert accel is not None
    assert accel.kind == "intel-igpu"


def test_nvidia_detection_via_which(monkeypatch):
    monkeypatch.setattr("app.platform_detect.shutil.which", lambda cmd: "/usr/bin/nvidia-smi" if cmd == "nvidia-smi" else None)
    accel = pd._detect_nvidia_gpu()
    assert accel is not None
    assert accel.kind == "nvidia-gpu"


def test_no_accelerator_when_nothing_present(monkeypatch):
    monkeypatch.setattr("app.platform_detect.os.path.exists", lambda p: False)
    monkeypatch.setattr("app.platform_detect.shutil.which", lambda cmd: None)
    assert pd._detect_intel_igpu() is None
    assert pd._detect_nvidia_gpu() is None
    assert pd._detect_hailo() is None


@pytest.mark.parametrize("vendor, expected", [
    ("GenuineIntel", "Intel"),
    ("AuthenticAMD", "AMD"),
    ("Apple", "Apple"),
    ("Unknown Vendor", "Unknown Vendor"),
])
def test_vendor_label(vendor, expected):
    assert pd.vendor_label(vendor) == expected
