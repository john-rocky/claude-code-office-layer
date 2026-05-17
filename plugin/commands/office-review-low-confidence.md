---
description: Surface low-confidence extractions so the user can correct them in one pass. (Phase 3)
allowed-tools: ["mcp__office-layer__list_workspaces", "mcp__office-layer__search_files"]
---

**Phase 3 workflow.**

The Office Layer marks fields / chunks / OCR pages with a confidence score. This command lists everything in a workspace below a threshold so the user can review.

Steps:
1. Resolve the workspace from `$ARGUMENTS`.
2. (Phase 3) Call the dedicated `create_low_confidence_review` tool when it ships.
3. Until then, fall back to: `search_files` over the workspace for documents with `extraction_error`, plus chunks with `confidence < 0.7`.
4. Present a compact checklist: file, page/sheet, snippet, current value, "needs verification?".
