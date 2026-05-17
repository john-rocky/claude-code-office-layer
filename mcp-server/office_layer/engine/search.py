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
from .query_understanding import parse as parse_query

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

    Strategy:
    - Split on non-word characters (CJK ranges preserved).
    - For each token: emit a quoted form for exact safety + a prefix form
      so '請求' will hit '請求書' (FTS5 unicode61 has no CJK segmentation
      — without prefix, partial words never match).
    - Drop tokens shorter than 2 chars to keep noise out.
    """
    if not text:
        return ""
    tokens = [t for t in re.split(r"[^\w぀-ヿ一-鿿]+", text) if len(t) >= 2]
    if not tokens:
        return ""
    parts: list[str] = []
    for t in tokens:
        safe = t.replace(chr(34), "")
        parts.append(f'"{safe}"')
        # Prefix variant — only if the token is alphanumeric or CJK
        # (FTS5 prefix syntax: "term"* )
        parts.append(f'"{safe}"*')
    return " OR ".join(parts)


class HybridSearcher:
    def __init__(self, storage: Storage, registry: AdapterRegistry):
        self.storage = storage
        self.registry = registry

    def search(self, query: SearchQuery) -> SearchResponse:
        started = time.monotonic()

        # Query understanding: enrich with kind + period hints unless the
        # caller already pinned them.
        enriched = parse_query(query.text)
        query = enriched.apply_to(query)

        match_expr = _sanitize_fts(query.text)
        candidates: list[tuple[DocumentChunk, Document, float]] = []
        if match_expr:
            candidates = self.storage.fts_search(
                match_expr,
                workspace_ids=query.workspace_ids,
                kinds=query.kinds,
                limit=max(query.limit * 4, 40),
            )

        # Entity-driven recall: catch documents whose chunks did not match
        # the FTS expression but where an indexed entity (company / email /
        # domain) does match a query token.
        entity_doc_ids = self._entity_doc_ids(query)
        if entity_doc_ids:
            seen = {doc.id for _, doc, _ in candidates}
            for doc_id in entity_doc_ids:
                if doc_id in seen:
                    continue
                doc = self.storage.get_document(doc_id)
                if doc is None:
                    continue
                chunks = self.storage.get_chunks(doc_id)
                chunk = chunks[0] if chunks else _dummy_chunk(doc)
                candidates.append((chunk, doc, 0.5))  # mid-range default
                seen.add(doc_id)

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

        results = self._rank(query, candidates, enriched_notes=enriched.notes)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return SearchResponse(
            query=query,
            results=results[: query.limit],
            total=len(results),
            elapsed_ms=elapsed_ms,
        )

    def _entity_doc_ids(self, query: SearchQuery) -> list[str]:
        """Documents whose entities include any non-trivial query token."""
        terms = [t for t in re.split(r"\s+", query.text) if len(t) >= 3]
        if not terms:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for t in terms[:5]:
            ids = self.storage.find_documents_with_entity(
                t, workspace_ids=query.workspace_ids, limit=20
            )
            for d in ids:
                if d not in seen:
                    seen.add(d)
                    out.append(d)
        return out

    # -- ranking --------------------------------------------------------------

    def _rank(
        self,
        query: SearchQuery,
        candidates: Iterable[tuple[DocumentChunk, Document, float]],
        *,
        enriched_notes: list[str] | None = None,
    ) -> list[SearchResult]:
        kind_pref = self._infer_kind_pref(query.text)
        terms = [t for t in re.split(r"\s+", query.text.lower()) if t]
        now = datetime.now(timezone.utc)
        ranked: list[SearchResult] = []
        date_from = query.date_from
        date_to = query.date_to
        for chunk, doc, raw_score in candidates:
            # Date filter: skip documents outside the inferred window.
            doc_mtime = doc.mtime if doc.mtime.tzinfo else doc.mtime.replace(tzinfo=timezone.utc)
            if date_from and doc_mtime < date_from:
                continue
            if date_to and doc_mtime > date_to:
                continue

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
                reasons.append(f"kind == {kind_pref.value if hasattr(kind_pref, 'value') else kind_pref}")

            # Entity boost: documents whose entities include any query term
            # get a small bump (the entity-recall step pulled them in, but
            # only with a default score — confirm the boost here).
            for t in terms:
                if len(t) < 3:
                    continue
                entity_hits = self.storage.find_documents_with_entity(
                    t, workspace_ids=query.workspace_ids, limit=5
                )
                if doc.id in entity_hits:
                    score += 2.0
                    reasons.append(f"entity ~ '{t}'")
                    break  # one boost per result is enough

            # Recency: 365 days → linear up to +2
            age_days = max(0.0, (now - doc_mtime).total_seconds() / 86400.0)
            recency_boost = max(0.0, 2.0 * (1 - min(age_days, 365.0) / 365.0))
            score += recency_boost
            if recency_boost > 0.5:
                reasons.append("recent")

            # Date-range match earns its own reason line so the user can see
            # the query understanding kicked in.
            if (date_from or date_to) and (enriched_notes):
                for n in enriched_notes:
                    if n.startswith("period:"):
                        reasons.append(n)
                        break

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
