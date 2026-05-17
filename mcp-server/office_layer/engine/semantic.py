"""Semantic index — glues an Embedder to Storage's sqlite-vec table.

Off by default. Activates only when a workspace has
``enable_vector_search=True`` AND the embedder backend AND ``sqlite-vec`` are
both installed. Failure of any prerequisite degrades gracefully to a no-op;
the hybrid ranker keeps producing keyword + entity hits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import DocumentChunk
from ..storage import Storage
from .embedder import Embedder, NullEmbedder

log = logging.getLogger(__name__)


@dataclass
class SemanticHit:
    chunk_id: str
    distance: float


class SemanticIndex:
    """Embedder + Storage vector ops, with lazy initialization.

    ``is_available()`` returns True only after the embedder model has been
    probed for its dimension and ``vec_chunks`` has been created in SQLite.
    The first call to :meth:`index_chunks` triggers that, so cheap-PC users
    who never enable vector search never pay the model load.
    """

    def __init__(self, storage: Storage, embedder: Embedder):
        self.storage = storage
        self.embedder = embedder
        self._init_tried = False
        self._init_ok = False

    @property
    def name(self) -> str:
        return self.embedder.name if self.embedder.is_available() else "disabled"

    def is_available(self) -> bool:
        return self._ensure_init()

    def _ensure_init(self) -> bool:
        if self._init_tried:
            return self._init_ok
        self._init_tried = True
        if not self.embedder.is_available():
            return False
        try:
            dim = self.embedder.dim
        except Exception:  # model load / network errors must not crash search
            log.exception("embedder dim probe failed; semantic search disabled")
            return False
        if not dim:
            return False
        ok = self.storage.enable_vector_index(dim)
        self._init_ok = bool(ok)
        if not ok:
            log.info("sqlite-vec unavailable; semantic search disabled")
        return self._init_ok

    def index_chunks(self, chunks: list[DocumentChunk]) -> int:
        """Embed + upsert each chunk's text. Returns the number of vectors written."""
        if not chunks or not self._ensure_init():
            return 0
        # Skip empty / whitespace-only chunks — embedding them is wasted work.
        keep = [c for c in chunks if c.text and c.text.strip()]
        if not keep:
            return 0
        try:
            embs = self.embedder.embed([c.text for c in keep])
        except Exception:
            log.exception("embed batch failed; skipping vectors for this batch")
            return 0
        if len(embs) != len(keep):
            log.warning("embedder returned %d vectors for %d texts", len(embs), len(keep))
            return 0
        with self.storage.tx():
            for c, e in zip(keep, embs):
                self.storage.vector_upsert(c.id, c.workspace_id, c.document_id, e)
        return len(keep)

    def remove_document(self, document_id: str) -> None:
        if not self._ensure_init():
            return
        self.storage.vector_delete_document(document_id)

    def query(
        self,
        text: str,
        *,
        workspace_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[SemanticHit]:
        if not text or not text.strip() or not self._ensure_init():
            return []
        try:
            embs = self.embedder.embed([text])
        except Exception:
            log.exception("query embed failed; skipping semantic hits")
            return []
        if not embs:
            return []
        rows = self.storage.vector_query(embs[0], workspace_ids=workspace_ids, limit=limit)
        return [SemanticHit(chunk_id=cid, distance=dist) for cid, dist in rows]


def null_semantic_index(storage: Storage) -> SemanticIndex:
    """Convenience constructor for tests / degraded mode."""
    return SemanticIndex(storage, NullEmbedder())
