"""Hybrid ranker — §17.7 independent value.

Combines:
- FTS5 BM25 score (lower-is-better → flip sign)
- filename match boost
- path match boost
- kind preference boost
- date proximity boost
- entity match boost (when entities are wired up, Phase 1+)
- recency boost (mtime within 365d)

The ranker is intentionally explicit and tunable — sub-scores live in the
``SearchResult.reason`` field so Claude can explain why a result was chosen.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Iterable

from ..adapters import AdapterRegistry
from ..models import (
    Document,
    DocumentChunk,
    DocumentKind,
    SearchMode,
    SearchQuery,
    SearchResponse,
    SearchResult,
)
from ..storage import Storage

log = logging.getLogger(__name__)


# Query terms that imply a specific DocumentKind preference.
KIND_HINTS: list[tuple[re.Pattern, DocumentKind]] = [
    (re.compile(r"請求書|invoice|請求|billing", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"領収書|receipt", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"見積|estimate|quote|quotation", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"契約書?|contract|agreement|NDA", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"スプレッドシート|spreadsheet|excel|エクセル", re.IGNORECASE), DocumentKind.XLSX),
    (re.compile(r"プレゼン|スライド|slide|powerpoint", re.IGNORECASE), DocumentKind.PPTX),
    (re.compile(r"議事録|meeting notes|minutes", re.IGNORECASE), DocumentKind.DOCX),
]


def _sanitize_fts(text: str) -> str:
    """Make raw user text safe for FTS5 MATCH.

    FTS5 treats unquoted bareword as prefix match if followed by '*'. To stay
    forgiving for natural-language queries (and avoid syntax errors on
    punctuation), we tokenize on non-word characters and OR the terms.
    """
    if not text:
        return ""
    tokens = [t for t in re.split(r"[^\w぀-ヿ一-鿿]+", text) if t]
    if not tokens:
        return ""
    quoted = [f'"{t.replace(chr(34), "")}"' for t in tokens]
    return " OR ".join(quoted)


class HybridSearcher:
    def __init__(self, storage: Storage, registry: AdapterRegistry):
        self.storage = storage
        self.registry = registry

    def search(self, query: SearchQuery) -> SearchResponse:
        started = time.monotonic()
        match_expr = _sanitize_fts(query.text)
        candidates: list[tuple[DocumentChunk, Document, float]] = []
        if match_expr:
            candidates = self.storage.fts_search(
                match_expr,
                workspace_ids=query.workspace_ids,
                kinds=query.kinds,
                limit=max(query.limit * 4, 40),
            )

        # Filename-only fallback: small workspaces with no extracted text yet.
        if not candidates and query.text:
            docs = self.storage.list_documents(
                workspace_id=(query.workspace_ids[0] if query.workspace_ids else None),
                limit=500,
            )
            terms = [t.lower() for t in re.split(r"\s+", query.text) if t]
            for d in docs:
                if any(t in d.name.lower() for t in terms):
                    candidates.append((_dummy_chunk(d), d, 999.0))

        results = self._rank(query, candidates)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return SearchResponse(
            query=query,
            results=results[: query.limit],
            total=len(results),
            elapsed_ms=elapsed_ms,
        )

    # -- ranking --------------------------------------------------------------

    def _rank(
        self,
        query: SearchQuery,
        candidates: Iterable[tuple[DocumentChunk, Document, float]],
    ) -> list[SearchResult]:
        kind_pref = self._infer_kind_pref(query.text)
        terms = [t for t in re.split(r"\s+", query.text.lower()) if t]
        now = datetime.now(timezone.utc)
        ranked: list[SearchResult] = []
        for chunk, doc, raw_score in candidates:
            score = -raw_score  # FTS bm25 is lower-is-better; flip
            reasons = []
            matched: list[str] = []

            for t in terms:
                if t in chunk.text.lower():
                    matched.append(t)
            for t in terms:
                if t in doc.name.lower():
                    score += 5.0
                    reasons.append(f"filename ~ '{t}'")
                if t in doc.path.lower() and t not in doc.name.lower():
                    score += 1.0
            if kind_pref and doc.kind == kind_pref:
                score += 3.0
                reasons.append(f"kind == {kind_pref}")
            # Recency: 365 days → linear up to +2
            age_days = max(0.0, (now - doc.mtime).total_seconds() / 86400.0)
            recency_boost = max(0.0, 2.0 * (1 - min(age_days, 365.0) / 365.0))
            score += recency_boost
            if recency_boost > 0.5:
                reasons.append("recent")

            ranked.append(
                SearchResult(
                    document=doc,
                    chunk=chunk if chunk.id != "__placeholder__" else None,
                    score=score,
                    reason="; ".join(reasons) if reasons else "fts match",
                    matched_terms=matched,
                )
            )

        ranked.sort(key=lambda r: r.score, reverse=True)
        # Dedup: keep only highest-scoring chunk per document for top-level
        # surface; callers can drill into chunks via get_chunks() later.
        seen_docs: set[str] = set()
        deduped: list[SearchResult] = []
        for r in ranked:
            if r.document.id in seen_docs:
                continue
            seen_docs.add(r.document.id)
            deduped.append(r)
        return deduped

    @staticmethod
    def _infer_kind_pref(text: str) -> DocumentKind | None:
        for pat, kind in KIND_HINTS:
            if pat.search(text):
                return kind
        return None


def _dummy_chunk(doc: Document) -> DocumentChunk:
    """Synthetic placeholder when we matched on filename only."""
    from ..models import ChunkKind

    return DocumentChunk(
        id="__placeholder__",
        document_id=doc.id,
        workspace_id=doc.workspace_id,
        kind=ChunkKind.PARAGRAPH,
        ordinal=0,
        text=doc.name,
        char_count=len(doc.name),
        confidence=0.5,
    )
