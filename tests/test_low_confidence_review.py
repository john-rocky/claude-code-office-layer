"""Tests for the Phase 3 low-confidence review workflow.

Covers:

* Pure-function tests over :func:`_group_by_document` — grouping +
  stable ordering, missing-document tolerance.
* Integration tests against a real Storage with seeded
  ``extracted_fields`` rows — threshold gating, ``section.*``
  exclusion, ``truncated`` flag, empty/missing workspace, multi-doc
  grouping.
* CLI smoke for ``office-layer review low-confidence`` (JSON path).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from click.testing import CliRunner  # noqa: E402

from office_layer.adapters import get_registry, registry as adapter_registry  # noqa: E402
from office_layer.cli import main as cli_main  # noqa: E402
import office_layer.engine.engine as engine_module  # noqa: E402
from office_layer.engine.embedder import NullEmbedder  # noqa: E402
from office_layer.engine.engine import Engine  # noqa: E402
from office_layer.models import (  # noqa: E402
    ChunkKind,
    Document,
    DocumentChunk,
    DocumentKind,
    ExtractedField,
    ExtractionMethod,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402
from office_layer.workflows.low_confidence_review import (  # noqa: E402
    LowConfidenceGroup,
    LowConfidenceItem,
    _group_by_document,
    create_low_confidence_review,
)


# -- pure grouping ------------------------------------------------------------


def _doc(doc_id: str, path: str) -> Document:
    return Document(
        id=doc_id,
        workspace_id="ws_test",
        path=path,
        name=Path(path).name,
        kind=DocumentKind.MARKDOWN,
        size_bytes=10,
        mtime=datetime.now(timezone.utc),
        has_extracted_text=True,
    )


def _field(doc_id: str, key: str, value: str, conf: float) -> ExtractedField:
    return ExtractedField(
        document_id=doc_id, key=key, value=value, confidence=conf
    )


def test_group_by_document_sorts_by_file_path() -> None:
    docs = {
        "doc_b": _doc("doc_b", "/ws/b.md"),
        "doc_a": _doc("doc_a", "/ws/a.md"),
    }
    fields = [
        _field("doc_b", "total", "$10", 0.5),
        _field("doc_a", "total", "$20", 0.4),
    ]
    groups = _group_by_document(fields, docs)
    assert [g.file_path for g in groups] == ["/ws/a.md", "/ws/b.md"]


def test_group_by_document_skips_orphan_fields() -> None:
    docs = {"doc_a": _doc("doc_a", "/ws/a.md")}
    fields = [
        _field("doc_a", "total", "$10", 0.4),
        _field("doc_gone", "total", "$20", 0.4),
    ]
    groups = _group_by_document(fields, docs)
    assert len(groups) == 1
    assert groups[0].document_id == "doc_a"


def test_low_confidence_item_to_dict_omits_null_locator() -> None:
    item = LowConfidenceItem(
        key="total", value="$1", confidence=0.4, value_type="amount"
    )
    d = item.to_dict()
    assert "page_number" not in d
    assert "cell_range" not in d
    assert d["confidence"] == 0.4


def test_low_confidence_item_to_dict_keeps_page_when_present() -> None:
    item = LowConfidenceItem(
        key="total", value="$1", confidence=0.4, value_type="amount", page_number=3
    )
    assert item.to_dict()["page_number"] == 3


# -- integration fixture ------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Real Storage + Engine, installed as the get_engine() singleton."""
    adapter_registry.reset_registry()
    storage = Storage(tmp_path / "office.sqlite")
    eng = Engine(storage=storage, registry=get_registry(), embedder=NullEmbedder())
    monkeypatch.setattr(engine_module, "_engine", eng)
    yield eng
    eng.close()
    adapter_registry.reset_registry()


