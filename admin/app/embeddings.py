"""Image embedding extraction for pet recognition.

We embed each event snapshot into a 576-dim feature vector using
MobileNetV3-Small (ImageNet-pretrained, classifier head removed).
Cosine similarity between embeddings is what `recognition.py` uses to
match new events against the user's reference photos.

Why MobileNetV3-Small and not CLIP:
  - 10 MB model file, 30 ms / inference on CPU. CLIP ViT-B/32 is 150 MB
    and 5-10× slower per inference.
  - For "is this Mochi or Maru" within one home, generic ImageNet
    features are usually enough — the discriminator is fur pattern +
    body shape, both well-represented in pre-trained convnets.
  - Pluggable via PAWCORDER_EMBEDDING_MODEL_URL if a more accurate
    drop-in is needed later.

The model is downloaded lazily on first use to /data/models/, NOT baked
into the image — keeps the image small for users who don't enable
recognition.

Test isolation: every code path that does a real model load is gated
behind EmbeddingExtractor.load(). Tests inject a fake extractor via
set_extractor() so no model file is needed in CI.
"""
from __future__ import annotations

import io
import logging
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("pawcorder.embeddings")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
MODELS_DIR = DATA_DIR / "models"


# ---- backbone registry ------------------------------------------------
#
# We support multiple ONNX backbones, picked by env var. Each entry
# carries the URL we lazy-download from, the on-disk filename, the
# embedding dimension, and an input-size hint so preprocessing stays in
# lock-step. Adding a new backbone = one row here + the corresponding
# ONNX export hosted somewhere reachable.
#
# Why pluggable: MobileNetV3-Small (576-d, ImageNet) is fast and tiny,
# adequate for 1-pet households. DINOv2-Small (384-d, self-supervised)
# is more discriminative on fine-grained features — meaningful win for
# multi-pet same-breed setups where MobileNet's class-level features
# can't separate "this tabby" from "the other tabby". Operators opt in
# via PAWCORDER_EMBEDDING_BACKBONE; default keeps the historical 576-d
# MobileNet so existing pets.yml embeddings stay valid.

@dataclass(frozen=True)
class _BackboneSpec:
    """ONNX-runnable embedding model. Keep this dataclass plain — it
    serialises to PetPhoto.backbone via just the ``name`` field."""
    name: str
    url: str
    filename: str          # cached at MODELS_DIR / filename
    embedding_dim: int
    input_size: int = 224  # ImageNet-style; both supported backbones use 224


_BACKBONES: dict[str, _BackboneSpec] = {
    "mobilenetv3_small_100": _BackboneSpec(
        name="mobilenetv3_small_100",
        url=("https://huggingface.co/timm/mobilenetv3_small_100.lamb_in1k/"
             "resolve/main/onnx/model.onnx"),
        filename="embedding_model.onnx",  # historical name — keep
        embedding_dim=576,
    ),
    "dinov2_small": _BackboneSpec(
        name="dinov2_small",
        # ONNX export of facebook/dinov2-small. Apache-2.0, 22M params,
        # 384-d output. Public hosted ONNX from Hugging Face spaces.
        # Operator can override via PAWCORDER_EMBEDDING_MODEL_URL if
        # they prefer a self-hosted copy.
        url=("https://huggingface.co/onnx-community/dinov2-small/"
             "resolve/main/onnx/model.onnx"),
        filename="embedding_model_dinov2_small.onnx",
        embedding_dim=384,
    ),
}

DEFAULT_BACKBONE = "mobilenetv3_small_100"


def _active_backbone() -> _BackboneSpec:
    """Resolve the configured backbone, with safe fallback to the
    default. An unknown name doesn't crash — we log a warning and use
    the default so a typo can't take recognition down."""
    name = os.environ.get("PAWCORDER_EMBEDDING_BACKBONE", DEFAULT_BACKBONE)
    spec = _BACKBONES.get(name)
    if spec is None:
        logger.warning("unknown PAWCORDER_EMBEDDING_BACKBONE=%r, using %s",
                       name, DEFAULT_BACKBONE)
        spec = _BACKBONES[DEFAULT_BACKBONE]
    return spec


def active_backbone_name() -> str:
    """Public — what's running right now. Used by recognition to skip
    photos whose stored embeddings came from a different backbone, and
    by the re-enroll flow to know what to embed against."""
    return _active_backbone().name


def backbone_dim(name: Optional[str] = None) -> int:
    """Embedding dim for the named backbone (default: active). Tests
    use this to construct stubs without hard-coding 576/384."""
    if name is None:
        return _active_backbone().embedding_dim
    spec = _BACKBONES.get(name)
    return spec.embedding_dim if spec else _active_backbone().embedding_dim


