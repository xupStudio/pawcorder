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

# MobileNetV3-Small with classifier removed, exported from torchvision.
# Output: 576-dim feature vector, post global-avg-pool, pre-classifier.
DEFAULT_MODEL_URL = (
    "https://huggingface.co/timm/mobilenetv3_small_100.lamb_in1k/resolve/"
    "main/onnx/model.onnx"
)
MODEL_URL = os.environ.get("PAWCORDER_EMBEDDING_MODEL_URL", DEFAULT_MODEL_URL)
MODEL_FILENAME = "embedding_model.onnx"
EMBEDDING_DIM = 576  # MobileNetV3-Small penultimate features
INPUT_SIZE = 224     # standard ImageNet preprocessing


@dataclass
class EmbeddingResult:
    vector: np.ndarray  # shape (EMBEDDING_DIM,), L2-normalized
    success: bool = True
    error: str = ""


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


def model_path() -> Path:
    return MODELS_DIR / MODEL_FILENAME


def download_model(*, force: bool = False, timeout: float = 60.0) -> bool:
    """Fetch the ONNX model to MODELS_DIR. Idempotent.

    Returns True on success or if the file already exists. Network errors
    return False without raising — the recognition layer treats
    "model unavailable" the same as "feature disabled".
    """
    target = model_path()
    if target.exists() and not force:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".downloading")
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
