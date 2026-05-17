---
name: office-search-skill
description: Use whenever the user asks something that needs to consult their indexed Office workspaces — "find...", "look up...", "what did we send to...", "summarise the folder...". Routes the request through the document-finder → evidence-builder pipeline.
---

# Office Search Skill

When this skill is active:

1. **Don't grep the filesystem.** Use `mcp__office-layer__search_files`. Even if the user gives a file path, prefer the indexed query — it surfaces relevant sibling files.
2. **Expand the query.** Company name → likely 略称 + email domain. Period like "去年" → concrete date range. File kind hints → restrict via `kinds=` only when the user clearly meant a specific kind.
3. **Always show locators.** Page, sheet, cell — that is the value proposition. Never quote without one.
4. **Hand off cleanly.** If the user wants an artifact (CSV / draft / report) build an Evidence Packet first; do not write content from raw search hits.
5. **Surface degraded mode.** If status shows OCR is off and the user is asking about scanned PDFs, tell them the install hint.

## Examples

User: "去年のA社の見積を全部見つけて"
→ expand to `見積 見積書 estimate quote A社 [aliases] 2024` → search_files → list with file paths + pages → ask if they want an Evidence Packet to draft a follow-up.

User: "経理フォルダにあるはずの請求書のCSVが欲しい"
→ /office-summarize-folder first (orientation) → /office-extract-invoices flow (Phase 2 fallback) → safety-reviewer before any CSV write.
