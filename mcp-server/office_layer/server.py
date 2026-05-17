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
            "file_watcher": registry.file_watcher.name if registry.file_watcher else None,
        },
        "degraded": [
            {"kind": d.adapter_kind, "reason": d.reason, "install_hint": d.install_hint}
            for d in registry.degraded
        ],
        "workspaces": [w.model_dump(mode="json") for w in engine.workspaces.list()],
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
