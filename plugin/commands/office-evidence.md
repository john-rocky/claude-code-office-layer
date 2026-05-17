---
description: Build an Evidence Packet — minimal grounded context for downstream drafting.
allowed-tools: ["mcp__office-layer__build_evidence_packet", "mcp__office-layer__list_workspaces"]
---

Build an Evidence Packet for: $ARGUMENTS

The user described an intent (what they want to do) and probably a query (which files / content to ground on). Parse the message:

- `intent` — what work the packet will be used for (draft email, extract invoices to CSV, compare contracts, etc.)
- `query` — keywords / company / period / file kind hints

Steps:
1. Call `build_evidence_packet(intent=<intent>, query=<query>)`.
2. Show the `summary_markdown` to the user.
3. Highlight `low_confidence_items` separately — these are the rows the user must verify before any artifact is produced.
4. Suggest the next step matching the intent (e.g. for "draft email": next call should be `office-draft-email` flow; for "invoice CSV": `office-export-csv`). These follow-up commands arrive in Phase 2 — for now, you can draft inline using the packet as ground truth.

NEVER quote content that did not come back inside the packet's sources.
