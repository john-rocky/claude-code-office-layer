"""Tests for Phase 2 email draft workflow.

Split into:

* Pure-function tests over :func:`build_email_draft_body` — assembles
  the markdown skeleton (front-matter + citations + checklist) without
  touching storage or disk.
* Integration tests that seed an isolated workspace + sqlite engine
  and exercise :func:`draft_email_from_evidence` end-to-end through
  ``get_engine()``, covering the safety gate (read-only workspace,
  outside-workspace target), timestamped filenames, and the
  extracted-field surfacing into the checklist.
"""

from __future__ import annotations

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
    DocumentKind,
    EvidencePacket,
    EvidenceSource,
    ExtractedField,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402
from office_layer.workflows.email_draft import (  # noqa: E402
    build_email_draft_body,
    draft_email_from_evidence,
)


# -- packet fixtures ----------------------------------------------------------


def _field(key: str, value: str, *, vt: str = "text", conf: float = 0.95) -> ExtractedField:
    return ExtractedField(
        document_id="doc_test",
        key=key,
        value=value,
        value_type=vt,  # type: ignore[arg-type]
        confidence=conf,
    )


def _source(
    *,
    file_path: str,
    file_name: str,
    text: str,
    fields: list[ExtractedField] | None = None,
    kind: DocumentKind = DocumentKind.MARKDOWN,
) -> EvidenceSource:
    return EvidenceSource(
        source_id=f"src_{file_name}",
        file_path=file_path,
        file_name=file_name,
        file_type=kind,
        workspace_name="ws",
        extracted_text=text,
        extracted_fields=fields or [],
    )


def _packet(
    *,
    intent: str = "follow up on invoice",
    sources: list[EvidenceSource] | None = None,
    low_confidence: list[str] | None = None,
) -> EvidencePacket:
    return EvidencePacket(
        intent=intent,
        sources=sources or [],
        low_confidence_items=low_confidence or [],
    )


# -- pure-function tests ------------------------------------------------------


def test_body_includes_front_matter_recipient_and_subject():
    """Front matter must carry the recipient + subject so downstream
    tools (editor plugins, search) can index the draft cleanly."""
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="hello")])
    draft = build_email_draft_body(
        packet, recipient="jane@acme.co.jp", subject="Re: March invoice", intent="confirm payment"
    )
    assert "recipient: jane@acme.co.jp" in draft.body_markdown
    assert "subject: Re: March invoice" in draft.body_markdown
    assert "intent: confirm payment" in draft.body_markdown
    assert draft.body_markdown.startswith("---\n")
    # status flag prevents anyone parsing this draft as a sent record
    assert "status: draft (never sent)" in draft.body_markdown


def test_language_auto_detects_japanese_from_packet():
    """Auto mode picks `ja` when packet content contains JP chars."""
    packet = _packet(
        intent="請求書の支払確認",
        sources=[_source(file_path="/ws/inv.md", file_name="inv.md", text="ACME 株式会社 様")],
    )
    draft = build_email_draft_body(
        packet, recipient="jane@acme.co.jp", subject="請求書確認", intent="支払い確認"
    )
    assert draft.language == "ja"
    assert "用件" in draft.body_markdown
    assert "本ドラフトは office-layer" in draft.body_markdown


def test_language_auto_falls_back_to_english_without_japanese():
    packet = _packet(
        intent="follow up on invoice",
        sources=[_source(file_path="/ws/inv.md", file_name="inv.md", text="Total: $7,403.50")],
    )
    draft = build_email_draft_body(
        packet, recipient="ap@northwind.test", subject="Invoice follow-up", intent="confirm payment"
    )
    assert draft.language == "en"
    assert "Purpose" in draft.body_markdown
    assert "This draft was staged by office-layer" in draft.body_markdown


def test_extracted_fields_surface_in_checklist():
    """Invoice number / total / due date in the packet must appear in
    the 'before sending' checklist so the sender is reminded to verify
    the values that, if wrong, embarrass them most."""
    packet = _packet(
        sources=[
            _source(
                file_path="/ws/inv.md",
                file_name="inv.md",
                text="invoice",
                fields=[
                    _field("invoice_number", "INV-2026-001", vt="id"),
                    _field("total", "$7,403.50", vt="amount"),
                    _field("due_date", "2026-02-15", vt="date"),
                ],
            )
        ]
    )
    draft = build_email_draft_body(
        packet, recipient="ap@northwind.test", subject="Invoice", intent="confirm"
    )
    assert "Verify invoice_number = `INV-2026-001`" in draft.body_markdown
    assert "Verify total = `$7,403.50`" in draft.body_markdown
    assert "Verify due_date = `2026-02-15`" in draft.body_markdown
    # checklist count = 3 header rows + 3 field rows + 0 low-confidence
    assert draft.checklist_item_count == 6


