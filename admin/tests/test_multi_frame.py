"""Multi-frame quality-weighted aggregation tests.

Covers the new ``EmbeddingExtractor.extract_many`` and the recognition-
side ``identify_event(frames=[...])`` route. We never load the real ONNX
model in CI — the embedding extractor is monkey-patched to a stub that
returns deterministic vectors keyed off the input bytes. This way a
regression in the pooling math shows up as a vector mismatch, not as a
"why won't onnxruntime import" CI flake.
"""
from __future__ import annotations

import io
import json

import numpy as np
import pytest


def _gradient_jpeg(brightness: int, sharpness: str = "sharp") -> bytes:
    """Produce a small JPEG with predictable luminance + sharpness so
    ``_frame_quality`` exercises both halves of its score function.

    ``brightness`` ∈ [0, 255] — the mean grey level.
    ``sharpness`` ∈ {"sharp", "blurry"} — sharp uses a checkerboard so
    the Laplacian variance is high; blurry uses a flat fill which the
    detector should down-weight.
    """
    from PIL import Image
    if sharpness == "sharp":
        # 8×8 checkerboard scaled up — high-frequency = high Laplacian.
        # Use signed intermediate so brightness-30 doesn't underflow uint8
        # for low-brightness fixtures.
        small = np.zeros((8, 8), dtype=np.int16)
        small[::2, ::2] = brightness + 30
        small[1::2, 1::2] = brightness + 30
        small[::2, 1::2] = brightness - 30
        small[1::2, ::2] = brightness - 30
        small = np.clip(small, 0, 255).astype(np.uint8)
        img = Image.fromarray(small).resize((128, 128), Image.NEAREST)
    else:
        img = Image.new("L", (128, 128), color=brightness)
    rgb = Image.merge("RGB", (img, img, img))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=80)
    return out.getvalue()


class _StubExtractor:
    """Drop-in for the real ONNX embedder. Emits deterministic vectors
    so we can assert on pooled output. Provides ``extract_many`` so it
    plumbs through recognition.identify_event's multi-frame path."""

    def __init__(self):
        self.calls: list[bytes] = []

    def extract(self, image_bytes: bytes):
        from app import embeddings
        self.calls.append(image_bytes)
        # Hash the bytes into a 576-d unit vector — different inputs
        # yield different embeddings, identical inputs collide.
        rng = np.random.default_rng(seed=int.from_bytes(image_bytes[:8], "big") or 1)
        v = rng.standard_normal(embeddings.EMBEDDING_DIM).astype(np.float32)
        v = v / max(float(np.linalg.norm(v)), 1e-6)
        return embeddings.EmbeddingResult(vector=v, success=True)

    def extract_many(self, frames):
        """Mirror EmbeddingExtractor.extract_many shape — equal-weight
        pool of per-frame vectors. The real implementation does softmax-
        weighted pooling; here we use uniform weights so test assertions
        depend only on the byte inputs, not on quality-detector quirks."""
        from app import embeddings
        per_frame = [self.extract(b).vector for b in frames]
        if not per_frame:
            return embeddings.MultiFrameResult(
                vector=np.zeros(embeddings.EMBEDDING_DIM, dtype=np.float32),
                success=False, error="no frames", frame_count=0,
            )
        stacked = np.stack(per_frame)
        pooled = stacked.mean(axis=0)
        norm = float(np.linalg.norm(pooled))
        if norm > 0:
            pooled = pooled / norm
        return embeddings.MultiFrameResult(
            vector=pooled.astype(np.float32),
            success=True, frame_count=len(per_frame),
        )


def test_frame_quality_prefers_sharp_over_flat():
    from app import embeddings
    sharp = _gradient_jpeg(brightness=128, sharpness="sharp")
    flat = _gradient_jpeg(brightness=128, sharpness="blurry")
    q_sharp = embeddings._frame_quality(sharp)
    q_flat = embeddings._frame_quality(flat)
    assert q_sharp > q_flat
    # Both must remain above the floor — blurry isn't penalised to zero.
    assert q_flat >= 0.05


def test_frame_quality_penalises_dark_and_blown_out():
    from app import embeddings
    dark = _gradient_jpeg(brightness=5, sharpness="sharp")
    bright = _gradient_jpeg(brightness=250, sharpness="sharp")
    normal = _gradient_jpeg(brightness=128, sharpness="sharp")
    assert embeddings._frame_quality(normal) > embeddings._frame_quality(dark)
    assert embeddings._frame_quality(normal) > embeddings._frame_quality(bright)


