"""Tests for Phase 2 contract section extractor.

Same split as :mod:`tests.test_invoice_extraction`:

* Pure-function tests over the heading regex / section cutter — fast, no I/O.
* Integration tests that seed a temp workspace into a temp SQLite DB and
  exercise ``extract_contract_sections`` end-to-end, asserting the
  ``section.*`` ExtractedField rows land in storage with the expected
  keys + values + ordering, and that other extractor's fields (e.g.
  invoice fields) survive a re-run.
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
from office_layer.workflows.contract_sections import (  # noqa: E402
    extract_contract_sections,
    extract_sections_from_text,
)


# -- pure-function tests ------------------------------------------------------


def test_jp_markdown_nda_five_clauses():
    """The shipped NDA fixture shape: `## 第N条 (タイトル)`."""
    text = (
        "# 秘密保持契約\n"
        "\n"
        "本契約は ACME 株式会社 と ジョン・ロッキー との間で締結する。\n"
        "\n"
        "## 第1条 (定義)\n"
        "本契約において「秘密情報」とは、開示されるすべての情報をいう。\n"
        "\n"
        "## 第2条 (秘密保持義務)\n"
        "受領者は秘密情報を 3年間 第三者に開示してはならない。\n"
        "\n"
        "## 第3条 (損害賠償)\n"
        "本契約違反により生じた損害については上限なしで賠償の責任を負う。\n"
        "\n"
        "## 第4条 (準拠法)\n"
        "本契約は日本法に準拠する。\n"
        "\n"
        "## 第5条 (合意管轄)\n"
        "東京地方裁判所を専属的合意管轄とする。\n"
    )
    sections, preamble = extract_sections_from_text(text)
    assert len(sections) == 5
    assert [s.ordinal for s in sections] == [1, 2, 3, 4, 5]
    assert [s.title for s in sections] == [
        "定義",
        "秘密保持義務",
        "損害賠償",
        "準拠法",
        "合意管轄",
    ]
    assert sections[0].heading == "第1条 (定義)"
    assert "ACME 株式会社" in preamble
    assert "本契約において" in sections[0].body
    assert "3年間" in sections[1].body
    # Section bodies do not bleed across boundaries.
    assert "秘密保持義務" not in sections[0].body


def test_en_section_and_article_headings():
    text = (
        "# Mutual NDA\n"
        "\n"
        "## Section 1 — Definitions\n"
        '"Confidential Information" means any non-public information.\n'
        "\n"
        "## Section 2: Obligations\n"
        "Recipient shall not disclose for five (5) years.\n"
        "\n"
        "## Article 3. Governing Law\n"
        "This Agreement is governed by the laws of New York.\n"
    )
    sections, _ = extract_sections_from_text(text)
    assert [s.ordinal for s in sections] == [1, 2, 3]
    assert [s.title for s in sections] == [
        "Definitions",
        "Obligations",
        "Governing Law",
    ]
    assert "five (5) years" in sections[1].body
    assert "New York" in sections[2].body


def test_numbered_markdown_headings():
    """`## 1. Title` form — used by some Western contract drafters."""
    text = (
        "# Agreement\n"
        "\n"
        "## 1. Definitions\n"
        "Body 1.\n"
        "\n"
        "## 2. Term\n"
        "Body 2.\n"
    )
    sections, _ = extract_sections_from_text(text)
    assert [s.ordinal for s in sections] == [1, 2]
    assert [s.title for s in sections] == ["Definitions", "Term"]


def test_bare_jp_headings_without_markdown_prefix():
    """Contracts coming out of a PDF extractor often drop the `##` prefix."""
    text = (
        "秘密保持契約書\n"
        "\n"
        "第1条 (目的)\n"
        "本契約は秘密情報の取り扱いを定める。\n"
        "\n"
        "第2条 (定義)\n"
        "秘密情報とは……。\n"
    )
    sections, _ = extract_sections_from_text(text)
    assert [s.ordinal for s in sections] == [1, 2]
    assert [s.title for s in sections] == ["目的", "定義"]


def test_inline_clause_reference_is_not_a_heading():
    """A prose line that mentions `第3条` mid-sentence must not split."""
    text = (
        "## 第1条 (定義)\n"
        "秘密情報とは ……。 前条 および 第3条 に従い、これを取り扱う。\n"
        "なお 第3条 に定める範囲を超えてはならない。\n"
        "\n"
        "## 第2条 (義務)\n"
        "義務の本文。\n"
    )
    sections, _ = extract_sections_from_text(text)
    # The "第3条" mentions inside prose must NOT yield a section — the
    # only headings are the two markdown ones.
    assert [s.ordinal for s in sections] == [1, 2]
    assert "第3条 に従い" in sections[0].body


def test_no_clause_headings_returns_empty_with_full_preamble():
    text = "本書はメモです。 契約条項はありません。\n"
    sections, preamble = extract_sections_from_text(text)
    assert sections == []
    assert preamble == text


def test_empty_text_returns_empty():
    assert extract_sections_from_text("") == ([], "")


def test_section_char_offsets_locate_back_into_text():
    """``char_offset`` must point at the heading line in the source string."""
    text = "preamble line\n\n## 第1条 (alpha)\nbody alpha\n\n## 第2条 (beta)\nbody beta\n"
    sections, _ = extract_sections_from_text(text)
    assert len(sections) == 2
    assert text[sections[0].char_offset:].startswith("## 第1条")
    assert text[sections[1].char_offset:].startswith("## 第2条")


def test_non_monotonic_ordinal_addendum():
    """Addenda sometimes restart numbering or jump — ordinal is the parsed
    number, not the position. The cutter must accept that without crashing."""
    text = "## 第3条 (補則)\nA\n\n## 第1条 (追加定義)\nB\n"
    sections, _ = extract_sections_from_text(text)
    assert [s.ordinal for s in sections] == [3, 1]


# -- integration tests --------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """Spin up an Engine on a temp sqlite DB and install it as the singleton
    so ``workflows/contract_sections.py``'s ``get_engine()`` finds it."""
    adapter_registry.reset_registry()
    storage = Storage(tmp_path / "office.sqlite")
    eng = Engine(storage=storage, registry=get_registry(), embedder=NullEmbedder())
    monkeypatch.setattr(engine_module, "_engine", eng)
    yield eng
    eng.close()
    adapter_registry.reset_registry()


