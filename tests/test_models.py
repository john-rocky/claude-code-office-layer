"""Smoke tests for the domain models — no external deps required."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the source layout importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.models import (  # noqa: E402
    Document,
    DocumentChunk,
    DocumentKind,
    EvidencePacket,
    EvidenceSource,
    OperationRiskLevel,
    SearchQuery,
    Workspace,
    WorkspacePolicy,
)


def test_workspace_defaults_read_only(tmp_path: Path) -> None:
    ws = Workspace(name="docs", root_path=str(tmp_path))
    assert ws.policy == WorkspacePolicy.READ_ONLY
    assert ws.pii_warning is True
    assert ws.enable_ocr is False


def test_document_make_id_is_stable() -> None:
    a = Document.make_id("ws_x", "/foo/bar.pdf")
    b = Document.make_id("ws_x", "/foo/bar.pdf")
    assert a == b
    assert a != Document.make_id("ws_y", "/foo/bar.pdf")


def test_chunk_make_id_is_ordered() -> None:
    doc_id = Document.make_id("ws_x", "/foo.pdf")
    a = DocumentChunk.make_id(doc_id, 0)
    b = DocumentChunk.make_id(doc_id, 1)
    assert a != b
    assert a < b


def test_evidence_packet_summary_markdown() -> None:
    packet = EvidencePacket(
        intent="draft followup",
        sources=[
            EvidenceSource(
                source_id="chk_1",
                file_path="/x/INV-2025-03.pdf",
                file_name="INV-2025-03.pdf",
                file_type=DocumentKind.PDF,
                workspace_name="docs",
                page_number=1,
                extracted_text="¥748,000 支払期限 2025/04/30",
                relevance_score=8.4,
                confidence_score=1.0,
                reason_for_inclusion="filename ~ 'INV-2025-03'",
            )
        ],
    )
    md = packet.summary_markdown()
    assert "Evidence Packet" in md
    assert "INV-2025-03.pdf" in md
    assert "p.1" in md
    assert "¥748,000" in md


def test_search_query_defaults() -> None:
    q = SearchQuery(text="hello")
    assert q.limit == 20
    assert q.include_chunks is True
    assert q.kinds is None
