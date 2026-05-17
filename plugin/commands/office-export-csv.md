---
description: Export a normalised CSV from extracted fields (invoices, receipts, etc.). (Phase 2)
allowed-tools: ["mcp__office-layer__build_evidence_packet", "mcp__office-layer__classify_operation_risk"]
---

**Phase 2 workflow.**

Steps:
1. `classify_operation_risk("export_csv", targets=[<output_path>])`. CSV writes are MEDIUM — confirm the output path is inside the workspace's `drafts/` subfolder.
2. Build an Evidence Packet covering the rows the user asked for.
3. Map packet fields to columns: file_name, page, issuer, date, amount, due_date, confidence, source_path.
4. Emit the CSV. Never overwrite an existing file — append a timestamp suffix if needed.
5. Print a short report: row count, confidence distribution, files that contributed no rows.

NEVER write outside the workspace root.
