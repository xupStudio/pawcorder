"""Small cross-cutting helpers that don't belong to any one module.

Everything here is intentionally dependency-free (stdlib only) so it
can be imported from anywhere — config_store, privacy, cameras_store,
backup — without creating circular imports.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import UploadFile


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Atomic UTF-8 write: temp sibling + os.replace.

    Why we keep reaching for this: any code path that owns a critical
    on-disk file (.env, cameras.yml, config.yml, privacy.json) must
    survive being killed mid-write. Plain Path.write_text truncates the
    target first — a kill between truncate and re-fill leaves a half-
    written file that the loader silently reads as empty/default,
    silently dropping the user's data.

    The .writing temp lives in the same directory so os.replace is a
    rename within a filesystem (atomic on POSIX, near-atomic on Windows
    via MoveFileEx). chmod is best-effort — bind-mounted volumes from
    macOS Docker hosts can refuse it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".writing")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except (PermissionError, OSError):
        # PermissionError on shared volumes; OSError on read-only fs.
        # Either way the temp's content is correct, just keep going.
        pass
    os.replace(tmp, path)


class UploadTooLarge(Exception):
    """Raised by read_capped_upload when the input exceeds max_bytes."""


async def read_capped_upload(file: "UploadFile", max_bytes: int) -> bytes:
    """Read an UploadFile in chunks, aborting as soon as we cross the cap.

    Plain `await file.read()` buffers the whole multipart into memory
    before any size check runs — that's an OOM vector for any public
    upload route. Instead we stream 64 KB at a time and throw
    UploadTooLarge once we've seen too much, leaving the rest unread
    so starlette discards it.

    Returns the bytes if size <= max_bytes, raises UploadTooLarge
    otherwise.
    """
    chunk_size = 64 * 1024
    out = bytearray()
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        if len(out) + len(chunk) > max_bytes:
            raise UploadTooLarge(
                f"upload exceeded {max_bytes} bytes (saw {len(out) + len(chunk)})"
            )
        out.extend(chunk)
    return bytes(out)