def test_extract_many_pools_equal_quality_frames():
    from app import embeddings
    stub = _StubExtractor()
    real_quality = embeddings._frame_quality
    embeddings.set_extractor(stub)
    try:
        # Stub the quality function to return identical scores so we can
        # assert on the math without depending on JPEG content.
        embeddings._frame_quality = lambda b: 0.7  # type: ignore[assignment]
        a = b"frame_aa" + b"\x00" * 16
        b = b"frame_bb" + b"\x00" * 16
        # Use the real EmbeddingExtractor.extract_many but with the stub
        # injected as the underlying extractor: we instantiate a real
        # one and bind its extract() to the stub.
        ext = embeddings.EmbeddingExtractor(model_path=None)  # type: ignore[arg-type]
        ext.extract = stub.extract  # type: ignore[assignment]
        m = ext.extract_many([a, b])
        assert m.success
        assert m.frame_count == 2
        # With equal quality the pooled vector is the L2-normed mean.
        v_a = stub.extract(a).vector
        v_b = stub.extract(b).vector
        # Note: the stub is deterministic, but we called it twice during
        # the recompute; deterministic bytes-in -> bytes-out.
        expected = (v_a + v_b) / 2
        expected = expected / max(float(np.linalg.norm(expected)), 1e-6)
        assert np.allclose(m.vector, expected, atol=1e-5)
    finally:
        embeddings._frame_quality = real_quality  # type: ignore[assignment]
        embeddings.set_extractor(None)


def test_extract_many_handles_all_failures():
    from app import embeddings

    class _Fail:
        def extract(self, _):
            return embeddings.EmbeddingResult(
                vector=np.zeros(embeddings.EMBEDDING_DIM, dtype=np.float32),
                success=False, error="stub-fail",
            )

    embeddings.set_extractor(_Fail())
    try:
        ext = embeddings.EmbeddingExtractor(model_path=None)  # type: ignore[arg-type]
        ext.extract = _Fail().extract
        m = ext.extract_many([b"x", b"y"])
        assert m.success is False
        assert m.frame_count == 0
    finally:
        embeddings.set_extractor(None)


def test_extract_many_caps_to_max_frames():
    """Caller can dump 20 frames in; we should only process MAX_FRAMES_PER_EVENT."""
    from app import embeddings
    stub = _StubExtractor()
    real_quality = embeddings._frame_quality
    embeddings._frame_quality = lambda b: 0.5  # type: ignore[assignment]
    try:
        ext = embeddings.EmbeddingExtractor(model_path=None)  # type: ignore[arg-type]
        ext.extract = stub.extract
        many = [bytes([i]) * 16 for i in range(20)]
        m = ext.extract_many(many)
        assert m.frame_count == embeddings.MAX_FRAMES_PER_EVENT
        assert len(stub.calls) == embeddings.MAX_FRAMES_PER_EVENT
    finally:
        embeddings._frame_quality = real_quality  # type: ignore[assignment]


def test_identify_event_records_frames_used(data_dir, monkeypatch):
    """End-to-end: feeding two frames lands frames_used=2 in the log.

    Uses the shared ``data_dir`` fixture so PAWCORDER_DATA_DIR is set
    *before* importing app modules — that's how SIGHTINGS_LOG and
    PETS_YAML pick up the temp path."""
    from app import recognition, embeddings, pets_store

    stub = _StubExtractor()
    embeddings.set_extractor(stub)

    # Force a reference photo whose embedding matches one of the frames.
    one_frame = b"frame_aa" + b"\x00" * 16
    one_emb = stub.extract(one_frame).vector
    pets_store.PETS_YAML.parent.mkdir(parents=True, exist_ok=True)
    pet = pets_store.Pet(
        pet_id="mochi", name="Mochi", species="cat",
        notes="", match_threshold=0.0,
        photos=[pets_store.PetPhoto(
            filename="x.jpg", embedding=one_emb.tolist(),
        )],
    )
    pets_store.PetStore().save_all([pet])

    try:
        real_q = embeddings._frame_quality
        embeddings._frame_quality = lambda b: 0.6  # type: ignore[assignment]
        result = recognition.identify_event(
            [one_frame, b"frame_bb" + b"\x00" * 16],
            event_id="evt-multi",
            camera="cam1", label="cat",
            start_time=1700000000.0, end_time=1700000001.0,
        )
        assert result.frames_used == 2
        line = recognition.SIGHTINGS_LOG.read_text().strip()
        row = json.loads(line)
        assert row.get("frames_used") == 2
    finally:
        embeddings._frame_quality = real_q  # type: ignore[assignment]
        embeddings.set_extractor(None)
