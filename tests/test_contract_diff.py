"""Tests for Phase 2 contract diff.

Pure-function pass over the pairing + status thresholds, then an
integration pass that wires real indexed NDA fixtures (nda-old vs
nda-new) through ``compare_contracts`` end-to-end.
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
    ExtractionMethod,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402
from office_layer.workflows.contract_diff import (  # noqa: E402
    compare_contracts,
    compare_sections,
)
from office_layer.workflows.contract_sections import (  # noqa: E402
    Section,
    extract_sections_from_text,
)


# -- pure-function helpers ----------------------------------------------------


def _sec(ordinal: int, title: str, body: str, heading: str | None = None) -> Section:
    return Section(
        ordinal=ordinal,
        title=title,
        heading=heading or f"第{ordinal}条 ({title})",
        body=body,
        char_offset=0,
    )


# -- pure-function tests ------------------------------------------------------


def test_identical_clause_lists_have_zero_diff():
    a = [
        _sec(1, "定義", "秘密情報とは……。"),
        _sec(2, "義務", "3年間開示しない。"),
    ]
    b = [
        _sec(1, "定義", "秘密情報とは……。"),
        _sec(2, "義務", "3年間開示しない。"),
    ]
    out = compare_sections(a, b)
    assert out["summary"] == {
        "identical": 2,
        "wording": 0,
        "substantive": 0,
        "added": 0,
        "removed": 0,
    }
    assert all(p["status"] == "identical" for p in out["pairs"])
    assert all(p["diff_hunks"] == [] for p in out["pairs"])


def test_substantive_change_in_term_year():
    """`3年間` → `5年間` is a single-char diff but on a 12-char clause
    body, so the edit ratio crosses the 0.10 threshold easily and the
    status is substantive."""
    a = [_sec(2, "義務", "3年間開示しない。")]
    b = [_sec(2, "義務", "5年間開示しない。")]
    out = compare_sections(a, b)
    assert out["summary"]["substantive"] == 1
    pair = out["pairs"][0]
    assert pair["status"] == "substantive"
    assert pair["edit_ratio"] > 0.10
    # The diff hunk should mention the year tokens on both sides.
    diff_text = "\n".join(pair["diff_hunks"])
    assert "-3年間" in diff_text and "+5年間" in diff_text


def test_wording_change_under_threshold():
    """A long body with a single punctuation change must land as wording,
    not substantive."""
    body_a = "本契約において「秘密情報」とは、開示されるすべての情報をいう。" * 5
    body_b = body_a.replace("「秘密情報」", "「秘密情報 」", 1)
    out = compare_sections([_sec(1, "定義", body_a)], [_sec(1, "定義", body_b)])
    pair = out["pairs"][0]
    assert pair["status"] == "wording"
    assert pair["edit_ratio"] < 0.10
    assert pair["diff_hunks"]  # there IS a diff, just a tiny one


def test_added_clause_in_b_only():
    a = [_sec(1, "定義", "A.")]
    b = [_sec(1, "定義", "A."), _sec(2, "新条項", "新規追加の本文。")]
    out = compare_sections(a, b)
    assert out["summary"]["added"] == 1
    added = [p for p in out["pairs"] if p["status"] == "added"]
    assert len(added) == 1
    assert added[0]["ordinal_a"] is None
    assert added[0]["ordinal_b"] == 2
    assert added[0]["title_b"] == "新条項"


def test_removed_clause_in_a_only():
    a = [_sec(1, "定義", "A."), _sec(2, "削除予定", "古い本文。")]
    b = [_sec(1, "定義", "A.")]
    out = compare_sections(a, b)
    assert out["summary"]["removed"] == 1
    removed = [p for p in out["pairs"] if p["status"] == "removed"]
    assert len(removed) == 1
    assert removed[0]["ordinal_a"] == 2
    assert removed[0]["ordinal_b"] is None
    assert removed[0]["title_a"] == "削除予定"


def test_renumbered_clause_falls_back_to_title_match():
    """B inserts a new §2, so old §2 becomes §3. Ordinal mismatch but
    titles align — title-fallback must pair them."""
    a = [
        _sec(1, "定義", "A."),
        _sec(2, "秘密保持義務", "3年間開示しない。"),
    ]
    b = [
        _sec(1, "定義", "A."),
        _sec(2, "新規条項", "新規。"),
        _sec(3, "秘密保持義務", "5年間開示しない。"),
    ]
    out = compare_sections(a, b)
    # A§2 should pair with B§3 by title; B§2 surfaces as added.
    paired = [p for p in out["pairs"] if p["title_a"] == "秘密保持義務"]
    assert len(paired) == 1
    assert paired[0]["ordinal_a"] == 2
    assert paired[0]["ordinal_b"] == 3
    assert paired[0]["status"] == "substantive"
    assert out["summary"]["added"] == 1


def test_unrelated_titles_are_not_paired():
    """When B's only unmatched clause has a wildly different title, the
    pair must NOT be glued together — old clause surfaces as removed,
    new clause as added."""
    a = [_sec(7, "天変地異", "不可抗力条項。")]
    b = [_sec(7, "知的財産", "IP 条項。")]
    # Identical ordinals would normally pair these — force a mismatch by
    # using different ordinals so we exercise the title-fallback floor.
    a = [_sec(7, "天変地異", "不可抗力条項。")]
    b = [_sec(8, "知的財産", "IP 条項。")]
    out = compare_sections(a, b)
    assert out["summary"]["added"] == 1
    assert out["summary"]["removed"] == 1
    assert out["summary"]["substantive"] == 0


def test_empty_inputs_both_sides():
    out = compare_sections([], [])
    assert out["pairs"] == []
    assert out["summary"] == {
        "identical": 0,
        "wording": 0,
        "substantive": 0,
        "added": 0,
        "removed": 0,
    }


def test_whitespace_only_difference_is_identical():
    """Trailing whitespace / blank-line shifts must not flip a clause to
    wording. The body normaliser strips per-line whitespace + drops
    blank lines."""
    body_a = "本契約は……。\n  \n受領者は……。\n"
    body_b = "本契約は……。\n受領者は……。"
    out = compare_sections([_sec(1, "X", body_a)], [_sec(1, "X", body_b)])
    assert out["pairs"][0]["status"] == "identical"
    assert out["pairs"][0]["diff_hunks"] == []


# -- integration fixture ------------------------------------------------------


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
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
    name: str,
    text: str,
    kind: DocumentKind = DocumentKind.MARKDOWN,
) -> Document:
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


# -- integration tests --------------------------------------------------------


_NDA_OLD = (
    "# 秘密保持契約 (NDA) — Version 1.0\n"
    "\n"
    "本契約は ACME 株式会社 と ジョン・ロッキー との間で締結する。\n"
    "\n"
    "## 第1条 (定義)\n"
    "本契約において「秘密情報」とは、本契約の履行に関連して開示されるすべての情報をいう。\n"
    "\n"
    "## 第2条 (秘密保持義務)\n"
    "受領者は秘密情報を 3年間 第三者に開示してはならない。\n"
    "\n"
    "## 第3条 (損害賠償)\n"
    "本契約違反により生じた損害については、 上限なし で賠償の責任を負う。\n"
    "\n"
    "## 第4条 (準拠法)\n"
    "本契約は日本法に準拠する。\n"
    "\n"
    "## 第5条 (合意管轄)\n"
    "東京地方裁判所を専属的合意管轄とする。\n"
)

_NDA_NEW = (
    "# 秘密保持契約 (NDA) — Version 2.0\n"
    "\n"
    "本契約は ACME 株式会社 と ジョン・ロッキー との間で締結する。\n"
    "\n"
    "## 第1条 (定義)\n"
    "本契約において「秘密情報」とは、本契約の履行に関連して開示されるすべての情報をいう。\n"
    "\n"
    "## 第2条 (秘密保持義務)\n"
    "受領者は秘密情報を 5年間 第三者に開示してはならない。\n"
    "\n"
    "## 第3条 (損害賠償)\n"
    "本契約違反により生じた損害については、 直接損害に限り、契約金額を上限 として賠償の責任を負う。\n"
    "\n"
    "## 第4条 (準拠法)\n"
    "本契約は日本法に準拠する。\n"
    "\n"
    "## 第5条 (合意管轄)\n"
    "東京地方裁判所を専属的合意管轄とする。\n"
)


def test_nda_fixture_substantive_changes_isolated(isolated_engine, tmp_path):
    """The two NDA fixtures differ on §2 (term length) and §3 (liability
    cap). §1 / §4 / §5 are byte-identical. The diff tool must isolate
    the substantive changes to those two clauses and leave the rest as
    identical."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc_old = _seed_doc(isolated_engine.storage, ws, "nda-old.md", _NDA_OLD)
    doc_new = _seed_doc(isolated_engine.storage, ws, "nda-new.md", _NDA_NEW)

    out = compare_contracts(doc_old.id, doc_new.id)
    assert "error" not in out
    assert out["section_count_a"] == 5
    assert out["section_count_b"] == 5
    summary = out["summary"]
    assert summary["substantive"] == 2
    assert summary["identical"] == 3
    assert summary["added"] == 0
    assert summary["removed"] == 0

    by_ordinal = {p["ordinal_a"]: p for p in out["pairs"]}
    # §2: term length flip
    assert by_ordinal[2]["status"] == "substantive"
    diff2 = "\n".join(by_ordinal[2]["diff_hunks"])
    assert "3年間" in diff2 and "5年間" in diff2
    # §3: liability cap change
    assert by_ordinal[3]["status"] == "substantive"
    diff3 = "\n".join(by_ordinal[3]["diff_hunks"])
    assert "上限なし" in diff3
    assert "契約金額" in diff3
    # Unchanged clauses
    assert by_ordinal[1]["status"] == "identical"
    assert by_ordinal[1]["diff_hunks"] == []
    assert by_ordinal[4]["status"] == "identical"
    assert by_ordinal[5]["status"] == "identical"


