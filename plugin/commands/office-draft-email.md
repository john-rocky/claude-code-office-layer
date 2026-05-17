---
description: Draft a reply email grounded in past correspondence + contract context. (Phase 2)
allowed-tools: ["mcp__office-layer__build_evidence_packet"]
---

**Phase 2 workflow.**

Steps:
1. From `$ARGUMENTS`, infer: recipient, situation, tone (formal / casual), constraints (e.g. "scope を守る").
2. `build_evidence_packet(intent="draft reply email", query=<recipient + topic>)`.
3. Draft the email IN THE DRAFTS FOLDER ONLY — never send.
4. Cite specific past messages or contract clauses inline where appropriate.
5. End with a checklist for the user: "before sending, confirm: (a) amount X, (b) date Y, (c) attached file Z".

Safety: classify_operation_risk("send_email") is HIGH. This command never crosses that line — output is a draft `.md` file in the workspace `drafts/` folder.
