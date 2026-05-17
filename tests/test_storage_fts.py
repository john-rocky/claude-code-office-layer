"""Storage smoke test: workspace + documents + chunks + FTS5 round-trip."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.models import (  # noqa: E402
    ChunkKind,
    Document,
    DocumentChunk,
    DocumentKind,
    ExtractionMethod,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402


def make_workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        name="t",
        root_path=str(tmp_path),
        policy=WorkspacePolicy.READ_ONLY,
    )


def test_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "office_layer.sqlite"
    storage = Storage(db)
    try:
        ws = make_workspace(tmp_path)
        storage.upsert_workspace(ws)
        loaded = storage.get_workspace(ws.id)
        assert loaded is not None
        assert loaded.name == "t"

        doc = Document(
            id=Document.make_id(ws.id, str(tmp_path / "x.md")),
            workspace_id=ws.id,
            path=str(tmp_path / "x.md"),
            name="x.md",
            kind=DocumentKind.MARKDOWN,
            size_bytes=10,
            mtime=datetime.now(timezone.utc),
        )
        storage.upsert_document(doc)
        c0 = DocumentChunk(
            id=DocumentChunk.make_id(doc.id, 0),
            document_id=doc.id,
            workspace_id=ws.id,
            kind=ChunkKind.PARAGRAPH,
            ordinal=0,
            text="hello world ACME invoice 請求書 2025",
            char_count=40,
            extraction_method=ExtractionMethod.NATIVE_TEXT,
        )
        storage.replace_chunks(doc.id, [c0])

        # Keyword that exists in both English and Japanese text.
        results = storage.fts_search('"ACME"')
        assert len(results) == 1
        chunk, fetched_doc, score = results[0]
        assert chunk.id == c0.id
        assert fetched_doc.id == doc.id

        # Japanese keyword.
        results_jp = storage.fts_search('"請求書"')
        assert len(results_jp) == 1
    finally:
        storage.close()
