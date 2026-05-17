"""Embedding backends — fastembed (default) or sentence-transformers (opt-in).

Selection is deliberate and lazy: nothing is imported at module load, so
``office_layer`` keeps installing on cheap PCs without pulling onnxruntime or
PyTorch. The Engine asks ``select_embedder()`` for the best-available backend;
if none is installed, ``NullEmbedder`` keeps semantic search a no-op and the
rest of the pipeline continues to use keyword + entity recall only.

Model override (both backends):

    OFFICE_LAYER_EMBED_MODEL=intfloat/multilingual-e5-small
    OFFICE_LAYER_EMBEDDER=fastembed|st|null
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

DEFAULT_MODEL = "intfloat/multilingual-e5-small"


@runtime_checkable
class Embedder(Protocol):
    name: str

    def is_available(self) -> bool: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class NullEmbedder:
    """Fallback when no embedder backend is installed."""

    name = "null"

    def is_available(self) -> bool:
        return False

    @property
    def dim(self) -> int:
        return 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []


class FastembedEmbedder:
    """fastembed (onnxruntime) backend.

    Lightweight relative to PyTorch (~200MB vs ~2GB). First call downloads
    the model into ``~/.cache/fastembed/``. The download is *not* triggered
    until :meth:`embed` runs, so calling ``is_available()`` does not hit the
    network — matters for ``office-layer status``.
    """

    name = "fastembed"

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or os.environ.get(
            "OFFICE_LAYER_EMBED_MODEL", DEFAULT_MODEL
        )
        self._model = None
        self._dim: int | None = None

    def is_available(self) -> bool:
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._ensure_loaded()
        return self._dim or 0

    def _ensure_loaded(self) -> None:
        from fastembed import TextEmbedding  # type: ignore

        self._model = TextEmbedding(model_name=self._model_name)
        # Probe dim with a cheap sample. fastembed yields generators.
        sample = next(iter(self._model.embed(["x"])))
        self._dim = len(list(sample))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            self._ensure_loaded()
        return [list(v) for v in self._model.embed(texts)]  # type: ignore[union-attr]


class SentenceTransformersEmbedder:
    """sentence-transformers backend — heavier (~2GB w/ PyTorch) but most
    flexible. Picked only when fastembed is unavailable or explicitly forced.
    """

    name = "sentence-transformers"

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or os.environ.get(
            "OFFICE_LAYER_EMBED_MODEL", DEFAULT_MODEL
        )
        self._model = None

    def is_available(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def dim(self) -> int:
        if self._model is None:
            self._ensure_loaded()
        return int(self._model.get_sentence_embedding_dimension())  # type: ignore[union-attr]

    def _ensure_loaded(self) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            self._ensure_loaded()
        arr = self._model.encode(  # type: ignore[union-attr]
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in arr]


def select_embedder() -> Embedder:
    """Pick the best-available embedder.

    Env override: ``OFFICE_LAYER_EMBEDDER=fastembed|st|sentence-transformers|null``.
    """
    forced = os.environ.get("OFFICE_LAYER_EMBEDDER", "").lower().strip()
    if forced == "null":
        return NullEmbedder()
    candidates: list[type] = []
    if forced == "fastembed":
        candidates = [FastembedEmbedder]
    elif forced in ("st", "sentence-transformers"):
        candidates = [SentenceTransformersEmbedder]
    else:
        candidates = [FastembedEmbedder, SentenceTransformersEmbedder]
    for cls in candidates:
        inst = cls()
        if inst.is_available():
            return inst
    return NullEmbedder()