def _seed_doc(
    storage: Storage,
    ws: Workspace,
    *,
    name: str,
    rel_path: str,
    fields: list[tuple[str, str, str, float]],
) -> Document:
    path = str(Path(ws.root_path) / rel_path)
    doc = Document(
        id=Document.make_id(ws.id, path),
        workspace_id=ws.id,
        path=path,
        name=name,
        kind=DocumentKind.MARKDOWN,
        size_bytes=10,
        mtime=datetime.now(timezone.utc),
        has_extracted_text=True,
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    storage.upsert_document(doc)
    chunk = DocumentChunk(
        id=DocumentChunk.make_id(doc.id, 0),
        document_id=doc.id,
        workspace_id=ws.id,
        kind=ChunkKind.PARAGRAPH,
        ordinal=0,
        text="seed",
        char_count=4,
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    storage.replace_chunks(doc.id, [chunk])
    storage.replace_extracted_fields(
        doc.id,
        [
            ExtractedField(
                document_id=doc.id,
                chunk_id=chunk.id,
                key=k,
                value=v,
                value_type=vt,  # type: ignore[arg-type]
                confidence=conf,
            )
            for k, v, vt, conf in fields
        ],
    )
    return doc


# -- integration --------------------------------------------------------------


def test_missing_workspace_is_error(isolated_engine) -> None:
    out = create_low_confidence_review("ws_does_not_exist")
    assert out == {"error": "workspace 'ws_does_not_exist' not found"}


def test_empty_workspace_returns_zero_counts(isolated_engine, tmp_path) -> None:
    ws = isolated_engine.workspaces.add(
        name="empty", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    out = create_low_confidence_review(ws.id)
    assert out["document_count"] == 0
    assert out["item_count"] == 0
    assert out["groups"] == []
    assert out["truncated"] is False
    assert out["threshold"] == 0.7


def test_threshold_gates_fields(isolated_engine, tmp_path) -> None:
    """Fields with confidence >= threshold must not appear."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="inv.md",
        rel_path="invoices/inv.md",
        fields=[
            ("invoice_number", "INV-2026-001", "id", 0.95),  # above
            ("total", "$7,403.50", "amount", 0.4),  # below
            ("due_date", "2026-02-15", "date", 0.65),  # below
        ],
    )
    out = create_low_confidence_review(ws.id)
    assert out["item_count"] == 2
    keys = {it["key"] for it in out["groups"][0]["items"]}
    assert keys == {"total", "due_date"}
    assert "invoice_number" not in keys


def test_section_keys_are_excluded(isolated_engine, tmp_path) -> None:
    """section.* keys come from the contract sectioner and must not surface."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="nda.md",
        rel_path="contracts/nda.md",
        fields=[
            ("section.1.title", "Confidentiality", "text", 0.2),
            ("section.1.body", "...", "text", 0.2),
            ("total", "$1", "amount", 0.2),
        ],
    )
    out = create_low_confidence_review(ws.id)
    assert out["item_count"] == 1
    assert out["groups"][0]["items"][0]["key"] == "total"


def test_multi_doc_groups_sorted_by_path(isolated_engine, tmp_path) -> None:
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="b.md",
        rel_path="z/b.md",
        fields=[("total", "$2", "amount", 0.5)],
    )
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="a.md",
        rel_path="a/a.md",
        fields=[("total", "$1", "amount", 0.5)],
    )
    out = create_low_confidence_review(ws.id)
    assert out["document_count"] == 2
    paths = [g["file_path"] for g in out["groups"]]
    assert paths == sorted(paths)


def test_truncated_flag_when_limit_exceeded(isolated_engine, tmp_path) -> None:
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="inv.md",
        rel_path="inv.md",
        fields=[(f"f{i}", str(i), "text", 0.3) for i in range(5)],
    )
    out = create_low_confidence_review(ws.id, limit=3)
    assert out["truncated"] is True
    assert out["item_count"] == 3


def test_threshold_validation_rejects_zero_and_above_one() -> None:
    assert "error" in create_low_confidence_review("ws_x", threshold=0.0)
    assert "error" in create_low_confidence_review("ws_x", threshold=1.5)


def test_limit_validation_rejects_zero() -> None:
    assert "error" in create_low_confidence_review("ws_x", limit=0)


def test_workspace_isolation(isolated_engine, tmp_path) -> None:
    """Low-confidence fields in workspace A must not leak into workspace B."""
    ws_a = isolated_engine.workspaces.add(
        name="a", root_path=str(tmp_path / "a"), policy=WorkspacePolicy.DRAFT_WRITE
    )
    ws_b = isolated_engine.workspaces.add(
        name="b", root_path=str(tmp_path / "b"), policy=WorkspacePolicy.DRAFT_WRITE
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _seed_doc(
        isolated_engine.storage,
        ws_a,
        name="inv.md",
        rel_path="inv.md",
        fields=[("total", "$1", "amount", 0.2)],
    )
    _seed_doc(
        isolated_engine.storage,
        ws_b,
        name="other.md",
        rel_path="other.md",
        fields=[("total", "$99", "amount", 0.2)],
    )
    out_a = create_low_confidence_review(ws_a.id)
    out_b = create_low_confidence_review(ws_b.id)
    assert out_a["item_count"] == 1
    assert out_b["item_count"] == 1
    assert out_a["groups"][0]["items"][0]["value"] == "$1"
    assert out_b["groups"][0]["items"][0]["value"] == "$99"


# -- CLI ----------------------------------------------------------------------


def test_cli_review_low_confidence_json_output(isolated_engine, tmp_path) -> None:
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="inv.md",
        rel_path="inv.md",
        fields=[("total", "$1", "amount", 0.3)],
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["review", "low-confidence", ws.id, "--json-output"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["item_count"] == 1
    assert payload["groups"][0]["items"][0]["key"] == "total"


def test_cli_review_low_confidence_missing_workspace_exits_1(isolated_engine) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["review", "low-confidence", "ws_missing"]
    )
    assert result.exit_code == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
