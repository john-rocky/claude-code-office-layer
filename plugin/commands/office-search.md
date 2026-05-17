---
description: Search indexed workspaces with hybrid keyword + filename ranking.
allowed-tools: ["mcp__office-layer__search_files", "mcp__office-layer__list_workspaces"]
---

Run a hybrid search for: $ARGUMENTS

Steps:
1. Call `search_files` with `text=$ARGUMENTS`. Limit defaults to 20.
2. For each result, surface: file name, file path, kind, page/sheet/cell locator, match preview (3 lines max), why it ranked (`reason`), confidence score.
3. If results look thin or wrong, suggest variants: drop a noisy word, narrow to a specific workspace, restrict by kind (PDF / XLSX / DOCX), or ask the user to clarify intent.
4. NEVER fabricate quotes — only quote text returned by the tool.
5. If the user clearly wants an artifact (CSV / draft / report), continue into `/office-make-report` or `/office-export-csv` flow with `build_evidence_packet` as the next step.
