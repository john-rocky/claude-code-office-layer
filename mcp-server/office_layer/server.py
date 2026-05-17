"""MCP server entrypoint — FastMCP wrapper around Engine.

Exposes the Phase 0 tools from spec §9.7.2. Tool functions are thin: parse
args, call the engine, log to audit, return JSON-safe dicts. The Engine /
adapters do all the heavy lifting so the server stays under 500 lines.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .engine import get_engine
from .engine.evidence import EvidencePacketBuilder
from .models import (
    DocumentKind,
    OperationRiskLevel,
    SearchMode,
    SearchQuery,
    WorkspacePolicy,
)
from .safety import AuditLogger, classify_operation
from .workflows import (
    build_client_history as _build_client_history,
    compare_contracts as _compare_contracts,
    create_low_confidence_review as _create_low_confidence_review,
    draft_email_from_evidence as _draft_email_from_evidence,
    extract_contract_sections as _extract_contract_sections,
    extract_invoice_fields as _extract_invoice_fields,
    extract_invoices_to_table as _extract_invoices_to_table,
)

log = logging.getLogger(__name__)


def _server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit(
            "mcp package not installed. Run `pip install claude-code-office-layer`"
            " or `pip install mcp pydantic pdfplumber python-docx openpyxl python-pptx watchdog`."
        ) from exc
    return FastMCP("office-layer")


mcp = _server()


# -- helpers ------------------------------------------------------------------


def _audit() -> AuditLogger:
    return AuditLogger(get_engine().storage)


def _workspace_or_error(workspace_id: str) -> dict:
    ws = get_engine().workspaces.get(workspace_id)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}
    return ws.model_dump(mode="json")


# -- workspace tools ----------------------------------------------------------


@mcp.tool()
def list_workspaces() -> list[dict]:
    """List all registered Office Layer workspaces."""
    engine = get_engine()
    return [w.model_dump(mode="json") for w in engine.workspaces.list()]


@mcp.tool()
def add_workspace(
    name: str,
    root_path: str,
    *,
    policy: str = "read-only",
    include_extensions: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    enable_ocr: bool = False,
    enable_vector_search: bool = False,
    max_file_size_mb: int = 100,
) -> dict:
    """Register a folder as an indexable workspace.

    The Office Layer will only search files inside this folder. ``policy``
    controls write permissions: read-only / draft-write / full-write.
    """
    engine = get_engine()
    try:
        wp = WorkspacePolicy(policy)
    except ValueError:
        return {"error": f"invalid policy '{policy}'; expected read-only|draft-write|full-write"}
    ws = engine.workspaces.add(
        name=name,
        root_path=root_path,
        policy=wp,
        include_extensions=include_extensions,
        exclude_globs=exclude_globs,
        enable_ocr=enable_ocr,
        enable_vector_search=enable_vector_search,
        max_file_size_mb=max_file_size_mb,
    )
    _audit().record(
        "add_workspace",
        tool="add_workspace",
        referenced_files=[ws.root_path],
        extra={"workspace_id": ws.id, "policy": policy},
    )
    return ws.model_dump(mode="json")


@mcp.tool()
def remove_workspace(workspace_id: str) -> dict:
    """Forget a workspace. Files on disk are untouched; only Office Layer state is removed."""
    engine = get_engine()
    ok = engine.workspaces.remove(workspace_id)
    _audit().record(
        "remove_workspace",
        tool="remove_workspace",
        extra={"workspace_id": workspace_id, "removed": ok},
    )
    return {"removed": ok, "workspace_id": workspace_id}


@mcp.tool()
def update_workspace_policy(workspace_id: str, policy: str) -> dict:
    """Change a workspace's read/write policy."""
    try:
        wp = WorkspacePolicy(policy)
    except ValueError:
        return {"error": f"invalid policy '{policy}'"}
    ws = get_engine().workspaces.update_policy(workspace_id, wp)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}
    _audit().record(
        "update_workspace_policy",
        tool="update_workspace_policy",
        extra={"workspace_id": workspace_id, "policy": policy},
    )
    return ws.model_dump(mode="json")


