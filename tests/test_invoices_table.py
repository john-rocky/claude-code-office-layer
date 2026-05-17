"""Tests for Phase 2 invoices-to-table workflow.

Split into:

* Pure-function tests over :func:`build_invoice_rows` — projects an
  already-extracted ``(Document, [ExtractedField])`` pair set into the
  CSV-shaped rows without touching storage.
* Integration tests that seed an isolated workspace + sqlite engine and
  exercise ``extract_invoices_to_table`` end-to-end through ``get_engine()``,
  including the safety gate (read-only workspace) and the write-outside-
  workspace refusal.
"""

from __future__ import annotations

import csv
import sys
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
    ExtractedField,
    ExtractionMethod,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402
from office_layer.workflows.invoices_table import (  # noqa: E402
    COLUMNS,
    build_invoice_rows,
    extract_invoices_to_table,
)


# -- pure-function tests ------------------------------------------------------


def _doc(name: str, path: str | None = None) -> Document:
    return Document(
        id=f"doc_{name}",
        workspace_id="ws_test",
        path=path or f"/ws/{name}",
        name=name,
        kind=DocumentKind.MARKDOWN,
        size_bytes=100,
        mtime=datetime.now(timezone.utc),
    )


def _field(doc: Document, key: str, value: str, *, vt: str = "text", conf: float = 0.95) -> ExtractedField:
    return ExtractedField(
        document_id=doc.id,
        key=key,
        value=value,
        value_type=vt,  # type: ignore[arg-type]
        confidence=conf,
    )


def test_build_rows_drops_docs_without_invoice_number():
    """The canonical "this is an invoice" signal is invoice_number — a
    doc with only `total` and no `invoice_number` is silently dropped."""
    doc_a = _doc("a.md")
    doc_b = _doc("b.md")
    rows = build_invoice_rows(
        [
            (doc_a, [_field(doc_a, "total", "¥748,000", vt="amount")]),
            (
                doc_b,
                [
                    _field(doc_b, "invoice_number", "INV-1", vt="id"),
                    _field(doc_b, "total", "¥748,000", vt="amount"),
                ],
            ),
        ]
    )
    assert len(rows) == 1
    assert rows[0].document_id == doc_b.id


def test_build_rows_currency_derived_from_total():
    doc = _doc("inv.md")
    rows = build_invoice_rows(
        [
            (
                doc,
                [
                    _field(doc, "invoice_number", "INV-1", vt="id"),
                    _field(doc, "total", "¥748,000", vt="amount"),
                ],
            )
        ]
    )
    assert rows[0].values["currency"] == "JPY"


def test_build_rows_currency_blank_when_total_unparseable():
    doc = _doc("inv.md")
    rows = build_invoice_rows(
        [
            (
                doc,
                [
                    _field(doc, "invoice_number", "INV-1", vt="id"),
                    _field(doc, "total", "bare 12,000", vt="amount"),
                ],
            )
        ]
    )
    assert rows[0].values["currency"] == ""


def test_build_rows_columns_in_lockstep_with_csv_header():
    """The dict keys on each row must be exactly the CSV header columns,
    so a downstream `DictWriter.writerow(row.values)` never silently drops
    or misorders a field."""
    doc = _doc("inv.md")
    rows = build_invoice_rows(
        [(doc, [_field(doc, "invoice_number", "INV-1", vt="id")])]
    )
    assert set(rows[0].values.keys()) == set(COLUMNS)


def test_build_rows_confidence_avg_only_counts_present_fields():
    """A sparse extraction (only 2 of 9 fields hit) should not be
    confidence-penalised against the 7 missing ones — avg is over what
    actually fired."""
    doc = _doc("inv.md")
    rows = build_invoice_rows(
        [
            (
                doc,
                [
                    _field(doc, "invoice_number", "INV-1", vt="id", conf=0.90),
                    _field(doc, "total", "¥1,000", vt="amount", conf=0.80),
                ],
            )
        ]
    )
    assert pytest.approx(rows[0].confidence_avg, abs=0.001) == 0.85


def test_build_rows_source_path_is_doc_path():
    doc = _doc("inv.md", path="/ws/invoices/inv.md")
    rows = build_invoice_rows(
        [(doc, [_field(doc, "invoice_number", "INV-1", vt="id")])]
    )
    assert rows[0].values["source_path"] == "/ws/invoices/inv.md"


