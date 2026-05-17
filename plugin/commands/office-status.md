---
description: Show adapter availability, workspaces, and degraded modes.
allowed-tools: ["mcp__office-layer__get_index_status"]
---

Show full Office Layer status.

Call `get_index_status` and report:

- which adapter is selected for each kind (file_discovery / pdf / office / text / ocr / semantic_search / file_watcher)
- which adapters are in degraded mode and the exact `pip install …` line that would fix each one
- per-workspace state: id, name, path, policy, status, document_count

If OCR is disabled and the user has scanned PDFs, suggest `brew install tesseract && pip install 'claude-code-office-layer[ocr]'`. If semantic search is disabled, suggest `pip install 'claude-code-office-layer[vec-sqlite]'`.

Do not invent state — only repeat what the tool returned.
