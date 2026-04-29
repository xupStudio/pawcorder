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