def test_low_confidence_items_get_explicit_checklist_rows():
    packet = _packet(
        sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")],
        low_confidence=["INV-LC (regex-baseline) confidence=0.50"],
    )
    draft = build_email_draft_body(
        packet, recipient="jane@acme.test", subject="x", intent="x"
    )
    assert "Re-check low-confidence item: INV-LC" in draft.body_markdown


def test_packet_with_no_sources_emits_no_evidence_notice():
    """Empty packet must still produce a valid draft, with a notice so
    Claude knows to ask the user rather than fabricate citations."""
    packet = _packet(sources=[])
    draft = build_email_draft_body(
        packet, recipient="jane@acme.test", subject="meeting", intent="schedule a call"
    )
    assert draft.citation_count == 0
    assert "No evidence was attached" in draft.body_markdown


def test_extra_context_block_rendered_verbatim():
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")])
    draft = build_email_draft_body(
        packet,
        recipient="jane@acme.test",
        subject="x",
        intent="x",
        extra_context="Client asked for a timeline by Friday.",
    )
    assert "## Additional context" in draft.body_markdown
    assert "Client asked for a timeline by Friday." in draft.body_markdown


def test_locator_renders_page_sheet_cell_for_each_source():
    """Citation block must surface page/sheet/cell when the source
    carries them — otherwise the user cannot click through to verify."""
    src = EvidenceSource(
        source_id="s1",
        file_path="/ws/inv.xlsx",
        file_name="inv.xlsx",
        file_type=DocumentKind.XLSX,
        workspace_name="ws",
        extracted_text="Total: $7,403.50",
        sheet_name="Invoices",
        cell_range="B14",
    )
    draft = build_email_draft_body(
        _packet(sources=[src]),
        recipient="ap@northwind.test",
        subject="x",
        intent="x",
    )
    assert "/ws/inv.xlsx" in draft.body_markdown
    assert "sheet=Invoices" in draft.body_markdown
    assert "cells=B14" in draft.body_markdown


# -- integration fixture ------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Spin up an Engine on a temp sqlite DB and install it as the
    singleton so ``draft_email_from_evidence``'s ``get_engine()`` lookup
    finds it."""
    adapter_registry.reset_registry()
    storage = Storage(tmp_path / "office.sqlite")
    eng = Engine(storage=storage, registry=get_registry(), embedder=NullEmbedder())
    monkeypatch.setattr(engine_module, "_engine", eng)
    yield eng
    eng.close()
    adapter_registry.reset_registry()


# -- integration tests --------------------------------------------------------


def test_end_to_end_stages_a_draft(isolated_engine, tmp_path):
    """Default path: real workspace, packet dict in, draft markdown
    file out under workspace/drafts/, draft_id + path returned."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(
        intent="invoice follow-up",
        sources=[
            _source(
                file_path=str(tmp_path / "invoices/inv.md"),
                file_name="inv.md",
                text="Invoice INV-2026-001 — total $7,403.50",
                fields=[_field("invoice_number", "INV-2026-001", vt="id")],
            )
        ],
    )
    out = draft_email_from_evidence(
        ws.id,
        packet=packet.model_dump(mode="json"),
        recipient="ap@northwind.test",
        subject="Invoice INV-2026-001 — payment confirmation",
    )
    assert "error" not in out
    assert out["draft_id"].startswith("ed_")
    written = Path(out["output_path"])
    assert written.exists()
    assert written.parent.name == "drafts"
    assert written.parent.parent == tmp_path
    body = written.read_text(encoding="utf-8")
    assert "INV-2026-001" in body
    assert "ap@northwind.test" in body
    assert out["citation_count"] == 1


def test_refuses_read_only_workspace(isolated_engine, tmp_path):
    """A read-only workspace must refuse the draft entirely — no file
    written, no directory created."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")])
    out = draft_email_from_evidence(
        ws.id,
        packet=packet.model_dump(mode="json"),
        recipient="jane@acme.test",
        subject="x",
    )
    assert "error" in out
    assert "high" in out["error"].lower() or "read-only" in out["error"].lower()
    assert not (tmp_path / "drafts").exists()


def test_timestamp_suffix_makes_consecutive_drafts_distinct(isolated_engine, tmp_path):
    """Two back-to-back drafts to the same recipient must not silently
    overwrite — both filenames end in HHMMSS so collisions only happen
    within the same wall-clock second."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")])
    out1 = draft_email_from_evidence(
        ws.id, packet=packet.model_dump(mode="json"),
        recipient="jane@acme.test", subject="ping",
    )
    out2 = draft_email_from_evidence(
        ws.id, packet=packet.model_dump(mode="json"),
        recipient="jane@acme.test", subject="ping",
    )
    p1 = Path(out1["output_path"])
    p2 = Path(out2["output_path"])
    assert p1.parent == p2.parent == tmp_path / "drafts"
    # Filename stem starts with the date + recipient slug
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert p1.name.startswith(f"{today}-jane-ping-") or p1.name.startswith(f"{today}-jane-")
    if p1 != p2:
        assert p1.exists() and p2.exists()


