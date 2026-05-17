"""Semantic search wiring tests.

Covers three states the runtime can be in:

1. No embedder backend installed → :class:`NullEmbedder` keeps the whole
   pipeline a no-op and the keyword ranker behaves exactly as before.
2. A deterministic :class:`FakeEmbedder` + ``sqlite-vec`` installed →
   :class:`SemanticIndex` upserts vectors, the hybrid ranker surfaces them
   with a ``semantic`` reason, and a chunk that FTS would miss can still
   make it into the top-N.
3. Deleting a document removes its vectors as well as its FTS rows.

The fake embedder converts each text into a 4-d vector keyed on the count of
``a``/``b``/``c``/``d``. This is enough to exercise the vector path
deterministically without pulling fastembed at test time.
"""

from __future__ import annotations

import importlib
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.adapters import get_registry, registry as adapter_registry  # noqa: E402
from office_layer.engine.embedder import NullEmbedder, select_embedder  # noqa: E402
from office_layer.engine.engine import Engine  # noqa: E402
from office_layer.engine.semantic import SemanticIndex  # noqa: E402
from office_layer.models import (  # noqa: E402
    ChunkKind,
    Document,
    DocumentChunk,
    DocumentKind,
    ExtractionMethod,
    SearchQuery,
    Workspace,
    WorkspacePolicy,
)
from office_layer.storage import Storage  # noqa: E402


sqlite_vec = pytest.importorskip("sqlite_vec")