def supported_backbones() -> list[dict]:
    """Surface for the System page: list of {name, dim} so the dropdown
    only ever offers backbones we actually know how to load."""
    return [{"name": s.name, "dim": s.embedding_dim} for s in _BACKBONES.values()]


# Module-level constants pinned at import time. Readers that still
# reference EMBEDDING_DIM / MODEL_URL get the active backbone's values
# *as of process start*. After a System-page swap, ``refresh_active()``
# below repopulates them and resets the extractor singleton — without
# that, in-process readers would see stale dims while
# ``active_backbone_name()`` reports the new one.
DEFAULT_MODEL_URL = _BACKBONES[DEFAULT_BACKBONE].url
MODEL_URL = os.environ.get(
    "PAWCORDER_EMBEDDING_MODEL_URL", _active_backbone().url,
)
MODEL_FILENAME = _active_backbone().filename
EMBEDDING_DIM = _active_backbone().embedding_dim
INPUT_SIZE = _active_backbone().input_size


def refresh_active() -> None:
    """Reload backbone-derived module state. Call after the operator
    changes ``PAWCORDER_EMBEDDING_BACKBONE`` so the running admin picks
    up the new dim / URL / filename without a full restart.

    Resets the extractor singleton too — the cached onnxruntime session
    is bound to whichever model file was loaded first; without this
    a backbone swap would silently keep using the old session.
    """
    global MODEL_URL, MODEL_FILENAME, EMBEDDING_DIM, INPUT_SIZE
    spec = _active_backbone()
    MODEL_URL = os.environ.get("PAWCORDER_EMBEDDING_MODEL_URL", spec.url)
    MODEL_FILENAME = spec.filename
    EMBEDDING_DIM = spec.embedding_dim
    INPUT_SIZE = spec.input_size
    set_extractor(None)


@dataclass
class EmbeddingResult:
    vector: np.ndarray  # shape (EMBEDDING_DIM,), L2-normalized
    success: bool = True
    error: str = ""


@dataclass
class MultiFrameResult:
    """Pooled embedding from multiple event frames + per-frame diagnostics.

    The diagnostics are useful for the UI ("3 of 5 frames usable") and
    for tuning — a recurring pattern of low quality_scores points at
    camera position / lighting issues the user could fix.
    """
    vector: np.ndarray
    success: bool = True
    error: str = ""
    frame_count: int = 0          # frames that successfully embedded
    quality_scores: list[float] | None = None
    weights: list[float] | None = None


# Cap how many frames we'll process per event. Empirically the marginal
# accuracy gain past ~6 frames is below the noise floor on indoor
# pet-cam footage, while inference cost scales linearly. 6 also keeps
# total CPU budget per event under ~200 ms on a Pi 5.
MAX_FRAMES_PER_EVENT = 6

# Softmax temperature on quality scores. Lower = peakier (one frame
# dominates); higher = flatter (closer to mean pooling). 0.5 lets the
# sharpest frame carry ~60-70% of the weight when there's a clear
# winner, but lets a runner-up still contribute 15-25% so we don't
# throw away all evidence on a single fluke frame.
QUALITY_TEMPERATURE = 0.5