@mcp.tool()
def set_workspace_vector_search(workspace_id: str, enabled: bool) -> dict:
    """Toggle semantic (vector) search on a workspace.

    Returns an info note if the embedder + sqlite-vec backend is not loaded —
    in that case the flag is stored but no vectors get indexed. Re-index the
    workspace (``start_indexing(..., force=true)``) after switching on.
    """
    engine = get_engine()
    ws = engine.workspaces.set_vector_search(workspace_id, bool(enabled))
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}
    _audit().record(
        "set_workspace_vector_search",
        tool="set_workspace_vector_search",
        extra={"workspace_id": workspace_id, "enabled": bool(enabled)},
    )
    out = ws.model_dump(mode="json")
    out["semantic_backend_ready"] = engine.semantic.is_available()
    if enabled and not engine.semantic.is_available():
        out["note"] = (
            "Flag stored, but no embedder + sqlite-vec backend is loaded. "
            "Install with `pip install 'claude-code-office-layer[vec-sqlite]' fastembed`."
        )
    return out


@mcp.tool()
def get_workspace_status(workspace_id: str) -> dict:
    """Return indexing status and document count for a workspace."""
    return _workspace_or_error(workspace_id)


# -- indexing tools -----------------------------------------------------------


@mcp.tool()
def start_indexing(workspace_id: str, *, force: bool = False, max_files: int | None = None) -> dict:
    """Scan a workspace and build/update its search index.

    Incremental by default — files with unchanged mtime+size are skipped.
    Pass ``force=true`` to re-extract everything.
    """
    engine = get_engine()
    if engine.workspaces.get(workspace_id) is None:
        return {"error": f"workspace '{workspace_id}' not found"}
    progress = engine.indexer.index_workspace(workspace_id, force=force, max_files=max_files)
    _audit().record(
        "start_indexing",
        tool="start_indexing",
        extra={
            "workspace_id": workspace_id,
            "indexed": progress.total_indexed,
            "skipped": progress.total_skipped,
            "errors": progress.total_errors,
        },
    )
    return {
        "workspace_id": workspace_id,
        "total_seen": progress.total_seen,
        "total_indexed": progress.total_indexed,
        "total_skipped": progress.total_skipped,
        "total_errors": progress.total_errors,
        "errors": progress.errors[:25],
        "started_at": progress.started_at.isoformat(),
        "finished_at": progress.finished_at.isoformat() if progress.finished_at else None,
    }


@mcp.tool()
def start_watch(workspace_id: str) -> dict:
    """Start background incremental indexing for a workspace.

    Watchdog re-indexes files as they change. Debounced 2s. Stop with
    ``stop_watch``. Watchers stop automatically when the MCP server exits.
    """
    engine = get_engine()
    try:
        handle = engine.background.start(workspace_id)
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}
    _audit().record("start_watch", tool="start_watch", extra={"workspace_id": workspace_id})
    return {
        "workspace_id": handle.workspace_id,
        "started_at": handle.started_at.isoformat(),
    }


@mcp.tool()
def stop_watch(workspace_id: str) -> dict:
    """Stop background watching for a workspace."""
    ok = get_engine().background.stop(workspace_id)
    _audit().record(
        "stop_watch", tool="stop_watch", extra={"workspace_id": workspace_id, "stopped": ok}
    )
    return {"workspace_id": workspace_id, "stopped": ok}


@mcp.tool()
def list_watches() -> list[dict]:
    """List active background watchers."""
    return get_engine().background.list_watches()


@mcp.tool()
def get_index_status() -> dict:
    """Return adapter availability and per-workspace indexing state."""
    engine = get_engine()
    registry = engine.registry
    return {
        "adapters": {
            "file_discovery": registry.file_discovery.name,
            "pdf": registry.pdf.name if registry.pdf else None,
            "office": [a.name for a in registry.office],
            "text": [a.name for a in registry.text],
            "ocr": registry.ocr.name if registry.ocr else None,
            "semantic_search": registry.semantic_search.name if registry.semantic_search else None,
            "embedder": engine.embedder.name,
            "semantic_ready": engine.semantic.is_available(),
            "file_watcher": registry.file_watcher.name if registry.file_watcher else None,
        },
        "degraded": [
            {"kind": d.adapter_kind, "reason": d.reason, "install_hint": d.install_hint}
            for d in registry.degraded
        ],
        "workspaces": [w.model_dump(mode="json") for w in engine.workspaces.list()],
        "watches": engine.background.list_watches(),
    }


