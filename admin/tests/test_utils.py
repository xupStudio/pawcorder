"""Tests for the small atomic_write_text helper used by every code path
that owns a critical on-disk file."""
from __future__ import annotations

import os

import pytest


def test_atomic_write_creates_file(tmp_path):
    from app.utils import atomic_write_text
    target = tmp_path / "x.txt"
    atomic_write_text(target, "hello\nworld\n")
    assert target.read_text() == "hello\nworld\n"


def test_atomic_write_creates_missing_parent(tmp_path):
    """If the parent directory doesn't exist yet, we make it."""
    from app.utils import atomic_write_text
    target = tmp_path / "deep" / "tree" / "x.txt"
    atomic_write_text(target, "hi")
    assert target.read_text() == "hi"


def test_atomic_write_replaces_existing_file(tmp_path):
    from app.utils import atomic_write_text
    target = tmp_path / "x.txt"
    target.write_text("old content")
    atomic_write_text(target, "new content")
    assert target.read_text() == "new content"


def test_atomic_write_temp_cleaned_up_on_success(tmp_path):
    """The .writing temp must be renamed away, not left behind."""
    from app.utils import atomic_write_text
    target = tmp_path / "x.txt"
    atomic_write_text(target, "ok")
    leftovers = list(tmp_path.glob("*.writing"))
    assert leftovers == []


def test_atomic_write_preserves_target_on_replace_failure(tmp_path, monkeypatch):
    """The whole point of atomic write — a kill mid-replace must leave
    the old file intact."""
    from app import utils
    target = tmp_path / "x.txt"
    target.write_text("OLD")

    def _boom(*_args, **_kw):
        raise OSError("kernel said no")
    monkeypatch.setattr(utils.os, "replace", _boom)

    with pytest.raises(OSError):
        utils.atomic_write_text(target, "NEW")

    assert target.read_text() == "OLD"


def test_atomic_write_chmod_failure_doesnt_block_replace(tmp_path, monkeypatch):
    """chmod failures (read-only volumes, macOS bind mounts) must not
    propagate — we want the write to succeed even when chmod can't."""
    from app import utils
    target = tmp_path / "x.txt"

    def _chmod_boom(*_args, **_kw):
        raise PermissionError("mounted read-only at metadata layer")
    monkeypatch.setattr(utils.os, "chmod", _chmod_boom)

    utils.atomic_write_text(target, "still works")
    assert target.read_text() == "still works"


# ---- read_capped_upload ------------------------------------------------

class _FakeUploadFile:
    """Minimal UploadFile stand-in: returns chunks from a bytes buffer."""
    def __init__(self, data: bytes, chunk_size: int = 1024):
        self._data = data
        self._cursor = 0
        self._chunk_size = chunk_size
    async def read(self, size: int = -1) -> bytes:
        if size <= 0 or size is None:
            chunk = self._data[self._cursor:]
            self._cursor = len(self._data)
            return chunk
        chunk = self._data[self._cursor:self._cursor + size]
        self._cursor += len(chunk)
        return chunk


def test_read_capped_upload_under_limit(tmp_path):
    import asyncio
    from app.utils import read_capped_upload
    f = _FakeUploadFile(b"x" * 5000)
    out = asyncio.run(read_capped_upload(f, max_bytes=10_000))
    assert out == b"x" * 5000


def test_read_capped_upload_aborts_when_over_limit(tmp_path):
    """The whole point — we throw BEFORE buffering the rest of the
    upload, so a 1 GB attack doesn't OOM us."""
    import asyncio
    from app.utils import read_capped_upload, UploadTooLarge
    import pytest

    f = _FakeUploadFile(b"x" * (1024 * 1024 * 50))  # 50 MB
    with pytest.raises(UploadTooLarge):
        asyncio.run(read_capped_upload(f, max_bytes=10 * 1024 * 1024))


def test_read_capped_upload_exactly_at_limit(tmp_path):
    """Boundary: cap = N bytes, upload = N bytes → succeeds."""
    import asyncio
    from app.utils import read_capped_upload
    f = _FakeUploadFile(b"x" * 1000)
    out = asyncio.run(read_capped_upload(f, max_bytes=1000))
    assert len(out) == 1000


def test_read_capped_upload_empty(tmp_path):
    import asyncio
    from app.utils import read_capped_upload
    f = _FakeUploadFile(b"")
    out = asyncio.run(read_capped_upload(f, max_bytes=1000))
    assert out == b""
