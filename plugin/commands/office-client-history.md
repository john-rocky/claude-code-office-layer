---
description: Build a chronological history of all materials related to a client. (Phase 2)
allowed-tools: ["mcp__office-layer__search_files", "mcp__office-layer__build_evidence_packet", "mcp__office-layer__summarize_folder"]
---

**Phase 2 workflow.**

User intent from `$ARGUMENTS`: a client name, alias, domain, or contact.

Steps:
1. Expand the query (Japanese 略称 / English short form / email domain) before searching.
2. `search_files` across all workspaces with that expansion.
3. Group hits by kind: 契約 / 見積 / 請求 / 議事録 / メール / その他.
4. For each bucket, build an Evidence Packet summarising the top 3 documents.
5. Combine into a chronology — earliest to latest — with citations.
6. Flag gaps: e.g. "見積はあるが対応する請求書は見当たらない" (un-invoiced work candidate).