# -- search tools -------------------------------------------------------------


@mcp.tool()
def search_files(
    text: str,
    *,
    workspace_ids: list[str] | None = None,
    kinds: list[str] | None = None,
    limit: int = 20,
) -> dict:
    """Hybrid search over indexed content + filenames.

    Returns ranked results with file paths, page/sheet/cell locators, and the
    matched chunk text. Use ``build_evidence_packet`` to convert these into a
    minimal grounded context for downstream drafting.
    """
    engine = get_engine()
    parsed_kinds: list[DocumentKind] | None = None
    if kinds:
        parsed_kinds = []
        for k in kinds:
            try:
                parsed_kinds.append(DocumentKind(k))
            except ValueError:
                continue
    query = SearchQuery(
        text=text,
        workspace_ids=workspace_ids,
        kinds=parsed_kinds,
        limit=limit,
        mode=SearchMode.HYBRID,
    )
    response = engine.search.search(query)
    return {
        "query": query.model_dump(mode="json"),
        "total": response.total,
        "elapsed_ms": response.elapsed_ms,
        "results": [r.model_dump(mode="json") for r in response.results],
    }


@mcp.tool()
def search_content(text: str, *, workspace_id: str | None = None, limit: int = 20) -> dict:
    """Convenience: search restricted to one workspace's body text only."""
    return search_files(
        text,
        workspace_ids=[workspace_id] if workspace_id else None,
        limit=limit,
    )


# -- extraction tools ---------------------------------------------------------


@mcp.tool()
def extract_document_text(file_path: str, *, max_chars: int = 50000) -> dict:
    """Return all extracted chunks for one indexed document, by path or document id."""
    engine = get_engine()
    p = Path(file_path).expanduser().resolve()
    # Try by path under any workspace
    ws_list = engine.workspaces.list()
    doc = None
    for ws in ws_list:
        doc = engine.storage.get_document_by_path(ws.id, str(p))
        if doc:
            break
    if doc is None:
        # Maybe caller passed a document id
        doc = engine.storage.get_document(file_path)
    if doc is None:
        return {"error": f"document not found: {file_path}"}
    chunks = engine.storage.get_chunks(doc.id)
    total = 0
    out_chunks = []
    for c in chunks:
        if total + c.char_count > max_chars:
            break
        out_chunks.append(c.model_dump(mode="json"))
        total += c.char_count
    return {
        "document": doc.model_dump(mode="json"),
        "chunks": out_chunks,
        "truncated": len(out_chunks) < len(chunks),
    }


# -- evidence tools -----------------------------------------------------------


@mcp.tool()
def build_evidence_packet(
    intent: str,
    *,
    query: str,
    workspace_ids: list[str] | None = None,
    kinds: list[str] | None = None,
    max_sources: int = 8,
) -> dict:
    """Search + package the top results as an Evidence Packet for Claude.

    ``intent`` is a short description of what Claude will do with the packet
    (e.g. "draft invoice follow-up email to client X"). The packet contains
    the minimal grounded context: file path, page/sheet/cell, extracted text,
    confidence, suggested next action.
    """
    engine = get_engine()
    parsed_kinds: list[DocumentKind] | None = None
    if kinds:
        parsed_kinds = []
        for k in kinds:
            try:
                parsed_kinds.append(DocumentKind(k))
            except ValueError:
                continue
    q = SearchQuery(
        text=query,
        workspace_ids=workspace_ids,
        kinds=parsed_kinds,
        limit=max(max_sources * 2, 16),
        mode=SearchMode.HYBRID,
    )
    response = engine.search.search(q)
    builder = EvidencePacketBuilder(engine.storage)
    packet = builder.build(intent=intent, results=response, max_sources=max_sources)
    _audit().record(
        "build_evidence_packet",
        tool="build_evidence_packet",
        user_request=intent,
        referenced_files=[s.file_path for s in packet.sources],
        extra={"packet_id": packet.packet_id, "source_count": len(packet.sources)},
    )
    return {
        "packet": packet.model_dump(mode="json"),
        "summary_markdown": packet.summary_markdown(),
    }


