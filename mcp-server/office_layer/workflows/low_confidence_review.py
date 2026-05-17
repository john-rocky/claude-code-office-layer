"""Low-confidence review — Phase 3.

Walks the workspace's persisted ``extracted_fields`` table for rows with
``confidence < threshold`` and groups them by document so the user can
correct them in one pass instead of re-running every per-doc extractor.

Design notes:

* **Field-level, not chunk-level.** OCR chunk confidence is a separate
  "re-OCR this page" UX; pulling both into the same checklist would
  conflate "the extractor was unsure about a value the user can fix
  inline" with "the upstream text quality was bad". Keep them apart and
  add chunks later if there is real demand.
* **Read-only — no safety gate.** This workflow only reads
  ``extracted_fields`` + ``documents``; nothing is written, so
  :mod:`safety.pretool` is not invoked.
* **Threshold default 0.7** matches the existing
  ``_LOW_CONFIDENCE_FLOOR`` constant used by
  :mod:`workflows.invoices_table` for the summary count. Callers can
  override but the default is meant to align with the rest of the
  pipeline so a sparse invoice that is "low confidence in the CSV" is
  also "low confidence in the review".
* **section.* keys are excluded by the storage query.** Section bodies
  emitted by :mod:`workflows.contract_sections` are pass-through
  extraction, not value inference, so listing them as "needs
  verification" would only add noise.
* **Empty path is a 0-item dict, not an error.** A workspace with no
  low-confidence fields (or no extractions at all) returns the same
  shape with empty groups — Claude / the CLI can branch on
  ``item_count`` without special-casing the absent / empty case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..engine import get_engine
from ..models import Document, ExtractedField

_DEFAULT_THRESHOLD = 0.7
_DEFAULT_LIMIT = 1000


@dataclass(frozen=True)
class LowConfidenceItem:
    """A single field flagged for human review."""

    key: str
    value: str
    confidence: float
    value_type: str
    page_number: int | None = None
    cell_range: str | None = None
    extractor: str = "heuristic"

    @classmethod
    def from_field(cls, f: ExtractedField) -> "LowConfidenceItem":
        return cls(
            key=f.key,
            value=f.value,
            confidence=f.confidence,
            value_type=f.value_type,
            page_number=f.page_number,
            cell_range=f.cell_range,
            extractor=f.extractor,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "value_type": self.value_type,
            "extractor": self.extractor,
        }
        if self.page_number is not None:
            d["page_number"] = self.page_number
        if self.cell_range is not None:
            d["cell_range"] = self.cell_range
        return d


@dataclass
class LowConfidenceGroup:
    """All flagged fields for one document, with file metadata for citations."""

    document_id: str
    file_path: str
    file_name: str
    items: list[LowConfidenceItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "file_path": self.file_path,
            "file_name": self.file_name,
            "item_count": len(self.items),
            "items": [it.to_dict() for it in self.items],
        }


def _group_by_document(
    fields: list[ExtractedField], docs_by_id: dict[str, Document]
) -> list[LowConfidenceGroup]:
    groups: dict[str, LowConfidenceGroup] = {}
    for f in fields:
        doc = docs_by_id.get(f.document_id)
        if doc is None:
            # Document was deleted between the JOIN and now — skip rather
            # than crash; the next index pass will GC the field.
            continue
        g = groups.get(f.document_id)
        if g is None:
            file_path = doc.path
            g = LowConfidenceGroup(
                document_id=f.document_id,
                file_path=file_path,
                file_name=Path(file_path).name,
            )
            groups[f.document_id] = g
        g.items.append(LowConfidenceItem.from_field(f))
    # Stable ordering: by file path so the same workspace always renders
    # in the same order, regardless of insertion timing.
    return sorted(groups.values(), key=lambda x: x.file_path)


def create_low_confidence_review(
    workspace_id: str,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Build a per-document checklist of low-confidence extractions.

    Returns ``{"workspace_id", "threshold", "document_count",
    "item_count", "limit", "truncated", "groups": [<LowConfidenceGroup
    json>]}``. Empty workspace or no flags ⇒ ``document_count = 0`` and
    ``item_count = 0`` with ``groups = []`` (not an error).
    """
    if not 0.0 < threshold <= 1.0:
        return {"error": f"threshold must be in (0.0, 1.0]; got {threshold}"}
    if limit < 1:
        return {"error": f"limit must be >= 1; got {limit}"}

    engine = get_engine()
    ws = engine.workspaces.get(workspace_id)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}

    # We pull (limit + 1) so we can tell the caller whether the result
    # was clipped. The MCP / CLI surface gets to decide whether to
    # ask the user to bump --limit or to page through.
    raw = engine.storage.list_low_confidence_fields(
        workspace_id, threshold=threshold, limit=limit + 1
    )
    truncated = len(raw) > limit
    fields = raw[:limit]

    doc_ids = {f.document_id for f in fields}
    docs_by_id: dict[str, Document] = {}
    for doc_id in doc_ids:
        d = engine.storage.get_document(doc_id)
        if d is not None:
            docs_by_id[doc_id] = d

    groups = _group_by_document(fields, docs_by_id)

    return {
        "workspace_id": workspace_id,
        "threshold": threshold,
        "limit": limit,
        "truncated": truncated,
        "document_count": len(groups),
        "item_count": sum(len(g.items) for g in groups),
        "groups": [g.to_dict() for g in groups],
    }