def _seed_doc_with_chunks(
    storage: Storage,
    ws: Workspace,
    name: str,
    chunks_text: list[str],
    kind: DocumentKind = DocumentKind.MARKDOWN,
) -> Document:
    path = str(Path(ws.root_path) / name)
    doc = Document(
        id=Document.make_id(ws.id, path),
        workspace_id=ws.id,
        path=path,
        name=name,
        kind=kind,
        size_bytes=sum(len(t) for t in chunks_text),
        mtime=datetime.now(timezone.utc),
        has_extracted_text=True,
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    storage.upsert_document(doc)
    chunks = [
        DocumentChunk(
            id=DocumentChunk.make_id(doc.id, i),
            document_id=doc.id,
            workspace_id=ws.id,
            kind=ChunkKind.PARAGRAPH,
            ordinal=i,
            text=t,
            char_count=len(t),
            extraction_method=ExtractionMethod.NATIVE_TEXT,
        )
        for i, t in enumerate(chunks_text)
    ]
    storage.replace_chunks(doc.id, chunks)
    return doc


def test_extract_contract_sections_persists_section_fields(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    text = (
        "## 第1条 (定義)\n"
        "秘密情報とは……。\n"
        "\n"
        "## 第2条 (義務)\n"
        "3年間開示してはならない。\n"
    )
    doc = _seed_doc_with_chunks(isolated_engine.storage, ws, "nda.md", [text])
    out = extract_contract_sections(doc.id, persist=True)
    assert out["section_count"] == 2
    assert out["persisted"] is True
    assert [s["ordinal"] for s in out["sections"]] == [1, 2]
    assert [s["title"] for s in out["sections"]] == ["定義", "義務"]
    assert all(not s["body_truncated"] for s in out["sections"])

    stored = {f.key: f for f in isolated_engine.storage.get_fields(doc.id)}
    assert stored["section.1.title"].value == "定義"
    assert stored["section.1.heading"].value == "第1条 (定義)"
    assert "秘密情報" in stored["section.1.body"].value
    assert stored["section.2.title"].value == "義務"
    assert "3年間" in stored["section.2.body"].value
    # The truncation flag is only emitted when truncation actually happened.
    assert "section.1.body_truncated" not in stored
    assert "section.2.body_truncated" not in stored


def test_body_cap_truncates_long_clause(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    long_body = "あ" * 5000  # well over the test cap below
    text = f"## 第1条 (長文)\n{long_body}\n\n## 第2条 (短い)\n短い本文。\n"
    doc = _seed_doc_with_chunks(isolated_engine.storage, ws, "long.md", [text])
    out = extract_contract_sections(doc.id, persist=True, body_cap=1000)

    sec1 = out["sections"][0]
    assert sec1["body_truncated"] is True
    assert sec1["body_char_count"] == 5000
    assert len(sec1["body"]) == 1000

    sec2 = out["sections"][1]
    assert sec2["body_truncated"] is False

    stored = {f.key: f for f in isolated_engine.storage.get_fields(doc.id)}
    assert stored["section.1.body_truncated"].value == "true"
    assert len(stored["section.1.body"].value) == 1000


def test_no_clauses_returns_empty_with_preamble(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc_with_chunks(
        isolated_engine.storage, ws, "memo.md", ["This is just a memo, no clauses."]
    )
    out = extract_contract_sections(doc.id)
    assert out["section_count"] == 0
    assert out["sections"] == []
    assert "just a memo" in out["preamble"]


def test_persist_false_leaves_storage_empty(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc_with_chunks(
        isolated_engine.storage, ws, "nda.md", ["## 第1条 (定義)\n本文。\n"]
    )
    out = extract_contract_sections(doc.id, persist=False)
    assert out["section_count"] == 1
    assert out["persisted"] is False
    assert isolated_engine.storage.get_fields(doc.id) == []


def test_sectioning_preserves_existing_invoice_fields(isolated_engine, tmp_path):
    """The merge writer must NOT clobber fields that other extractors
    (e.g. ``workflows.invoice``) wrote earlier on the same doc."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc_with_chunks(
        isolated_engine.storage, ws, "hybrid.md", ["## 第1条 (定義)\n本文。\n"]
    )
    # Pre-seed an invoice field as if extract_invoice_fields had already run.
    isolated_engine.storage.replace_extracted_fields(
        doc.id,
        [
            ExtractedField(
                document_id=doc.id,
                key="invoice_number",
                value="INV-2026-001",
                value_type="id",
                confidence=0.95,
                extractor="invoice.label",
            )
        ],
    )
    out = extract_contract_sections(doc.id, persist=True)
    assert out["section_count"] == 1
    stored = {f.key: f for f in isolated_engine.storage.get_fields(doc.id)}
    assert stored["invoice_number"].value == "INV-2026-001"
    assert stored["section.1.title"].value == "定義"


def test_rerun_replaces_previous_sections(isolated_engine, tmp_path):
    """Re-running over a doc whose clause set shrank must drop the old
    section.* keys for clauses that no longer exist."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc_with_chunks(
        isolated_engine.storage,
        ws,
        "nda.md",
        ["## 第1条 (定義)\nA\n\n## 第2条 (義務)\nB\n\n## 第3条 (準拠法)\nC\n"],
    )
    extract_contract_sections(doc.id, persist=True)
    keys_before = {f.key for f in isolated_engine.storage.get_fields(doc.id)}
    assert "section.3.title" in keys_before

    # Replace the doc with a 1-clause version.
    isolated_engine.storage.replace_chunks(
        doc.id,
        [
            DocumentChunk(
                id=DocumentChunk.make_id(doc.id, 0),
                document_id=doc.id,
                workspace_id=ws.id,
                kind=ChunkKind.PARAGRAPH,
                ordinal=0,
                text="## 第1条 (定義のみ)\nA\n",
                char_count=20,
                extraction_method=ExtractionMethod.NATIVE_TEXT,
            )
        ],
    )
    extract_contract_sections(doc.id, persist=True)
    keys_after = {f.key for f in isolated_engine.storage.get_fields(doc.id)}
    assert "section.1.title" in keys_after
    assert "section.2.title" not in keys_after
    assert "section.3.title" not in keys_after


def test_clauses_spanning_chunks_are_stitched(isolated_engine, tmp_path):
    """A PDF extractor that splits one clause across two chunks must still
    produce a continuous section body once concatenated in ordinal order."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc_with_chunks(
        isolated_engine.storage,
        ws,
        "split.md",
        [
            "## 第1条 (定義)\n本文の前半。",
            "本文の後半。\n\n## 第2条 (義務)\n義務本文。",
        ],
    )
    out = extract_contract_sections(doc.id, persist=True)
    assert out["section_count"] == 2
    assert "前半" in out["sections"][0]["body"]
    assert "後半" in out["sections"][0]["body"]


def test_extract_contract_sections_missing_document(isolated_engine):
    out = extract_contract_sections("doc_does_not_exist", persist=False)
    assert "error" in out


def test_extract_contract_sections_no_chunks(isolated_engine, tmp_path):
    """A document upserted without chunks gets a friendly note, not a crash."""
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
    out = extract_contract_sections(doc.id)
    assert out["sections"] == []
    assert "note" in out