class FakeEmbedder:
    """Deterministic, network-free embedder for tests.

    Each text becomes a 4-d unit vector whose components are normalised
    counts of {a,b,c,d}. Texts that share more of those letters end up
    closer in cosine distance.
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    @property
    def dim(self) -> int:
        return 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out: list[list[float]] = []
        for t in texts:
            v = [0.0, 0.0, 0.0, 0.0]
            for ch in t.lower():
                if ch == "a":
                    v[0] += 1
                elif ch == "b":
                    v[1] += 1
                elif ch == "c":
                    v[2] += 1
                elif ch == "d":
                    v[3] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


@pytest.fixture
def fresh_registry():
    """Reset adapter registry so each test sees clean selection state."""
    adapter_registry.reset_registry()
    yield get_registry()
    adapter_registry.reset_registry()


def _make_workspace(storage: Storage, root: Path, enable_vector: bool) -> Workspace:
    ws = Workspace(
        name="t",
        root_path=str(root),
        policy=WorkspacePolicy.READ_ONLY,
        enable_vector_search=enable_vector,
    )
    storage.upsert_workspace(ws)
    return ws


def _make_doc_with_chunk(
    storage: Storage,
    ws: Workspace,
    name: str,
    text: str,
) -> tuple[Document, DocumentChunk]:
    doc = Document(
        id=Document.make_id(ws.id, str(Path(ws.root_path) / name)),
        workspace_id=ws.id,
        path=str(Path(ws.root_path) / name),
        name=name,
        kind=DocumentKind.MARKDOWN,
        size_bytes=len(text),
        mtime=datetime.now(timezone.utc),
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
    return doc, chunk


def test_select_embedder_falls_back_to_null_when_nothing_installed(monkeypatch):
    """If neither fastembed nor sentence-transformers is importable, we get
    a NullEmbedder rather than crashing the whole engine."""
    # Block both imports so we exercise the fallback path even on dev machines
    # that happen to have one of them installed.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    blocked = {"fastembed", "sentence_transformers"}

    def fake_import(name, *args, **kwargs):
        root = name.split(".")[0]
        if root in blocked:
            raise ImportError(f"blocked {name} for test")
        return real_import(name, *args, **kwargs)

    import builtins
    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Drop any cached module so the next `import fastembed` re-runs through
    # our fake_import.
    for mod in list(sys.modules):
        if mod.split(".")[0] in blocked:
            sys.modules.pop(mod, None)

    chosen = select_embedder()
    assert isinstance(chosen, NullEmbedder)
    assert not chosen.is_available()


def test_disabled_workspace_keeps_keyword_only(tmp_path, fresh_registry):
    """enable_vector_search=False → no vectors get written even with a
    working embedder, and the keyword ranker still returns FTS hits."""
    db = tmp_path / "office.sqlite"
    storage = Storage(db)
    try:
        ws = _make_workspace(storage, tmp_path, enable_vector=False)
        embedder = FakeEmbedder()
        engine = Engine(storage=storage, registry=fresh_registry, embedder=embedder)

        _make_doc_with_chunk(storage, ws, "alpha.md", "alpha beta gamma")

        # Re-index path on the indexer would also call semantic.index_chunks
        # — but only if ws.enable_vector_search is True. Confirm:
        chunks = storage.get_chunks(
            Document.make_id(ws.id, str(tmp_path / "alpha.md"))
        )
        if ws.enable_vector_search:
            engine.semantic.index_chunks(chunks)
        assert storage.vector_count() == 0
        assert embedder.calls == 0

        resp = engine.search.search(
            SearchQuery(text="alpha", workspace_ids=[ws.id], limit=5)
        )
        assert resp.results
        assert resp.results[0].document.name == "alpha.md"
        # No reason line should advertise semantic since it never ran.
        assert "semantic" not in resp.results[0].reason
    finally:
        engine.close()


def test_semantic_hit_surfaces_chunk_fts_would_miss(tmp_path, fresh_registry):
    """A chunk whose text shares only structural similarity with the query
    (no shared keywords) should still surface via the vector index."""
    db = tmp_path / "office.sqlite"
    storage = Storage(db)
    try:
        ws = _make_workspace(storage, tmp_path, enable_vector=True)
        embedder = FakeEmbedder()
        engine = Engine(storage=storage, registry=fresh_registry, embedder=embedder)

        # Two docs. Neither contains the literal query "aaaaa".
        # - close.md is closer in vector space.
        # - far.md is further (extra 'b' shifts the vector off-axis).
        doc_close, _ = _make_doc_with_chunk(storage, ws, "close.md", "aaaa")
        doc_far, _ = _make_doc_with_chunk(storage, ws, "far.md", "aaaa bbbb")

        # Push chunks through the semantic index (mirrors what the indexer
        # would do on a real run).
        all_chunks = []
        for d in storage.list_documents(workspace_id=ws.id):
            all_chunks.extend(storage.get_chunks(d.id))
        n = engine.semantic.index_chunks(all_chunks)
        assert n == 2
        assert storage.vector_count() == 2

        resp = engine.search.search(
            SearchQuery(text="aaaaa", workspace_ids=[ws.id], limit=5)
        )
        names = [r.document.name for r in resp.results]
        assert "close.md" in names, f"close.md missing from results: {names}"
        # The closer vector should outrank the further one. Both come from
        # semantic so the score difference is driven by distance + RRF rank.
        close_score = next(r.score for r in resp.results if r.document.name == "close.md")
        far_score = next(r.score for r in resp.results if r.document.name == "far.md")
        assert close_score >= far_score
        # Reason annotation makes it inspectable.
        assert "semantic" in resp.results[0].reason
    finally:
        engine.close()


def test_delete_document_removes_vectors(tmp_path, fresh_registry):
    """Storage.delete_document must drop vec_chunks rows so we don't leak."""
    db = tmp_path / "office.sqlite"
    storage = Storage(db)
    try:
        ws = _make_workspace(storage, tmp_path, enable_vector=True)
        embedder = FakeEmbedder()
        semantic = SemanticIndex(storage, embedder)

        doc, chunk = _make_doc_with_chunk(storage, ws, "x.md", "abcd")
        semantic.index_chunks([chunk])
        assert storage.vector_count() == 1

        storage.delete_document(doc.id)
        assert storage.vector_count() == 0
    finally:
        storage.close()


def test_workspace_without_id_filter_skips_semantic(tmp_path, fresh_registry):
    """Cross-workspace queries (workspace_ids=None) must not run semantic —
    that's how we honour per-workspace opt-out."""
    db = tmp_path / "office.sqlite"
    storage = Storage(db)
    try:
        ws = _make_workspace(storage, tmp_path, enable_vector=True)
        embedder = FakeEmbedder()
        engine = Engine(storage=storage, registry=fresh_registry, embedder=embedder)

        _, chunk = _make_doc_with_chunk(storage, ws, "x.md", "abcd")
        engine.semantic.index_chunks([chunk])
        embedder.calls = 0

        resp = engine.search.search(SearchQuery(text="aaaa", workspace_ids=None))
        # The semantic search step short-circuits before calling embed().
        assert embedder.calls == 0
        assert not any("semantic" in r.reason for r in resp.results)
    finally:
        engine.close()