# -- workflow tools -----------------------------------------------------------


def _resolve_document_id(file_or_id: str) -> str | None:
    """Accept either a document_id (`doc_xxx`) or a filesystem path."""
    engine = get_engine()
    doc = engine.storage.get_document(file_or_id)
    if doc is not None:
        return doc.id
    p = Path(file_or_id).expanduser().resolve()
    for ws in engine.workspaces.list():
        match = engine.storage.get_document_by_path(ws.id, str(p))
        if match is not None:
            return match.id
    return None


@mcp.tool()
def extract_invoice_fields(document: str, *, persist: bool = True) -> dict:
    """Pull invoice fields (issuer / recipient / dates / amounts / account) from one indexed doc.

    ``document`` is either a ``doc_xxx`` id or the file path of an already-
    indexed document. Returns the extracted fields with confidence scores;
    ``persist=true`` (default) also writes them to the storage so the next
    ``build_evidence_packet`` call surfaces them under ``extracted_fields``.
    """
    doc_id = _resolve_document_id(document)
    if doc_id is None:
        return {"error": f"document not found (path or id): {document}"}
    result = _extract_invoice_fields(doc_id, persist=persist)
    _audit().record(
        "extract_invoice_fields",
        tool="extract_invoice_fields",
        referenced_files=[result.get("document", {}).get("path", document)],
        extra={
            "document_id": doc_id,
            "field_count": result.get("field_count", 0),
            "avg_confidence": result.get("avg_confidence", 0.0),
            "persisted": bool(persist),
        },
    )
    return result


@mcp.tool()
def build_client_history(
    client_name: str,
    *,
    workspace_ids: list[str] | None = None,
    aliases: list[str] | None = None,
    timeline_limit: int = 50,
    evidence_max_sources: int = 5,
) -> dict:
    """Aggregate one client's documents across indexed workspaces.

    Returns invoice / contract / email / note counts, total invoiced amount
    grouped by currency, a chronological timeline of all hits, and a trimmed
    Evidence Packet ready for the next workflow step (draft follow-up email,
    compare contracts, etc.).

    ``client_name`` is any of: company name (``"ACME 株式会社"``), short stem
    (``"ACME"``), email domain (``"acme.co.jp"``), or contact email. The
    extractor strips corporate suffixes and expands the input into search
    variants internally; pass extra spelling variants via ``aliases`` if the
    client uses a project codename or katakana spelling the heuristic would
    not find.
    """
    result = _build_client_history(
        client_name,
        workspace_ids=workspace_ids,
        aliases=aliases,
        timeline_limit=timeline_limit,
        evidence_max_sources=evidence_max_sources,
    )
    _audit().record(
        "build_client_history",
        tool="build_client_history",
        extra={
            "client": client_name,
            "document_count": result.get("document_count", 0),
            "invoice_count": result.get("invoice_count", 0),
            "contract_count": result.get("contract_count", 0),
            "workspace_ids": result.get("workspace_ids", []),
        },
    )
    return result


@mcp.tool()
def extract_contract_sections(
    document: str,
    *,
    persist: bool = True,
    body_cap: int = 4096,
) -> dict:
    """Cut one indexed contract into clauses (`第N条` / `Section N` / `Article N`).

    ``document`` is either a ``doc_xxx`` id or the file path of an already-
    indexed document. Returns each section's ordinal / parsed title / full
    heading / body / char offset, plus the preamble text before the first
    clause. ``persist=true`` (default) also writes ``section.{n}.{title,
    heading, body, body_truncated}`` extracted-field rows so a follow-up
    ``compare_contracts`` or evidence-packet call can reach them. Existing
    non-``section.*`` fields on the document (e.g. invoice fields) are not
    disturbed.
    """
    doc_id = _resolve_document_id(document)
    if doc_id is None:
        return {"error": f"document not found (path or id): {document}"}
    result = _extract_contract_sections(doc_id, persist=persist, body_cap=body_cap)
    _audit().record(
        "extract_contract_sections",
        tool="extract_contract_sections",
        referenced_files=[result.get("document", {}).get("path", document)],
        extra={
            "document_id": doc_id,
            "section_count": result.get("section_count", 0),
            "persisted": bool(persist),
        },
    )
    return result


