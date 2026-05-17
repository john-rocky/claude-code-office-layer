"""Folder summary workflow — Phase 0.

Counts documents per kind + recent files. Exposed through MCP as
``summarize_folder``. Kept here too so other workflows (Phase 2 invoice /
contract / etc.) can reuse the same primitive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..engine import get_engine


def summarize_folder(workspace_id: str, *, limit: int = 50) -> dict[str, Any]:
    engine = get_engine()
    ws = engine.workspaces.get(workspace_id)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}
    docs = engine.storage.list_documents(workspace_id=workspace_id, limit=2000)
    by_kind: dict[str, int] = {}
    for d in docs:
        kind_str = d.kind if isinstance(d.kind, str) else d.kind.value
        by_kind[kind_str] = by_kind.get(kind_str, 0) + 1
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
            }
            for d in recent
        ],
    }
