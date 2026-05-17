"""Tests for Phase 2 client-history workflow.

Split into:

* Pure-function tests over alias expansion / amount parsing / bucket
  classification — fast, no I/O.
* Integration tests that seed two workspaces into a temp SQLite DB and
  exercise ``build_client_history`` end-to-end through ``get_engine()``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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
    Entity,
    ExtractedField,
    ExtractionMethod,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402
from office_layer.workflows.client_history import (  # noqa: E402
    build_client_history,
    classify_doc,
    expand_aliases,
    parse_amount,
)


# -- pure-function tests ------------------------------------------------------


def test_expand_aliases_strips_jp_corporate_suffixes():
    out = expand_aliases("ACME 株式会社")
    lo = {v.lower() for v in out}
    assert "acme 株式会社" in lo
    assert "acme" in lo


def test_expand_aliases_strips_en_corporate_suffix():
    out = expand_aliases("Northwind Trading Inc.")
    lo = {v.lower() for v in out}
    assert "northwind trading inc." in lo
    assert "northwind trading" in lo


def test_expand_aliases_expands_email_to_local_domain_label():
    out = expand_aliases("alice@acme.co.jp")
    lo = {v.lower() for v in out}
    assert "alice@acme.co.jp" in lo
    assert "alice" in lo
    assert "acme.co.jp" in lo
    assert "acme" in lo


def test_expand_aliases_expands_bare_domain():
    out = expand_aliases("acme.co.jp")
    lo = {v.lower() for v in out}
    assert "acme.co.jp" in lo
    assert "acme" in lo


def test_expand_aliases_appends_caller_aliases_and_dedupes():
    out = expand_aliases("ACME 株式会社", extra=["ACME", "プロジェクト・ロケット", "ACME"])
    lo = [v.lower() for v in out]
    # Dedup: "acme" appears only once even though both the stripped stem and
    # the caller-supplied alias produce the same lowercased key.
    assert lo.count("acme") == 1
    assert "プロジェクト・ロケット" in out


def test_expand_aliases_filters_too_short():
    out = expand_aliases("X 株式会社")
    # "X" alone is below MIN_ALIAS_LEN — dropped to avoid LIKE-junk.
    lo = {v.lower() for v in out}
    assert "x" not in lo
    assert "x 株式会社" in lo


def test_parse_amount_jpy_symbol_and_suffix():
    assert parse_amount("¥748,000") == ("JPY", 748000.0)
    assert parse_amount("12,000 円") == ("JPY", 12000.0)
    assert parse_amount("100 万円") == ("JPY", 1_000_000.0)
    assert parse_amount("1.5億円") == ("JPY", 150_000_000.0)


def test_parse_amount_usd_eur():
    assert parse_amount("$7,403.50") == ("USD", 7403.5)
    assert parse_amount("€500") == ("EUR", 500.0)
    assert parse_amount("USD 1,200.50") == ("USD", 1200.5)


def test_parse_amount_returns_none_on_bare_number():
    # Refuse to guess a currency from a bare number — the wrong default is
    # worse than no number.
    assert parse_amount("12,000") is None
    assert parse_amount("") is None
    assert parse_amount("garbage") is None


def test_classify_doc_invoice_by_extracted_field():
    doc = _doc_stub("notes/something.md", DocumentKind.MARKDOWN)
    assert classify_doc(doc, {"invoice_number", "total"}) == "invoice"


def test_classify_doc_invoice_by_path_when_no_fields():
    doc = _doc_stub("/ws/invoices/x.md", DocumentKind.MARKDOWN)
    assert classify_doc(doc, set()) == "invoice"


def test_classify_doc_contract_by_path():
    doc = _doc_stub("/ws/contracts/nda.md", DocumentKind.MARKDOWN)
    assert classify_doc(doc, set()) == "contract"


def test_classify_doc_email_by_kind():
    doc = _doc_stub("/ws/inbox/m.eml", DocumentKind.EMAIL)
    assert classify_doc(doc, set()) == "email"


def test_classify_doc_note_fallback():
    doc = _doc_stub("/ws/random/note.md", DocumentKind.MARKDOWN)
    assert classify_doc(doc, set()) == "note"


# -- integration tests -------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Spin up an Engine on a temp sqlite DB and install it as the singleton
    so ``workflows.client_history``'s ``get_engine()`` finds it."""
    adapter_registry.reset_registry()
    storage = Storage(tmp_path / "office.sqlite")
    eng = Engine(storage=storage, registry=get_registry(), embedder=NullEmbedder())
    monkeypatch.setattr(engine_module, "_engine", eng)
    yield eng
    eng.close()
    adapter_registry.reset_registry()