@mcp.tool()
def extract_invoices_to_table(
    workspace_id: str,
    output_path: str,
    *,
    run_extractor: bool = True,
) -> dict:
    """Export every invoice in ``workspace_id`` as one CSV row.

    Loops the invoice extractor over each PDF / MD / XLSX / DOCX in the
    workspace, keeps the docs that produced an ``invoice_number``, and
    writes a 12-column CSV (invoice_number, issue_date, due_date, issuer,
    recipient, subtotal, tax, total, currency, payment_account,
    source_path, confidence_avg). Relative ``output_path`` is resolved
    against the workspace root; a UTC timestamp suffix is appended to the
    filename so re-running never silently overwrites a previous export.
    Refuses to run on read-only workspaces or write outside the workspace
    root. Pass ``run_extractor=false`` if the fields are already
    populated and you only want the table projection.
    """
    result = _extract_invoices_to_table(
        workspace_id, output_path, run_extractor=run_extractor
    )
    _audit().record(
        "extract_invoices_to_table",
        tool="extract_invoices_to_table",
        output_files=[result["output_path"]] if result.get("output_path") else [],
        extra={
            "workspace_id": workspace_id,
            "row_count": result.get("row_count", 0),
            "candidate_count": result.get("candidate_count", 0),
            "skipped_count": result.get("skipped_count", 0),
            "low_confidence_count": result.get("low_confidence_count", 0),
            "ran_extractor": bool(run_extractor),
            "error": result.get("error"),
        },
    )
    return result


@mcp.tool()
def draft_email_from_evidence(
    workspace_id: str,
    *,
    packet: dict,
    recipient: str,
    subject: str | None = None,
    intent: str | None = None,
    extra_context: str | None = None,
    language: str = "auto",
) -> dict:
    """Stage an evidence-grounded reply email under ``<workspace>/drafts/``.

    ``packet`` is the dict returned by ``build_evidence_packet`` or
    ``build_client_history`` — pass it through unchanged. The body is a
    skeleton with citations + a "before sending" checklist; the parent
    agent (Claude) is expected to rewrite the prose grounded in the
    same packet. Writes a timestamped ``.md`` file under the workspace
    ``drafts/`` directory and refuses to leave that directory. Read-
    only workspaces refuse the operation outright. ``language="auto"``
    detects Japanese vs English from the packet content.
    """
    result = _draft_email_from_evidence(
        workspace_id,
        packet=packet,
        recipient=recipient,
        subject=subject,
        intent=intent,
        extra_context=extra_context,
        language=language,
    )
    _audit().record(
        "draft_email_from_evidence",
        tool="draft_email_from_evidence",
        output_files=[result["output_path"]] if result.get("output_path") else [],
        extra={
            "workspace_id": workspace_id,
            "recipient": recipient,
            "draft_id": result.get("draft_id"),
            "packet_id": result.get("packet_id"),
            "citation_count": result.get("citation_count", 0),
            "checklist_item_count": result.get("checklist_item_count", 0),
            "language": result.get("language"),
            "error": result.get("error"),
        },
    )
    return result


