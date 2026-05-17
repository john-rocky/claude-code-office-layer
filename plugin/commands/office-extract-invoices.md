---
description: Extract invoice fields (issuer, date, amount, due date) from PDFs in a folder. (Phase 2)
allowed-tools: ["mcp__office-layer__build_evidence_packet", "mcp__office-layer__search_files"]
---

**Phase 2 workflow — not yet a single MCP tool.**

Until the dedicated `extract_invoices_to_table` tool ships, use this flow with the Phase 0 building blocks:

1. Call `search_files` with the user's request (e.g. `"請求書 2025 取引先"`) restricted to the relevant workspace.
2. For each top candidate, call `build_evidence_packet(intent="extract invoice fields", query=<file_name>)`.
3. From each packet's extracted text + tables, draft these fields with citations:
   - issuer (取引先)
   - invoice number (請求書番号)
   - invoice date (請求日)
   - due date (支払期限)
   - subtotal / tax / total (小計 / 消費税 / 合計)
   - payment account (振込先)
4. Emit a single Markdown table. For every cell, cite `file_name p.N` (or sheet/cell).
5. List any low-confidence rows separately and tell the user to verify those manually.

Do NOT write CSV or modify files yet — that is a higher-risk operation gated by `/office-export-csv`.