# -- integration fixture ------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Spin up an Engine on a temp sqlite DB and install it as the
    singleton so ``extract_invoices_to_table``'s ``get_engine()`` lookup
    finds it."""
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
    text: str,
    kind: DocumentKind = DocumentKind.MARKDOWN,
    fields: list[tuple[str, str, str, float]] | None = None,
) -> Document:
    path = str(Path(ws.root_path) / rel_path)
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
    if fields:
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


_INVOICE_MD_JP = (
    "# 請求書 INV-2025-03\n"
    "\n"
    "## 取引先\n"
    "ACME 株式会社\n"
    "\n"
    "## 請求日\n"
    "2025/03/15\n"
    "\n"
    "## 支払期限\n"
    "2025/04/30\n"
    "\n"
    "## 合計\n"
    "- 小計: ¥680,000\n"
    "- 消費税 (10%): ¥68,000\n"
    "- 合計: ¥748,000\n"
)

_INVOICE_MD_EN = (
    "# Invoice\n"
    "\n"
    "Invoice No.: INV-2026-001\n"
    "Issue Date: 2026-01-15\n"
    "Due Date: 2026-02-15\n"
    "\n"
    "Bill To: Northwind Trading Inc.\n"
    "From: ACME Studios LLC\n"
    "\n"
    "Subtotal: $6,800.00\n"
    "Tax (8.875%): $603.50\n"
    "Total: $7,403.50\n"
)


# -- integration tests -------------------------------------------------------


def test_end_to_end_runs_extractor_and_writes_csv(isolated_engine, tmp_path):
    """Default path: seed two invoice docs, run the workflow, the file
    extractor fires, the CSV lands in workspace/drafts/ with one row per
    invoice and the expected 12-column header."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv-jp.md", rel_path="invoices/inv-jp.md",
        text=_INVOICE_MD_JP,
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv-en.md", rel_path="invoices/inv-en.md",
        text=_INVOICE_MD_EN,
    )

    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    assert "error" not in out
    assert out["row_count"] == 2
    assert out["candidate_count"] == 2
    assert out["skipped_count"] == 0
    written = Path(out["output_path"])
    assert written.exists()
    assert written.parent.name == "drafts"
    # Timestamp suffix prevents naïve overwrite — base stem must be
    # preserved and the rest must match `YYYYMMDD-HHMMSS`.
    assert written.stem.startswith("invoices-")
    assert written.suffix == ".csv"

    with written.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == list(COLUMNS)
        rows = list(reader)
    assert len(rows) == 2
    by_id = {r["invoice_number"]: r for r in rows}
    assert "INV-2025-03" in by_id
    assert "INV-2026-001" in by_id
    jp = by_id["INV-2025-03"]
    assert jp["currency"] == "JPY"
    assert jp["total"] == "¥748,000"
    assert jp["source_path"].endswith("invoices/inv-jp.md")
    en = by_id["INV-2026-001"]
    assert en["currency"] == "USD"
    assert en["total"] == "$7,403.50"


def test_skips_docs_without_invoice_number_after_extraction(isolated_engine, tmp_path):
    """A markdown that is NOT an invoice (no invoice_number anywhere)
    must end up in `skipped`, not in the CSV — and the CSV should still
    write successfully for the doc that did match."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv.md", rel_path="invoices/inv.md",
        text=_INVOICE_MD_JP,
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="not-invoice.md", rel_path="notes/not-invoice.md",
        text="# Meeting notes\n\nDiscussed Q1 roadmap.\n",
    )

    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    assert out["row_count"] == 1
    assert out["skipped_count"] == 1
    assert out["skipped"][0]["path"].endswith("notes/not-invoice.md")


def test_non_invoice_kinds_are_not_candidates(isolated_engine, tmp_path):
    """Kinds outside {PDF, MD, XLSX, DOCX} should never even reach the
    extractor — a `txt` receipt is excluded by the candidate filter."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="receipt.txt", rel_path="receipts/receipt.txt",
        text="スターバックス\n2025/04/02\n合計  ¥495\n",
        kind=DocumentKind.TXT,
    )
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    assert out["candidate_count"] == 0
    assert out["row_count"] == 0
    # Note explains the empty CSV, output_path stays null since we never
    # opened a file handle.
    assert out["output_path"] is None
    assert "note" in out


