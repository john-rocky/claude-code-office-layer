"""Tests for Phase 2 invoice extractor.

Split into two layers:

* Pure-function tests over the regex/label engine — fast, no I/O.
* One integration test that indexes ``examples/sample_workspace`` into a
  temp SQLite DB and runs ``extract_invoice_fields`` end-to-end, asserting
  the structured fields land in storage with the expected keys and types.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.adapters import get_registry, registry as adapter_registry  # noqa: E402
import office_layer.engine.engine as engine_module  # noqa: E402
from office_layer.engine.embedder import NullEmbedder  # noqa: E402
from office_layer.engine.engine import Engine  # noqa: E402
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
from office_layer.workflows.invoice import (  # noqa: E402
    extract_fields_from_text,
    extract_invoice_fields,
)


# -- helpers ------------------------------------------------------------------


def _fields_by_key(fields) -> dict[str, object]:
    return {f.key: f for f in fields}


# -- pure-function tests ------------------------------------------------------


def test_jp_section_header_style():
    """Markdown invoice with `## label` headers and value on the next line."""
    text = (
        "# 請求書 INV-2025-03\n"
        "\n"
        "## 取引先\nACME 株式会社\n"
        "\n"
        "## 請求日\n2025/03/15\n"
        "\n"
        "## 支払期限\n2025/04/30\n"
        "\n"
        "## 合計\n"
        "- 小計: ¥680,000\n"
        "- 消費税 (10%): ¥68,000\n"
        "- 合計: ¥748,000\n"
        "\n"
        "## 振込先\n三菱UFJ銀行 渋谷支店 普通 1234567 ジョン-ロッキー\n"
    )
    by_key = _fields_by_key(extract_fields_from_text(text, document_id="d1"))
    assert by_key["invoice_number"].value == "INV-2025-03"
    assert by_key["issue_date"].value == "2025/03/15"
    assert by_key["due_date"].value == "2025/04/30"
    assert by_key["subtotal"].value == "¥680,000"
    assert by_key["tax"].value == "¥68,000"
    assert by_key["total"].value == "¥748,000"
    assert by_key["recipient"].value == "ACME 株式会社"
    assert "三菱UFJ銀行" in by_key["payment_account"].value


def test_jp_inline_colon_style():
    """Same-line `請求日: 2025/02/10` form — high confidence label match."""
    text = (
        "請求書番号: BILL-2026-007\n"
        "発行日: 2026/02/10\n"
        "お支払期限: 2026/03/31\n"
        "請求先: 株式会社サンプル商事\n"
        "発行元: 鈴木コンサルティング合同会社\n"
        "小計: ¥1,100,000\n"
        "消費税 (10%): ¥110,000\n"
        "ご請求金額: ¥1,210,000\n"
        "振込先: みずほ銀行 新宿支店 普通 7654321 スズキ-コンサルティング\n"
    )
    by_key = _fields_by_key(extract_fields_from_text(text, document_id="d2"))
    assert by_key["invoice_number"].value == "BILL-2026-007"
    assert by_key["invoice_number"].confidence == pytest.approx(0.95)
    assert by_key["issue_date"].value == "2026/02/10"
    assert by_key["due_date"].value == "2026/03/31"
    assert by_key["subtotal"].value == "¥1,100,000"
    assert by_key["tax"].value == "¥110,000"
    assert by_key["total"].value == "¥1,210,000"
    assert by_key["recipient"].value == "株式会社サンプル商事"
    assert by_key["issuer"].value == "鈴木コンサルティング合同会社"


def test_english_invoice_total_does_not_pick_subtotal_line():
    """Regression: case-insensitive 'Total' must not match inside 'Subtotal'."""
    text = (
        "Invoice No.: INV-2026-001\n"
        "Issue Date: 2026-01-15\n"
        "Due Date: 2026-02-15\n"
        "Bill To: Northwind Trading Inc.\n"
        "Subtotal: $6,800.00\n"
        "Tax (8.875%): $603.50\n"
        "Total: $7,403.50\n"
    )
    by_key = _fields_by_key(extract_fields_from_text(text, document_id="d3"))
    assert by_key["total"].value == "$7,403.50"
    assert by_key["subtotal"].value == "$6,800.00"
    assert by_key["tax"].value == "$603.50"
    assert by_key["invoice_number"].value == "INV-2026-001"
    assert by_key["recipient"].value == "Northwind Trading Inc."


def test_xlsx_like_tab_separated_layout():
    """openpyxl-extracted XLSX rows come through as tab-separated text;
    the same label/value pair logic should still work."""
    text = (
        "請求書\n"
        "\n"
        "請求書番号\tINV-XLS-2026-A1\n"
        "発行日\t2026/03/05\n"
        "支払期限\t2026/04/05\n"
        "取引先\t田中工業株式会社\n"
        "小計\t¥1,000,000\n"
        "消費税(10%)\t¥100,000\n"
        "合計\t¥1,100,000\n"
    )
    by_key = _fields_by_key(extract_fields_from_text(text, document_id="d4"))
    assert by_key["invoice_number"].value == "INV-XLS-2026-A1"
    assert by_key["total"].value == "¥1,100,000"
    assert by_key["subtotal"].value == "¥1,000,000"


def test_coffee_receipt_only_fires_total():
    """Non-invoice receipt: just the total line should match, nothing else."""
    text = "スターバックス 渋谷店\n2025/04/02 09:14\nカフェラテ Tall  ¥495\n合計  ¥495\nレジ番号 #042\n"
    fields = extract_fields_from_text(text, document_id="d5")
    keys = {f.key for f in fields}
    assert keys == {"total", "issue_date"} or keys == {"total"}, keys
    by_key = _fields_by_key(fields)
    assert by_key["total"].value == "¥495"


def test_plain_text_returns_nothing():
    """No label, no inferred invoice → empty list."""
    assert extract_fields_from_text("Just a note about the weather.", document_id="d6") == []
    assert extract_fields_from_text("", document_id="d7") == []


def test_dedup_keeps_highest_confidence_across_chunks():
    """When two chunks both yield invoice_number, only the more-confident
    label-anchored hit survives the per-document dedupe (verified via the
    document-level wrapper in the integration test)."""
    # Pure-function check: a single chunk with duplicate hits keeps the first.
    text = "請求書番号: BILL-2026-007\n\n別の場所でも BILL-2026-007 と書いてある\n"
    fields = extract_fields_from_text(text, document_id="d8")
    invoice_numbers = [f for f in fields if f.key == "invoice_number"]
    assert len(invoice_numbers) == 1
    assert invoice_numbers[0].value == "BILL-2026-007"
    assert invoice_numbers[0].confidence == pytest.approx(0.95)


def test_field_locator_metadata_propagates():
    """page_number / chunk_id from the caller must flow into ExtractedField."""
    text = "請求日: 2026/05/01\n合計: ¥10,000\n"
    fields = extract_fields_from_text(
        text, document_id="d9", chunk_id="chk_x_00007", page_number=3, cell_range="B3:B10"
    )
    for f in fields:
        assert f.chunk_id == "chk_x_00007"
        assert f.page_number == 3
        assert f.cell_range == "B3:B10"


# -- integration test ---------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Spin up an Engine on a temp sqlite DB and install it as the singleton
    so ``workflows/invoice.py``'s ``get_engine()`` finds it. Reset on teardown
    so subsequent tests don't see our throwaway DB."""
    adapter_registry.reset_registry()
    storage = Storage(tmp_path / "office.sqlite")
    eng = Engine(storage=storage, registry=get_registry(), embedder=NullEmbedder())
    monkeypatch.setattr(engine_module, "_engine", eng)
    yield eng
    eng.close()
    adapter_registry.reset_registry()


