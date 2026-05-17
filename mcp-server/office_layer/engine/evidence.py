"""Evidence Packet builder — §9.5 + §17.7 independent value.

Takes a SearchResponse (or a manually curated list) and produces an
EvidencePacket: a minimal, grounded, source-attributed work context for
Claude Code. The shape is stable across Phases — adapters/extractors can
change without changing how Claude consumes evidence.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..models import (
    DocumentChunk,
    DocumentKind,
    EvidencePacket,
    EvidenceSource,
    ExtractedField,
    ExtractedTable,
    SearchQuery,
    SearchResponse,
    SearchResult,
    Workspace,
)
from ..storage import Storage

log = logging.getLogger(__name__)


MAX_SOURCES_DEFAULT = 8
MAX_TEXT_CHARS_PER_SOURCE = 4000


class EvidencePacketBuilder:
    def __init__(self, storage: Storage):
        self.storage = storage

    def build(
        self,
        intent: str,
        *,
        results: list[SearchResult] | SearchResponse,
        query: SearchQuery | None = None,
        max_sources: int = MAX_SOURCES_DEFAULT,
        max_text_chars: int = MAX_TEXT_CHARS_PER_SOURCE,
    ) -> EvidencePacket:
        if isinstance(results, SearchResponse):
            response = results
            results_list = response.results
            query = query or response.query
        else:
            results_list = results

        workspaces_seen: set[str] = set()
        sources: list[EvidenceSource] = []
        low_confidence: list[str] = []
        truncated = False

        for r in results_list[:max_sources]:
            doc = r.document
            chunk = r.chunk
            workspace = self.storage.get_workspace(doc.workspace_id)
            workspace_name = workspace.name if workspace else doc.workspace_id
            workspaces_seen.add(doc.workspace_id)

            extracted_text = ""
            page_number = sheet_name = cell_range = slide_number = None
            extraction_method = doc.extraction_method
            confidence = 1.0

            if chunk is not None:
                extracted_text = chunk.text
                page_number = chunk.page_number
                sheet_name = chunk.sheet_name
                cell_range = chunk.cell_range
                slide_number = chunk.slide_number
                extraction_method = chunk.extraction_method or extraction_method
                confidence = chunk.confidence
            else:
                # No chunk → filename-only match. Pull first available chunk if any.
                chunks = self.storage.get_chunks(doc.id)
                if chunks:
                    c0 = chunks[0]
                    extracted_text = c0.text
                    page_number = c0.page_number
                    sheet_name = c0.sheet_name
                    cell_range = c0.cell_range
                    slide_number = c0.slide_number
                    extraction_method = c0.extraction_method or extraction_method
                    confidence = c0.confidence

            if len(extracted_text) > max_text_chars:
                extracted_text = extracted_text[:max_text_chars] + "..."
                truncated = True

            # Pull tables only for highly-relevant top picks (tables can be large).
            tables: list[ExtractedTable] = []
            fields: list[ExtractedField] = self.storage.get_fields(doc.id)[:8]

            if confidence < 0.6:
                low_confidence.append(
                    f"{doc.name} ({extraction_method}) confidence={confidence:.2f}"
                )

            sources.append(
                EvidenceSource(
                    source_id=chunk.id if chunk else f"{doc.id}:doc",
                    file_path=doc.path,
                    file_name=doc.name,
                    file_type=doc.kind,
                    workspace_name=workspace_name,
                    page_number=page_number,
                    sheet_name=sheet_name,
                    cell_range=cell_range,
                    slide_number=slide_number,
                    extracted_text=extracted_text,
                    extracted_tables=tables,
                    extracted_fields=fields,
                    relevance_score=r.score,
                    confidence_score=confidence,
                    extraction_method=extraction_method,
                    reason_for_inclusion=r.reason or "search result",
                    suggested_next_action=self._suggest_action(doc.kind, intent),
                )
            )

        packet = EvidencePacket(
            intent=intent,
            query=query,
            sources=sources,
            workspace_ids=sorted(workspaces_seen),
            low_confidence_items=low_confidence,
            truncated=truncated,
        )
        return packet

    @staticmethod
    def _suggest_action(kind: DocumentKind, intent: str) -> str | None:
        intent_l = intent.lower()
        if "請求" in intent or "invoice" in intent_l:
            return "extract invoice fields and add to CSV"
        if "契約" in intent or "contract" in intent_l:
            return "extract clauses and compare against the other contract version"
        if "メール" in intent or "email" in intent_l or "返信" in intent:
            return "summarise past correspondence; draft a reply that stays in scope"
        if kind == DocumentKind.XLSX:
            return "review sheet ranges; cite by sheet + cell range when quoting"
        if kind == DocumentKind.PDF:
            return "cite by page number when quoting"
        return None
