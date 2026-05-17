---
description: Inspect / change workspace settings (policy, OCR, exclude globs).
allowed-tools: ["mcp__office-layer__list_workspaces", "mcp__office-layer__update_workspace_policy", "mcp__office-layer__get_workspace_status"]
---

Adjust Office Layer workspace settings: $ARGUMENTS

Steps:
1. If no workspace given, list and ask.
2. Currently editable from this command:
   - policy → `update_workspace_policy(workspace_id, "read-only" | "draft-write" | "full-write")`
3. Confirm changes back to the user explicitly. Read-only is the safe default; warn before moving any workspace to `full-write`.