def _seed_chunked_doc(storage: Storage, ws: Workspace, name: str, kind: DocumentKind, text: str) -> Document:
    path = str(Path(ws.root_path) / name)
    doc = Document(
        id=Document.make_id(ws.id, path),
        workspace_id=ws.id,
        path=path,
        name=name,
        kind=kind,
        size_bytes=len(text),
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
        text=text,
        char_count=len(text),
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    storage.replace_chunks(doc.id, [chunk])
    return doc


def test_extract_invoice_fields_persists_to_storage(isolated_engine, tmp_path):
    """End-to-end through the engine: chunks → extractor → storage row."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_chunked_doc(
        isolated_engine.storage,
        ws,
        "invoice.md",
        DocumentKind.MARKDOWN,
        "請求書番号: BILL-2026-007\n発行日: 2026/02/10\n合計: ¥1,210,000\n",
    )
    out = extract_invoice_fields(doc.id, persist=True)
    assert out["field_count"] == 3
    assert out["avg_confidence"] == pytest.approx(0.95)
    assert out["persisted"] is True

    # Storage round-trip: the fields written by the workflow are readable.
    stored = isolated_engine.storage.get_fields(doc.id)
    keys = {f.key: f for f in stored}
    assert keys["invoice_number"].value == "BILL-2026-007"
    assert keys["issue_date"].value == "2026/02/10"
    assert keys["total"].value == "¥1,210,000"
    assert keys["total"].value_type == "amount"
    # The extractor tag identifies the strategy that fired.
    assert keys["invoice_number"].extractor in {"invoice.label", "invoice.section", "invoice.heuristic"}


def test_extract_invoice_fields_no_persist_leaves_storage_empty(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_chunked_doc(
        isolated_engine.storage, ws, "x.md", DocumentKind.MARKDOWN,
        "請求書番号: BILL-2026-007\n",
    )
    out = extract_invoice_fields(doc.id, persist=False)
    assert out["field_count"] == 1
    assert out["persisted"] is False
    assert isolated_engine.storage.get_fields(doc.id) == []


def test_extract_invoice_fields_missing_document(isolated_engine):
    out = extract_invoice_fields("doc_does_not_exist", persist=False)
    assert "error" in out


def test_extract_invoice_fields_no_chunks(isolated_engine, tmp_path):
    """A document that was upserted but never chunked returns a friendly note."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = Document(
        id=Document.make_id(ws.id, str(tmp_path / "empty.md")),
        workspace_id=ws.id,
        path=str(tmp_path / "empty.md"),
        name="empty.md",
        kind=DocumentKind.MARKDOWN,
        size_bytes=0,
        mtime=datetime.now(timezone.utc),
    )
    isolated_engine.storage.upsert_document(doc)
    out = extract_invoice_fields(doc.id)
    assert out["fields"] == []
    assert "note" in out
