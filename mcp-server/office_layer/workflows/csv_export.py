"""CSV export — Phase 2 stub.

Plan:
- Run safety classifier on (`export_csv`, [target_path], workspace).
- Refuse target paths outside workspace `drafts/`.
- Map ExtractedField rows + chunk locators into CSV columns.
- Suffix the output path with a timestamp instead of overwriting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def export(workspace_id: str, target_path: str | Path, rows: list[dict[str, Any]]) -> Path:  # pragma: no cover
    raise NotImplementedError("Phase 2 — see plugin/commands/office-export-csv.md")