def test_aggregates_invoice_and_contract_across_one_workspace(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="acme-ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )

    # Invoice doc: client name as recipient + a persisted total.
    inv = _seed_doc(
        isolated_engine.storage,
        ws,
        name="invoice-acme-2026-01.md",
        rel_path="invoices/invoice-acme-2026-01.md",
        kind=DocumentKind.MARKDOWN,
        text="請求書\n取引先: ACME 株式会社\n合計: ¥748,000\n",
        entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "INV-2026-01", "id"), ("total", "¥748,000", "amount")],
    )

    # Contract doc: ACME named in the body, persisted as an entity.
    ct = _seed_doc(
        isolated_engine.storage,
        ws,
        name="nda-acme.md",
        rel_path="contracts/nda-acme.md",
        kind=DocumentKind.MARKDOWN,
        text="本契約は ACME 株式会社 と ジョン・ロッキー との間で締結する。",
        entities=[("ACME 株式会社", "org")],
    )

    # Unrelated doc: a different client. Must not appear.
    _seed_doc(
        isolated_engine.storage,
        ws,
        name="invoice-other.md",
        rel_path="invoices/invoice-other.md",
        kind=DocumentKind.MARKDOWN,
        text="取引先: 株式会社サンプル商事\n合計: ¥100,000\n",
        entities=[("株式会社サンプル商事", "org")],
        fields=[("invoice_number", "INV-OTHER", "id"), ("total", "¥100,000", "amount")],
    )

    out = build_client_history("ACME 株式会社")
    assert out["client"] == "ACME 株式会社"
    assert out["document_count"] == 2
    assert out["invoice_count"] == 1
    assert out["contract_count"] == 1
    assert out["email_count"] == 0
    assert out["note_count"] == 0
    assert out["total_invoiced_amount"] == {"JPY": 748000.0}
    timeline_doc_ids = {row["doc_id"] for row in out["timeline"]}
    assert inv.id in timeline_doc_ids
    assert ct.id in timeline_doc_ids
    # The evidence packet wraps the same docs, capped at evidence_max_sources.
    packet = out["evidence_packet"]
    assert packet["intent"] == "client_history:ACME 株式会社"
    assert len(packet["sources"]) >= 1


def test_aggregates_across_workspaces_when_not_filtered(isolated_engine, tmp_path):
    """Two workspaces, ACME doc in each. Without ``workspace_ids`` they merge."""
    ws_a = isolated_engine.workspaces.add(
        name="a", root_path=str(tmp_path / "a"), policy=WorkspacePolicy.READ_ONLY
    )
    ws_b = isolated_engine.workspaces.add(
        name="b", root_path=str(tmp_path / "b"), policy=WorkspacePolicy.READ_ONLY
    )
    _seed_doc(
        isolated_engine.storage, ws_a,
        name="invoice-a.md", rel_path="invoices/invoice-a.md",
        kind=DocumentKind.MARKDOWN,
        text="ACME 株式会社\n合計: ¥100,000\n",
        entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "INV-A", "id"), ("total", "¥100,000", "amount")],
    )
    _seed_doc(
        isolated_engine.storage, ws_b,
        name="invoice-b.md", rel_path="invoices/invoice-b.md",
        kind=DocumentKind.MARKDOWN,
        text="ACME 株式会社\n合計: ¥200,000\n",
        entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "INV-B", "id"), ("total", "¥200,000", "amount")],
    )

    out = build_client_history("ACME")
    assert out["invoice_count"] == 2
    assert out["total_invoiced_amount"] == {"JPY": 300000.0}
    assert set(out["workspace_ids"]) == {ws_a.id, ws_b.id}

    # And when we DO filter, only that workspace's doc is returned.
    only_a = build_client_history("ACME", workspace_ids=[ws_a.id])
    assert only_a["invoice_count"] == 1
    assert only_a["total_invoiced_amount"] == {"JPY": 100000.0}