@mcp.tool()
def compare_contracts(doc_a: str, doc_b: str) -> dict:
    """Diff two indexed contract documents clause-by-clause.

    ``doc_a`` / ``doc_b`` are each either a ``doc_xxx`` id or the file
    path of an already-indexed document. Returns a list of ``pairs``
    (one per matched clause + unmatched leftovers) and a ``summary``
    counter of ``identical / wording / substantive / added / removed``.
    Pairing prefers ordinal match (`第N条` lines up across versions); the
    fallback is greedy title edit-distance. Status thresholds: identical
    = byte-equal after whitespace strip, wording = char-edit ratio < 0.10,
    substantive = ≥0.10. Each non-identical pair carries unified-diff
    hunks for the clause body. Nothing is persisted — the diff is a
    one-shot comparison artifact.
    """
    a_id = _resolve_document_id(doc_a)
    if a_id is None:
        return {"error": f"document not found (path or id): {doc_a}"}
    b_id = _resolve_document_id(doc_b)
    if b_id is None:
        return {"error": f"document not found (path or id): {doc_b}"}
    result = _compare_contracts(a_id, b_id)
    if "error" not in result:
        _audit().record(
            "compare_contracts",
            tool="compare_contracts",
            referenced_files=[
                result.get("doc_a", {}).get("path", doc_a),
                result.get("doc_b", {}).get("path", doc_b),
            ],
            extra={
                "doc_a_id": a_id,
                "doc_b_id": b_id,
                "section_count_a": result.get("section_count_a", 0),
                "section_count_b": result.get("section_count_b", 0),
                "summary": result.get("summary", {}),
            },
        )
    return result


@mcp.tool()
def summarize_folder(workspace_id: str, *, limit: int = 50) -> dict:
    """Return a quick overview of an indexed workspace — file counts by kind, recent files."""
    engine = get_engine()
    ws = engine.workspaces.get(workspace_id)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}
    docs = engine.storage.list_documents(workspace_id=workspace_id, limit=2000)
    by_kind: dict[str, int] = {}
    for d in docs:
        by_kind[d.kind if isinstance(d.kind, str) else d.kind.value] = (
            by_kind.get(d.kind if isinstance(d.kind, str) else d.kind.value, 0) + 1
        )
    recent = sorted(docs, key=lambda d: d.mtime, reverse=True)[:limit]
    return {
        "workspace": ws.model_dump(mode="json"),
        "document_count": len(docs),
        "by_kind": by_kind,
        "recent": [
            {
                "name": d.name,
                "path": d.path,
                "kind": d.kind if isinstance(d.kind, str) else d.kind.value,
                "size_bytes": d.size_bytes,
                "mtime": d.mtime.isoformat(),
                "indexed": d.indexed_at.isoformat() if d.indexed_at else None,
            }
            for d in recent
        ],
    }


@mcp.tool()
def create_low_confidence_review(
    workspace_id: str,
    *,
    threshold: float = 0.7,
    limit: int = 1000,
) -> dict:
    """List every extracted field in the workspace below the confidence threshold.

    Walks the persisted ``extracted_fields`` table for ``workspace_id``,
    groups the flagged rows by document, and returns a checklist shape:
    ``{workspace_id, threshold, limit, truncated, document_count,
    item_count, groups: [{document_id, file_path, file_name, item_count,
    items: [...]}]}``. Read-only — no safety gate. ``section.*`` keys
    are excluded; OCR chunk confidence is intentionally out of scope.
    """
    result = _create_low_confidence_review(
        workspace_id, threshold=threshold, limit=limit
    )
    _audit().record(
        "create_low_confidence_review",
        tool="create_low_confidence_review",
        extra={
            "workspace_id": workspace_id,
            "threshold": threshold,
            "document_count": result.get("document_count", 0),
            "item_count": result.get("item_count", 0),
            "truncated": result.get("truncated", False),
            "error": result.get("error"),
        },
    )
    return result


# -- safety tools -------------------------------------------------------------


@mcp.tool()
def classify_operation_risk(
    operation: str,
    *,
    targets: list[str] | None = None,
    workspace_id: str | None = None,
) -> dict:
    """Classify an operation as low/medium/high risk before Claude executes it."""
    engine = get_engine()
    workspace = engine.workspaces.get(workspace_id) if workspace_id else None
    risk = classify_operation(operation, targets=targets, workspace=workspace)
    return risk.model_dump(mode="json")


@mcp.tool()
def recent_audit_log(limit: int = 50) -> list[dict]:
    """Return the latest audit log entries."""
    entries = _audit().recent(limit=limit)
    return [e.model_dump(mode="json") for e in entries]


# -- entrypoint ---------------------------------------------------------------


def run() -> None:
    """Console-script entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    run()