def test_refuses_read_only_workspace(isolated_engine, tmp_path):
    """Spec: `safety: write は workspace policy が draft-write 以上の
    ときだけ`. A read-only workspace must refuse the export entirely."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv.md", rel_path="invoices/inv.md",
        text=_INVOICE_MD_JP,
    )
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    assert "error" in out
    assert "read-only" in out["error"].lower() or "high" in out["error"].lower()
    # And no file got written.
    assert not (tmp_path / "drafts").exists()


def test_refuses_target_outside_workspace_root(isolated_engine, tmp_path):
    """Even on a draft-write workspace, writing OUTSIDE the workspace
    root escalates to HIGH risk — refused."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(ws_root), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv.md", rel_path="invoices/inv.md",
        text=_INVOICE_MD_JP,
    )
    outside = tmp_path / "elsewhere" / "invoices.csv"
    out = extract_invoices_to_table(ws.id, str(outside))
    assert "error" in out
    assert not outside.exists()


def test_no_extract_uses_existing_persisted_fields(isolated_engine, tmp_path):
    """`run_extractor=False` skips :func:`extract_invoice_fields` and
    projects whatever is already in `extracted_fields`. Lets callers
    avoid re-running the regex when they have pre-extracted data."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    # Note: text body would NOT yield an invoice_number under the
    # extractor (the heading just says "Notes"), so the only way this
    # row ends up in the CSV is if the pre-persisted fields are used.
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv.md", rel_path="invoices/inv.md",
        text="# Notes\n\nNothing invoice-shaped here.\n",
        fields=[
            ("invoice_number", "INV-PRE-1", "id", 0.95),
            ("total", "¥100,000", "amount", 0.95),
            ("issuer", "ACME 株式会社", "text", 0.95),
        ],
    )
    out = extract_invoices_to_table(
        ws.id, "drafts/invoices.csv", run_extractor=False
    )
    assert out["row_count"] == 1
    assert out["ran_extractor"] is False
    with Path(out["output_path"]).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["invoice_number"] == "INV-PRE-1"
    assert rows[0]["currency"] == "JPY"
    assert rows[0]["issuer"] == "ACME 株式会社"


def test_empty_workspace_returns_empty_result(isolated_engine, tmp_path):
    """A workspace with no candidate documents at all must return the
    empty-result shape (note set, output_path None) instead of crashing
    on the file write."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    assert out["row_count"] == 0
    assert out["candidate_count"] == 0
    assert out["output_path"] is None
    assert "note" in out


def test_unknown_workspace_returns_error(isolated_engine, tmp_path):
    out = extract_invoices_to_table("ws_does_not_exist", "/tmp/x.csv")
    assert "error" in out


def test_low_confidence_rows_are_flagged(isolated_engine, tmp_path):
    """Rows with avg confidence below 0.70 must surface in
    `low_confidence_paths` so the caller can prompt the user to verify
    them by hand."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv-lowconf.md", rel_path="invoices/inv-lowconf.md",
        text="# Notes\n\nNothing invoice-shaped here.\n",
        fields=[("invoice_number", "INV-LC", "id", 0.50)],
    )
    out = extract_invoices_to_table(
        ws.id, "drafts/invoices.csv", run_extractor=False
    )
    assert out["low_confidence_count"] == 1
    assert out["low_confidence_paths"][0].endswith("invoices/inv-lowconf.md")


def test_timestamp_suffix_makes_consecutive_writes_distinct(isolated_engine, tmp_path):
    """Two back-to-back exports must write to different timestamped
    files. We can't always guarantee a clock tick within the test, but
    if the timestamps DO collide the second write should still succeed
    by overwriting its own previous output (deterministic for the
    second-call user); the contract is that the FIRST file is never
    silently destroyed by the second call."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv.md", rel_path="invoices/inv.md",
        text=_INVOICE_MD_JP,
    )
    out1 = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    out2 = extract_invoices_to_table(ws.id, "drafts/invoices.csv")
    p1 = Path(out1["output_path"])
    p2 = Path(out2["output_path"])
    # Whether they tick to different seconds or not, both files are
    # under drafts/ with the same stem prefix.
    assert p1.parent == p2.parent == tmp_path / "drafts"
    assert p1.stem.startswith("invoices-")
    assert p2.stem.startswith("invoices-")
    # If they DID tick to a different second the original file must
    # still be on disk (no overwrite happened).
    if p1 != p2:
        assert p1.exists() and p2.exists()


# -- mass-op guard ------------------------------------------------------------