def test_domain_input_finds_company_via_alias_expansion(isolated_engine, tmp_path):
    """Calling with ``acme.co.jp`` should expand to ``acme`` and hit the
    ACME-named invoice through the filename + entity fallback."""
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="invoice-acme.md", rel_path="invoices/invoice-acme.md",
        kind=DocumentKind.MARKDOWN,
        text="ACME 株式会社\n合計: ¥50,000\n",
        entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "INV-Z", "id"), ("total", "¥50,000", "amount")],
    )
    out = build_client_history("acme.co.jp")
    assert out["invoice_count"] == 1
    assert any(v.lower() == "acme" for v in out["aliases_used"])


def test_multi_currency_totals_grouped_not_summed(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv-jpy.md", rel_path="invoices/inv-jpy.md",
        kind=DocumentKind.MARKDOWN,
        text="ACME 株式会社\n合計: ¥748,000\n",
        entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "INV-JP", "id"), ("total", "¥748,000", "amount")],
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="inv-usd.md", rel_path="invoices/inv-usd.md",
        kind=DocumentKind.MARKDOWN,
        text="ACME Studios LLC\nTotal: $7,403.50\n",
        entities=[("ACME Studios LLC", "org")],
        fields=[("invoice_number", "INV-US", "id"), ("total", "$7,403.50", "amount")],
    )
    out = build_client_history("ACME")
    assert out["total_invoiced_amount"] == {"JPY": 748000.0, "USD": 7403.5}
    assert out["invoice_count"] == 2


def test_unknown_client_returns_zero_counts(isolated_engine, tmp_path):
    isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    out = build_client_history("DoesNotExistCorp")
    assert out["document_count"] == 0
    assert out["invoice_count"] == 0
    assert out["timeline"] == []
    assert out["total_invoiced_amount"] == {}


def test_empty_client_name_returns_error(isolated_engine):
    assert "error" in build_client_history("")
    assert "error" in build_client_history("   ")


def test_timeline_sorted_newest_first(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="ws", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    now = datetime.now(timezone.utc)
    _seed_doc(
        isolated_engine.storage, ws,
        name="old.md", rel_path="invoices/old.md", kind=DocumentKind.MARKDOWN,
        text="ACME 株式会社\n", entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "OLD", "id")],
        mtime=now - timedelta(days=60),
    )
    _seed_doc(
        isolated_engine.storage, ws,
        name="new.md", rel_path="invoices/new.md", kind=DocumentKind.MARKDOWN,
        text="ACME 株式会社\n", entities=[("ACME 株式会社", "org")],
        fields=[("invoice_number", "NEW", "id")],
        mtime=now,
    )
    out = build_client_history("ACME")
    assert [r["name"] for r in out["timeline"][:2]] == ["new.md", "old.md"]


# -- seeding helpers ---------------------------------------------------------


def _doc_stub(path: str, kind: DocumentKind) -> Document:
    """Cheap Document used only by classify_doc unit tests — bypasses storage."""
    return Document(
        id="d_stub",
        workspace_id="ws_stub",
        path=path,
        name=Path(path).name,
        kind=kind,
        size_bytes=0,
        mtime=datetime.now(timezone.utc),
    )


def _seed_doc(
    storage: Storage,
    ws: Workspace,
    *,
    name: str,
    rel_path: str,
    kind: DocumentKind,
    text: str,
    entities: list[tuple[str, str]] | None = None,
    fields: list[tuple[str, str, str]] | None = None,
    mtime: datetime | None = None,
) -> Document:
    path = str(Path(ws.root_path) / rel_path)
    doc = Document(
        id=Document.make_id(ws.id, path),
        workspace_id=ws.id,
        path=path,
        name=name,
        kind=kind,
        size_bytes=len(text),
        mtime=mtime or datetime.now(timezone.utc),
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
    if entities:
        storage.replace_entities(
            doc.id,
            [
                Entity(document_id=doc.id, chunk_id=chunk.id, text=t, kind=k)  # type: ignore[arg-type]
                for t, k in entities
            ],
        )
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
                    confidence=0.95,
                )
                for k, v, vt in fields
            ],
        )
    return doc
