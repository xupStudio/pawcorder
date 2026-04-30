"""Talk to the host Docker daemon via the mounted socket.

The admin container runs with /var/run/docker.sock mounted, so we control
the Frigate container and inspect compose state from here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator

import docker
from docker.errors import APIError, NotFound

FRIGATE_CONTAINER = os.environ.get("FRIGATE_CONTAINER", "pawcorder-frigate")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "pawcorder")


@dataclass
class ContainerStatus:
    name: str
    exists: bool
    running: bool
    status: str
    health: str | None
    image: str | None


def _client() -> docker.DockerClient:
    return docker.from_env()


def get_frigate_status() -> ContainerStatus:
    try:
        c = _client().containers.get(FRIGATE_CONTAINER)
    except NotFound:
        return ContainerStatus(
            name=FRIGATE_CONTAINER,
            exists=False,
            running=False,
            status="not_created",
            health=None,
            image=None,
        )
    state = c.attrs.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    return ContainerStatus(
        name=FRIGATE_CONTAINER,
        exists=True,
        running=state.get("Running", False),
        status=state.get("Status", "unknown"),
        health=health,
        image=c.image.tags[0] if c.image and c.image.tags else None,
    )


def restart_frigate() -> None:
    try:
        c = _client().containers.get(FRIGATE_CONTAINER)
    except NotFound as exc:
        raise RuntimeError(
            "Frigate container does not exist yet. Run `make up` after setup."
        ) from exc
    c.restart(timeout=20)


def stream_frigate_logs(tail: int = 200) -> Iterator[bytes]:
    try:
        c = _client().containers.get(FRIGATE_CONTAINER)
    except NotFound:
        return iter(())
    return c.logs(tail=tail, stream=True, follow=False)


def recent_frigate_logs(tail: int = 200) -> str:
    try:
        c = _client().containers.get(FRIGATE_CONTAINER)
    except NotFound:
        return ""
    try:
        return c.logs(tail=tail, stream=False, follow=False).decode("utf-8", errors="replace")
    except APIError as exc:
        return f"<error reading logs: {exc}>"


async def compose_pull_and_up() -> dict:
    """Trigger ``docker compose pull && docker compose up -d`` against
    the host. Used by the OTA "Apply update" button.

    We don't try to be clever with the Docker SDK here — compose isn't
    a first-class API in `docker-py`. The path of least surprise is to
    shell out to the same `docker compose` the user runs by hand.

    Returns ``{"ok": bool, "stdout": str, "stderr": str}``. The caller
    should expect to LOSE the connection mid-call once `up -d` recreates
    the admin container — the HTTP response is best-effort.
    """
    import asyncio

    project_root = os.environ.get("PAWCORDER_PROJECT_ROOT", "/opt/pawcorder")
    compose_file = os.environ.get(
        "PAWCORDER_COMPOSE_FILE",
        os.path.join(project_root, "docker-compose.yml"),
    )
    cmd = ["docker", "compose", "-f", compose_file, "pull"]
    pull = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    pull_out, pull_err = await pull.communicate()
    if pull.returncode != 0:
        return {"ok": False,
                "stdout": pull_out.decode("utf-8", errors="replace"),
                "stderr": pull_err.decode("utf-8", errors="replace"),
                "step": "pull"}
    up = await asyncio.create_subprocess_exec(
        "docker", "compose", "-f", compose_file, "up", "-d",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    up_out, up_err = await up.communicate()
    return {
        "ok": up.returncode == 0,
        "stdout": (pull_out + up_out).decode("utf-8", errors="replace"),
        "stderr": (pull_err + up_err).decode("utf-8", errors="replace"),
        "step": "up" if up.returncode != 0 else "done",
    }
