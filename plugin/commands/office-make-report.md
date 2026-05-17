---
description: Generate a Markdown report drawing from one workspace. (Phase 2)
allowed-tools: ["mcp__office-layer__build_evidence_packet", "mcp__office-layer__summarize_folder"]
---

**Phase 2 workflow.**

From `$ARGUMENTS`, get: the report topic, the source workspace, and the target reader.

Steps:
1. Summarise the source workspace via `summarize_folder` for orientation.
2. Build one Evidence Packet per section of the planned outline.
3. Write the report as Markdown in a `drafts/` subfolder of the workspace.
4. Every factual claim cites a source from a packet (file_name + locator).
5. Conclude with "Open questions / low-confidence items" so the user can verify before sharing.
