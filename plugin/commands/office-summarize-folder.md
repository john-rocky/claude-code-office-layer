---
description: Quick overview of an indexed workspace — what's in there, recent files, kinds, gaps.
allowed-tools: ["mcp__office-layer__list_workspaces", "mcp__office-layer__summarize_folder"]
---

Summarise the workspace `$ARGUMENTS`.

Steps:
1. If `$ARGUMENTS` is empty, list workspaces and pick the one the user means (ask if ambiguous).
2. Call `summarize_folder`.
3. Report:
   - document count
   - breakdown by kind (PDF / XLSX / DOCX / etc.)
   - top 10 most recent files
   - any extraction error notes
4. If the workspace has no indexed documents yet, suggest running `/office-index <ws>` first.
5. Do not list every file — pick representative samples. The user wants orientation, not a manifest.