class EmbeddingExtractor:
    """Wraps an onnxruntime session. Created lazily — the import of
    onnxruntime itself can take 100ms+ so we only pay it when the user
    actually enables recognition.

    Thread-safety: `extract()` calls `session.run()` which is reentrant
    in onnxruntime. No lock needed.
    """

    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self._session = None

    def load(self) -> bool:
        """Return True iff the ONNX model loaded successfully. Soft-fails
        on any error so the rest of the admin keeps working."""
        if self._session is not None:
            return True
        try:
            import onnxruntime  # local import: heavy dep, gated on use
        except ImportError as exc:
            logger.warning("onnxruntime not installed: %s", exc)
            return False
        if not self.model_path.exists():
            logger.warning("embedding model missing at %s — call download_model() first",
                           self.model_path)
            return False
        try:
            self._session = onnxruntime.InferenceSession(
                str(self.model_path),
                providers=["CPUExecutionProvider"],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load embedding model: %s", exc)
            return False

    def extract(self, image_bytes: bytes) -> EmbeddingResult:
        """Run one inference. Returns L2-normalized vector so cosine
        similarity is just a dot product downstream."""
        if not self.load():
            return EmbeddingResult(
                vector=np.zeros(EMBEDDING_DIM, dtype=np.float32),
                success=False, error="model not loaded",
            )
        try:
            arr = _preprocess(image_bytes)
        except Exception as exc:  # noqa: BLE001
            return EmbeddingResult(
                vector=np.zeros(EMBEDDING_DIM, dtype=np.float32),
                success=False, error=f"preprocess failed: {exc}",
            )
        try:
            assert self._session is not None  # for type checkers
            outputs = self._session.run(None, {self._session.get_inputs()[0].name: arr})
            vec = np.asarray(outputs[0]).reshape(-1).astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return EmbeddingResult(vector=vec, success=True)
        except Exception as exc:  # noqa: BLE001
            return EmbeddingResult(
                vector=np.zeros(EMBEDDING_DIM, dtype=np.float32),
                success=False, error=f"inference failed: {exc}",
            )

    def extract_many(self, frames: list[bytes]) -> "MultiFrameResult":
        """Embed up to N frames and pool them with per-frame quality weights.

        Quality = sharpness × brightness sanity. Sharpness uses the variance
        of a 3×3 Laplacian (a classic blur detector — high variance means
        crisp edges). Brightness sanity penalises blown-out / black frames
        which carry no identity signal even when the file decodes.

        Pooling: weighted mean of L2-normalized vectors, then re-normalize.
        Weights are softmax(quality / temperature) so a single bright,
        sharp frame dominates a cluster of blurry ones — but never to the
        exclusion of all others (temperature=0.5 leaves room for a runner
        up to contribute 10–30% if it's also decent).

        Returns an empty success=False result if every frame fails.
        """
        if not frames:
            return MultiFrameResult(
                vector=np.zeros(EMBEDDING_DIM, dtype=np.float32),
                success=False, error="no frames", frame_count=0,
            )
        # Cap the work — recognition pulls 4–8 frames per event, never more.
        # Beyond that the marginal gain is below noise floor and the cost
        # is linear.
        frames = frames[:MAX_FRAMES_PER_EVENT]

        per_frame: list[tuple[np.ndarray, float]] = []
        for body in frames:
            r = self.extract(body)
            if not r.success or r.vector.size == 0:
                continue
            try:
                q = _frame_quality(body)
            except Exception:  # noqa: BLE001
                q = 0.5  # neutral — prefer to include over drop
            per_frame.append((r.vector, q))

        if not per_frame:
            return MultiFrameResult(
                vector=np.zeros(EMBEDDING_DIM, dtype=np.float32),
                success=False, error="all frames failed",
                frame_count=0,
            )

        # Softmax over quality scores so the weights sum to 1 and the
        # sharpest frame dominates without zeroing out the others.
        qualities = np.array([q for _, q in per_frame], dtype=np.float32)
        weights = _softmax(qualities / QUALITY_TEMPERATURE)
        stacked = np.stack([v for v, _ in per_frame])  # (N, D)
        pooled = (weights[:, None] * stacked).sum(axis=0)
        norm = float(np.linalg.norm(pooled))
        if norm > 0:
            pooled = pooled / norm
        return MultiFrameResult(
            vector=pooled.astype(np.float32),
            success=True,
            frame_count=len(per_frame),
            quality_scores=qualities.tolist(),
            weights=weights.tolist(),
        )


def _preprocess(image_bytes: bytes) -> np.ndarray:
    """ImageNet preprocessing: resize to 224×224, normalize, CHW float32.

    Pillow is already a transitive dep of qrcode, so no new requirement.
    """
    from PIL import Image  # local: keeps qrcode-only callers fast

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize(
        (INPUT_SIZE, INPUT_SIZE), Image.BILINEAR,
    )
    arr = np.asarray(img, dtype=np.float32) / 255.0
    # ImageNet mean / std normalization (RGB)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    arr = arr.transpose(2, 0, 1)  # HWC → CHW
    return arr[np.newaxis, ...]   # add batch dim → 1×3×224×224


def _frame_quality(image_bytes: bytes) -> float:
    """Cheap single-frame quality score in [0, 1].

    Combines two signals that don't need any model:
      * **Sharpness** — variance of a 3×3 Laplacian convolution on the
        grayscale image. Standard blur detector (Pech-Pacheco 2000); high
        variance ⇒ crisp edges. We squash via tanh so the curve flattens
        for already-sharp frames (no extra reward for absurdly crisp).
      * **Brightness sanity** — penalises frames whose mean luminance is
        below 15/255 (black) or above 240/255 (blown out). A camera with
        the IR cut filter stuck wide open produces unusable embeddings;
        we want those down-weighted, not dropped (sometimes that *is* the
        only frame we have).

    Failure → 0.5 (neutral) so a quirky decode doesn't penalise a frame
    that the embedding model itself was happy with.
    """
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:  # noqa: BLE001
        return 0.5
    # 256-side downsample is plenty for a blur detector and keeps the
    # convolution under a millisecond on Pi-class hardware.
    if img.width > 256 or img.height > 256:
        img.thumbnail((256, 256), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32)
    if arr.size == 0:
        return 0.5
    # 3×3 Laplacian kernel — same one OpenCV ships as cv2.Laplacian.
    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    # Manual valid-padding 2D conv via stride tricks — avoids pulling in
    # scipy / cv2 for a 9-tap kernel.
    h, w = arr.shape
    if h < 3 or w < 3:
        return 0.3
    sub = (
        arr[0:h - 2, 0:w - 2] * kernel[0, 0]
        + arr[0:h - 2, 1:w - 1] * kernel[0, 1]
        + arr[0:h - 2, 2:w - 0] * kernel[0, 2]
        + arr[1:h - 1, 0:w - 2] * kernel[1, 0]
        + arr[1:h - 1, 1:w - 1] * kernel[1, 1]
        + arr[1:h - 1, 2:w - 0] * kernel[1, 2]
        + arr[2:h - 0, 0:w - 2] * kernel[2, 0]
        + arr[2:h - 0, 1:w - 1] * kernel[2, 1]
        + arr[2:h - 0, 2:w - 0] * kernel[2, 2]
    )
    sharpness = float(sub.var())
    # 100 is the empirical "this is a usable frame" knee on indoor pet
    # cams. tanh(x/100) saturates around 0.95 for sharpness >= 200.
    sharp_score = float(np.tanh(sharpness / 100.0))

    mean_lum = float(arr.mean())
    if mean_lum < 15 or mean_lum > 240:
        bright_score = 0.2          # very dark / very bright — penalise hard
    elif mean_lum < 30 or mean_lum > 220:
        bright_score = 0.6          # marginal
    else:
        bright_score = 1.0          # well-exposed

    # Multiplicative — both must be reasonable. Floor at 0.05 so even the
    # worst frame doesn't get zero weight (softmax handles the rest).
    return max(0.05, sharp_score * bright_score)


def _softmax(x: np.ndarray) -> np.ndarray:
    """Numerically stable softmax over a 1-D array."""
    if x.size == 0:
        return x
    shifted = x - x.max()
    e = np.exp(shifted)
    s = e.sum()
    if s <= 0:
        # All -inf or NaN — fall back to uniform so the caller gets a
        # mean-pool rather than a zero vector.
        return np.full_like(x, 1.0 / x.size)
    return e / s


def model_path() -> Path:
    return MODELS_DIR / MODEL_FILENAME


def download_model(*, force: bool = False, timeout: float = 60.0) -> bool:
    """Fetch the ONNX model to MODELS_DIR. Idempotent.

    Returns True on success or if the file already exists. Network errors
    return False without raising — the recognition layer treats
    "model unavailable" the same as "feature disabled".

    Two writers can race here: the System-page save spawns a warm-up
    thread that calls into this function, and an immediate Re-enroll
    click triggers another download via ``EmbeddingExtractor.load``.
    The temp path used to be a fixed ``.downloading`` suffix — both
    threads would write to the same name and one's `os.replace` could
    target the other's mid-write file. Suffixing with PID + thread id
    gives each writer its own tmp; whichever finishes first wins the
    `os.replace`, the other one stomps the same target with identical
    bytes (or a partial rename of its own tmp), still ending in a
    valid file because both pulled the same URL.
    """
    target = model_path()
    if target.exists() and not force:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    import threading as _t
    tmp = target.with_suffix(
        f"{target.suffix}.{os.getpid()}.{_t.get_ident()}.downloading"
    )
    # HuggingFace's CDN returns 403 to the default urllib User-Agent
    # ("Python-urllib/3.x"). A plain UA string gets us through; we
    # don't need to pretend to be a browser.
    request = urllib.request.Request(
        MODEL_URL, headers={"User-Agent": "pawcorder-admin/1.0"},
    )
    try:
        logger.info("downloading embedding model from %s", MODEL_URL)
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            data = resp.read()
        tmp.write_bytes(data)
        os.replace(tmp, target)
        logger.info("embedding model saved to %s (%d bytes)", target, len(data))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to download embedding model: %s", exc)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Both vectors expected to be L2-normalized (extract() does this).
    With normalized inputs cosine similarity == dot product."""
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float(np.dot(a, b))


# ---- module-level singleton with test override ------------------------

_extractor: Optional[EmbeddingExtractor] = None


def get_extractor() -> EmbeddingExtractor:
    """Lazy module-singleton. Tests override via set_extractor()."""
    global _extractor
    if _extractor is None:
        _extractor = EmbeddingExtractor(model_path())
    return _extractor


def set_extractor(ext: Optional[EmbeddingExtractor]) -> None:
    """Inject (or reset to None) for tests."""
    global _extractor
    _extractor = ext