def test_compare_contracts_missing_doc_a(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc(isolated_engine.storage, ws, "x.md", _NDA_OLD)
    out = compare_contracts("doc_does_not_exist", doc.id)
    assert "error" in out


def test_compare_contracts_missing_doc_b(isolated_engine, tmp_path):
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc = _seed_doc(isolated_engine.storage, ws, "x.md", _NDA_OLD)
    out = compare_contracts(doc.id, "doc_does_not_exist")
    assert "error" in out


def test_compare_contracts_zero_chunks_returns_empty_pairs(isolated_engine, tmp_path):
    """A document that has been upserted but never indexed (no chunks)
    must produce an empty section list, not crash. Both bodies empty
    means every comparison degenerates to ``identical`` for ordinal
    matches — but since there are no sections at all, the pair list is
    empty and the summary is all zeros."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    doc_a = Document(
        id=Document.make_id(ws.id, str(tmp_path / "a.md")),
        workspace_id=ws.id,
        path=str(tmp_path / "a.md"),
        name="a.md",
        kind=DocumentKind.MARKDOWN,
        size_bytes=0,
        mtime=datetime.now(timezone.utc),
    )
    doc_b = Document(
        id=Document.make_id(ws.id, str(tmp_path / "b.md")),
        workspace_id=ws.id,
        path=str(tmp_path / "b.md"),
        name="b.md",
        kind=DocumentKind.MARKDOWN,
        size_bytes=0,
        mtime=datetime.now(timezone.utc),
    )
    isolated_engine.storage.upsert_document(doc_a)
    isolated_engine.storage.upsert_document(doc_b)
    out = compare_contracts(doc_a.id, doc_b.id)
    assert out["section_count_a"] == 0
    assert out["section_count_b"] == 0
    assert out["pairs"] == []
    assert out["summary"]["identical"] == 0


def test_compare_contracts_pairs_clauses_across_chunk_boundaries(isolated_engine, tmp_path):
    """The integration call uses the same chunk-stitch path as
    ``extract_contract_sections`` so a clause split across two chunks
    on the A side must still pair against the equivalent clause on the
    B side."""
    ws = isolated_engine.workspaces.add(
        name="t", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY
    )
    # Doc A: 2 chunks, clause body straddles the boundary.
    path_a = str(tmp_path / "split.md")
    doc_a = Document(
        id=Document.make_id(ws.id, path_a),
        workspace_id=ws.id,
        path=path_a,
        name="split.md",
        kind=DocumentKind.MARKDOWN,
        size_bytes=200,
        mtime=datetime.now(timezone.utc),
        has_extracted_text=True,
        extraction_method=ExtractionMethod.NATIVE_TEXT,
    )
    isolated_engine.storage.upsert_document(doc_a)
    isolated_engine.storage.replace_chunks(
        doc_a.id,
        [
            DocumentChunk(
                id=DocumentChunk.make_id(doc_a.id, 0),
                document_id=doc_a.id,
                workspace_id=ws.id,
                kind=ChunkKind.PARAGRAPH,
                ordinal=0,
                text="## 第1条 (定義)\n本文の前半。",
                char_count=20,
                extraction_method=ExtractionMethod.NATIVE_TEXT,
            ),
            DocumentChunk(
                id=DocumentChunk.make_id(doc_a.id, 1),
                document_id=doc_a.id,
                workspace_id=ws.id,
                kind=ChunkKind.PARAGRAPH,
                ordinal=1,
                text="本文の後半。\n",
                char_count=10,
                extraction_method=ExtractionMethod.NATIVE_TEXT,
            ),
        ],
    )
    # Doc B: same clause, different wording.
    doc_b = _seed_doc(
        isolated_engine.storage,
        ws,
        "single.md",
        "## 第1条 (定義)\n本文の前半。 本文の後半に修正を加えた。\n",
    )
    out = compare_contracts(doc_a.id, doc_b.id)
    assert out["section_count_a"] == 1
    assert out["section_count_b"] == 1
    pair = out["pairs"][0]
    assert pair["ordinal_a"] == 1
    assert pair["ordinal_b"] == 1
    assert pair["status"] in {"wording", "substantive"}
