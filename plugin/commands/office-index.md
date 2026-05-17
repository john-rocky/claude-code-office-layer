---
description: Build or refresh the search index for a workspace.
allowed-tools: ["mcp__office-layer__list_workspaces", "mcp__office-layer__start_indexing", "mcp__office-layer__get_workspace_status"]
---

Index the workspace `$ARGUMENTS`.

Steps:
1. If `$ARGUMENTS` is empty, call `list_workspaces` and ask the user which one.
2. Call `start_indexing` with the workspace id. This is incremental — unchanged files are skipped automatically.
3. Report counts back to the user: seen / indexed / skipped / errors.
4. If `total_errors > 0`, surface the first few error paths so the user can decide whether they need an optional dep (OCR for scans, OCRmyPDF for image-only PDFs, etc.).
5. Suggest the user run `/office-search "<query>"` to try the index.
