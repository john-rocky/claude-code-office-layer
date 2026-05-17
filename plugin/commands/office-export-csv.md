---
description: Export a normalised CSV from extracted fields (invoices, …). (Phase 2)
allowed-tools: ["mcp__office-layer__extract_invoices_to_table", "mcp__office-layer__classify_operation_risk"]
---

**Phase 2 workflow.**

For invoices, the dedicated tool is `/office-extract-invoices` —
`extract_invoices_to_table` handles the kind filter, the per-doc extractor
loop, the safety gate, the timestamp suffix, and the column projection in one
call. Prefer that over hand-rolling a CSV.

For other artifact types where no dedicated tool exists yet:
1. `classify_operation_risk("export_csv", targets=[<output_path>])`. CSV writes
   are MEDIUM — confirm the output path is inside the workspace's `drafts/`
   subfolder.
2. Build an Evidence Packet covering the rows the user asked for.
3. Map packet fields to columns: file_name, page, …, source_path.
4. Emit the CSV. Never overwrite an existing file — append a timestamp suffix.
5. Print a short report: row count, confidence distribution, files that
   contributed no rows.

NEVER write outside the workspace root.