def test_unknown_workspace_returns_error(isolated_engine, tmp_path):
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")])
    out = draft_email_from_evidence(
        "ws_does_not_exist",
        packet=packet.model_dump(mode="json"),
        recipient="jane@acme.test",
        subject="x",
    )
    assert "error" in out


def test_empty_recipient_returns_error(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")])
    out = draft_email_from_evidence(
        ws.id, packet=packet.model_dump(mode="json"), recipient="   ", subject="x"
    )
    assert "error" in out
    assert "recipient" in out["error"].lower()


def test_invalid_packet_shape_returns_error(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    out = draft_email_from_evidence(
        ws.id, packet="not a packet", recipient="jane@acme.test", subject="x"  # type: ignore[arg-type]
    )
    assert "error" in out
    assert "packet" in out["error"].lower()


def test_accepts_evidence_packet_object_directly(isolated_engine, tmp_path):
    """Caller may pass either a dict (MCP boundary) or the model
    instance directly (Python-side callers like the CLI)."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="x")])
    out = draft_email_from_evidence(
        ws.id, packet=packet, recipient="jane@acme.test", subject="x"
    )
    assert "error" not in out
    assert Path(out["output_path"]).exists()


# -- PII checklist promotion --------------------------------------------------


def test_pii_in_citation_preview_is_promoted_to_checklist(isolated_engine, tmp_path):
    """A phone number sitting on the first line of a source doc lands in
    the citation preview, so the PII scanner must catch it from the
    body and promote a `Verify phone` checklist row."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(
        intent="支払期限のご確認",
        sources=[
            _source(
                file_path=str(tmp_path / "x.md"),
                file_name="x.md",
                text="連絡先 山田 太郎  電話: 03-1234-5678\n本文以下省略",
            )
        ],
    )
    out = draft_email_from_evidence(
        ws.id, packet=packet.model_dump(mode="json"),
        recipient="ap@example.test", subject="ご請求",
    )
    assert out["pii_hit_count"] >= 1
    body = Path(out["output_path"]).read_text(encoding="utf-8")
    assert "PII 検出" in body
    assert "03-1234-5678" in body


def test_pii_in_extra_context_is_promoted(isolated_engine, tmp_path):
    """A credit card pasted into extra_context must surface via Luhn."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="hello")])
    out = draft_email_from_evidence(
        ws.id,
        packet=packet.model_dump(mode="json"),
        recipient="ap@example.test",
        subject="payment",
        extra_context="card on file: 4111-1111-1111-1111",
    )
    assert out["pii_hit_count"] == 1
    assert out["pii_hits"][0]["kind"] == "credit_card"


def test_no_pii_means_no_pii_section(isolated_engine, tmp_path):
    """Clean draft must not append a PII section — the row only appears
    when there is something to confirm."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(sources=[_source(file_path="/ws/x.md", file_name="x.md", text="hello")])
    out = draft_email_from_evidence(
        ws.id, packet=packet.model_dump(mode="json"),
        recipient="jane@acme.test", subject="ping",
    )
    body = Path(out["output_path"]).read_text(encoding="utf-8")
    assert out["pii_hit_count"] == 0
    assert "PII 検出" not in body
    assert "PII detected" not in body


def test_invoice_id_with_separators_does_not_trigger_pii(isolated_engine, tmp_path):
    """The canonical false-positive case from the manual probe:
    `INV-1234-5678` as the subject + invoice_number must NOT raise a
    credit_card warning."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE
    )
    packet = _packet(
        intent="支払期限のご確認",
        sources=[
            _source(
                file_path=str(tmp_path / "inv.md"),
                file_name="inv.md",
                text="請求書\nINV-1234-5678",
                fields=[_field("invoice_number", "INV-1234-5678", vt="id")],
            )
        ],
    )
    out = draft_email_from_evidence(
        ws.id, packet=packet.model_dump(mode="json"),
        recipient="ap@example.test",
        subject="請求書 INV-1234-5678 の確認",
    )
    assert out["pii_hit_count"] == 0
