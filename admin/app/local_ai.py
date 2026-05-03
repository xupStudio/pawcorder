"""Local LLM via Ollama — auto-detect, install, RAM-aware model picker.

The /system page used to ask the user to type the Ollama base URL and
the model name. Two paste fields plus the assumption that the user knows
what models exist. This module replaces both with auto-detection +
sensible defaults derived from the host's RAM.

Detection: probe ``$OLLAMA_BASE/api/tags`` (default ``http://127.0.0.1:11434``).
If 200, list the user's existing models. If unreachable, the UI offers
a one-click install button that runs Ollama's official installer
script (MIT, https://github.com/ollama/ollama).

Model recommendation: pick the smallest ``qwen2.5`` variant that fits in
RAM. Qwen 2.5 is Apache-2.0 and pet-diary-quality at every size.

Pull-model: stream POST ``/api/pull`` so the UI can show progress.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import httpx

logger = logging.getLogger("pawcorder.local_ai")

DEFAULT_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
PROBE_TIMEOUT = 1.5


@dataclass
class Status:
    reachable: bool
    base_url: str
    models: list[str]                 # ["qwen2.5:3b", "phi3.5", …] — empty if none pulled
    binary_present: bool              # is the `ollama` CLI on PATH?
    error: str = ""


# RAM tier → recommended model. Sized to leave headroom for the OS and
# Pawcorder itself; users can override in the UI if they want bigger.
_RECOMMENDED_BY_RAM_GB = [
    (4,  "qwen2.5:0.5b"),     # < 4 GB total → micro model
    (8,  "qwen2.5:3b"),       # 4–8 GB → 3B fits comfortably
    (16, "qwen2.5:7b"),       # 8–16 GB → 7B is the sweet spot
    (32, "qwen2.5:14b"),      # 16–32 GB → step up
]
DEFAULT_MODEL = "qwen2.5:3b"


def _ram_total_gb() -> int:
    """Best-effort host RAM in GB. Falls back to 8 GB so the recommended
    default stays sensible even if /proc isn't readable."""
    # /proc/meminfo is the most portable path on Linux containers.
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return max(1, kb // (1024 * 1024))
    except OSError:
        pass
    # macOS / non-procfs hosts: psutil is widely available but optional;
    # fall through to a safe default rather than adding a hard dep.
    try:
        import psutil  # type: ignore
        return max(1, psutil.virtual_memory().total // (1024 ** 3))
    except ImportError:
        return 8


def recommend_model() -> str:
    """Pick the smallest ``qwen2.5`` variant that comfortably fits in RAM."""
    ram = _ram_total_gb()
    for cap, model in _RECOMMENDED_BY_RAM_GB:
        if ram <= cap:
            return model
    # Above 32 GB → still default to 14B, larger models can't be quantised
    # in the user's favour without ggml manual work.
    return _RECOMMENDED_BY_RAM_GB[-1][1]


async def status(base_url: str | None = None) -> Status:
    """Probe Ollama at the given base URL (or DEFAULT_BASE). Never raises."""
    base = (base_url or DEFAULT_BASE).rstrip("/")
    binary_present = shutil.which("ollama") is not None
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(f"{base}/api/tags")
        if resp.status_code != 200:
            return Status(reachable=False, base_url=base, models=[],
                          binary_present=binary_present,
                          error=f"HTTP {resp.status_code}")
        data = resp.json()
        models = [m.get("name") or m.get("model") for m in (data.get("models") or [])]
        models = [m for m in models if m]
        return Status(reachable=True, base_url=base, models=models,
                      binary_present=binary_present)
    except (httpx.HTTPError, ValueError) as exc:
        return Status(reachable=False, base_url=base, models=[],
                      binary_present=binary_present, error=str(exc)[:200])


def install() -> tuple[bool, str]:
    """Run Ollama's official install script.

    On Linux/macOS: ``curl -fsSL https://ollama.com/install.sh | sh``
    On Windows: not supported here (user runs OllamaSetup.exe directly via WSL2).

    Returns (ok, output) — the output is truncated to keep the response
    payload reasonable.
    """
    cmd = "curl -fsSL https://ollama.com/install.sh | sh"
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"installer failed: {exc}"
    combined = (proc.stdout + proc.stderr).strip()[:4096]
    return proc.returncode == 0, combined


async def pull_model(model: str, base_url: str | None = None,
                      timeout: float = 600.0) -> tuple[bool, str]:
    """Trigger ``POST /api/pull`` and stream until done. Returns
    (ok, last_status_line). The caller can keep the connection short
    by reading progress from the response stream and updating UI.
    """
    base = (base_url or DEFAULT_BASE).rstrip("/")
    last_line = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{base}/api/pull",
                json={"name": model, "stream": True},
            ) as resp:
                if resp.status_code != 200:
                    return False, f"HTTP {resp.status_code}"
                async for chunk in resp.aiter_lines():
                    if chunk.strip():
                        last_line = chunk.strip()[:200]
                # `success` is the final status emitted on a clean pull.
                return ("success" in last_line.lower()), last_line
    except httpx.HTTPError as exc:
        return False, f"pull failed: {exc}"
