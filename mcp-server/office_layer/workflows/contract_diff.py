"""Contract diff — Phase 2 stub.

Plan:
- Parse clause boundaries from extracted chunks (numbered headings / 第N条).
- Pair clauses across versions by heading similarity (cosine of TF-IDF, then
  fall back to fuzzy string match).
- Classify each pair: identical / wording / substantive / added / removed.
- Rank substantive diffs by topic priority (payment > term > liability > IP
  > confidentiality > governing law > misc).
- Output a Markdown diff report with citations.
"""

from __future__ import annotations

from typing import Any


def compare(doc_a_id: str, doc_b_id: str) -> dict[str, Any]:  # pragma: no cover - stub
    raise NotImplementedError("Phase 2 — see plugin/commands/office-contract-diff.md")
