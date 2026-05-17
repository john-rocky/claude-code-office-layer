---
description: Draft a reply email grounded in past correspondence + contract context. (Phase 2)
allowed-tools:
  - "mcp__office-layer__build_evidence_packet"
  - "mcp__office-layer__build_client_history"
  - "mcp__office-layer__draft_email_from_evidence"
---

**Phase 2 workflow.**

Steps:
1. From `$ARGUMENTS`, infer: workspace_id, recipient, situation, tone (formal / casual), constraints (e.g. "scope を守る").
2. Build evidence:
   - If a client name is in scope → `build_client_history(client_name)` (returns `evidence_packet`).
   - Otherwise → `build_evidence_packet(intent="draft reply email", query=<recipient + topic>)`.
3. `draft_email_from_evidence(workspace_id, packet=<the evidence_packet dict>, recipient, subject, intent, extra_context)`.
   - The tool stages a markdown skeleton under `<workspace>/drafts/` with citations + a "before sending" checklist already filled with the amounts / dates pulled from the packet's `extracted_fields`.
   - It never writes outside `drafts/`; read-only workspaces refuse it.
4. Rewrite the body inline in the returned `body_markdown`, keeping the citation block + the checklist intact. Quote source clauses verbatim where appropriate.
5. Surface the file path back to the user and remind them the draft has NOT been sent.

Safety: `classify_operation("send_email")` is HIGH. This command never crosses that line — output is a draft `.md` file in the workspace `drafts/` folder. The `draft_email_from_evidence` tool itself runs under `classify_operation("new_draft")` (MEDIUM) and refuses on read-only workspaces.