def _seed_invoice(storage, ws, *, n: int) -> None:
    """Seed a workspace with `n` pre-extracted invoice docs.

    Uses `run_extractor=False` callers — the doc bodies are throwaway
    (the extractor would not pick them up). Pre-persisted fields make
    each row pass the `has invoice_number` filter so we can fan up to
    arbitrary row counts without writing N realistic invoice markdowns.
    """
    for i in range(n):
        _seed_doc(
            storage, ws,
            name=f"inv-{i:03d}.md",
            rel_path=f"invoices/inv-{i:03d}.md",
            text="# placeholder\n",
            fields=[
                ("invoice_number", f"INV-MASS-{i:03d}", "id", 0.95),
                ("total", "¥10,000", "amount", 0.95),
            ],
        )


def test_mass_op_at_threshold_writes_directly(isolated_engine, tmp_path):
    """Exactly the threshold (5 rows) must NOT trigger the gate — the
    rule is `> threshold`, not `>=`."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_invoice(isolated_engine.storage, ws, n=5)
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    assert out.get("confirmation_required") is None or not out["confirmation_required"]
    assert out["row_count"] == 5
    assert out["output_path"] is not None
    assert Path(out["output_path"]).exists()
    # Preview file must NOT be staged when no confirmation is needed.
    preview = tmp_path / "drafts" / "invoices.preview.csv"
    assert not preview.exists()


def test_mass_op_over_threshold_stages_preview_and_blocks_write(isolated_engine, tmp_path):
    """6 rows + confirm=False: no full CSV, preview written, response
    carries confirmation_required + preview_path."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_invoice(isolated_engine.storage, ws, n=6)
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    assert out["confirmation_required"] is True
    assert out["row_count"] == 6
    assert out["output_path"] is None
    assert out["reason"].startswith("bulk_modify of 6 rows exceeds")
    # Preview file was written and contains the head, not the whole set.
    preview_path = Path(out["preview_path"])
    assert preview_path.exists()
    assert preview_path.name == "invoices.preview.csv"
    with preview_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    # 6 rows ≤ default head of 10, so all of them land in the preview.
    assert len(rows) == 6
    # Critically, no timestamped CSV exists yet — only the preview.
    timestamped = [p for p in (tmp_path / "drafts").iterdir() if p.name.startswith("invoices-")]
    assert timestamped == []


def test_mass_op_over_threshold_with_confirm_writes_full_csv(isolated_engine, tmp_path):
    """confirm=True must skip the gate and write the full file."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_invoice(isolated_engine.storage, ws, n=7)
    out = extract_invoices_to_table(
        ws.id, "drafts/invoices.csv", run_extractor=False, confirm=True
    )
    assert not out.get("confirmation_required")
    assert out["row_count"] == 7
    assert out["confirmed"] is True
    assert out["output_path"] is not None
    with Path(out["output_path"]).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 7


def test_mass_op_preview_head_caps_at_10_for_large_row_counts(isolated_engine, tmp_path):
    """A 50-row export must still stage just the first 10 rows in the
    preview — the preview is a review artifact, not a stand-in for the
    full export."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_invoice(isolated_engine.storage, ws, n=50)
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    assert out["confirmation_required"] is True
    assert out["row_count"] == 50
    with Path(out["preview_path"]).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 10


def test_mass_op_preview_overwrites_on_rerun(isolated_engine, tmp_path):
    """The preview file is intentionally NOT timestamped — re-running
    the same call must overwrite its previous preview instead of
    leaving a pile of `.preview-YYYY...csv` siblings."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_invoice(isolated_engine.storage, ws, n=6)
    out1 = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    out2 = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    # Same preview path on both calls.
    assert out1["preview_path"] == out2["preview_path"]
    # No accumulating `*.preview-*.csv` files.
    drafts = tmp_path / "drafts"
    preview_like = [p for p in drafts.iterdir() if ".preview" in p.name]
    assert preview_like == [Path(out1["preview_path"])]


def test_mass_op_refuses_read_only_workspace_before_preview(isolated_engine, tmp_path):
    """A read-only workspace must refuse the export before staging any
    preview — the workspace policy is a hard rule, not a soft one."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    _seed_invoice(isolated_engine.storage, ws, n=10)
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    assert "error" in out
    # And no preview was staged.
    assert not (tmp_path / "drafts" / "invoices.preview.csv").exists()


def test_mass_op_under_threshold_does_not_set_confirmed_when_default(isolated_engine, tmp_path):
    """The `confirmed` field reflects the caller's intent, not the path
    we took. Under-threshold + default confirm=False → confirmed=False
    even though no gate fired."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    _seed_invoice(isolated_engine.storage, ws, n=3)
    out = extract_invoices_to_table(ws.id, "drafts/invoices.csv", run_extractor=False)
    assert out["confirmed"] is False
    assert out["row_count"] == 3
    assert out["output_path"] is not None
