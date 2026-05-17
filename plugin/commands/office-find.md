---
description: Find files by name / kind / period without reading their content.
allowed-tools: ["mcp__office-layer__search_files"]
---

Find files matching: $ARGUMENTS

This is filename-and-metadata search only (no body excerpt). Use this when the user wants a list of candidate files quickly, not their contents.

Steps:
1. Call `search_files` with the query — the ranker already weights filename matches highly.
2. Return path + size + mtime + kind. Skip the content preview.
3. If the result count is high, group by directory and ask whether to drill into one.

Phase 1 will add explicit filename-only / metadata-only modes; for now this command uses the same hybrid ranker tuned for filename hits.
