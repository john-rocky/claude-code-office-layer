"""Low-confidence review — Phase 3 stub.

Plan:
- Query Storage for chunks with `confidence < threshold` or
  `extraction_method` containing 'ocr'.
- Group by document.
- Emit a checklist: file, page/sheet, snippet, current parsed value,
  "needs verification".
"""

from __future__ import annotations

from typing import Any


def review(workspace_id: str, *, threshold: float = 0.7) -> list[dict[str, Any]]:  # pragma: no cover
    raise NotImplementedError("Phase 3 — see plugin/commands/office-review-low-confidence.md")
