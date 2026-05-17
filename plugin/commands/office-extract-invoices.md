---
description: Export every invoice in a workspace as one CSV row. (Phase 2)
allowed-tools: ["mcp__office-layer__extract_invoices_to_table"]
---

**Phase 2 workflow — single MCP tool.**

1. Call `extract_invoices_to_table(workspace_id=<id>, output_path=<rel_or_abs_path>)`.
   - Relative paths resolve against the workspace root.
   - A UTC timestamp suffix is appended to the filename, so re-running never
     silently overwrites a previous export.
   - The tool loops `extract_invoice_fields` over every PDF / MD / XLSX / DOCX
     in the workspace, keeps the docs that produced an `invoice_number`, and
     writes a 12-column CSV.
2. The return dict carries `row_count`, `skipped` (with reason per dropped
   doc), `low_confidence_paths`, and the final `output_path` with timestamp.
3. Surface `low_confidence_paths` to the user with the prompt "verify these by
   hand before sending the CSV downstream".
4. If the tool returns `error`: it is one of (workspace not found) /
   (workspace is read-only) / (output path is outside the workspace root).
   None of these should be retried automatically — surface to the user.

Columns (in order): `invoice_number, issue_date, due_date, issuer, recipient,
subtotal, tax, total, currency, payment_account, source_path, confidence_avg`.

Pass `run_extractor=false` only when the fields are already persisted and the
caller just wants the CSV projection.
