---
description: Inspect / change workspace settings (policy, OCR, semantic search, exclude globs).
allowed-tools: ["mcp__office-layer__list_workspaces", "mcp__office-layer__update_workspace_policy", "mcp__office-layer__set_workspace_vector_search", "mcp__office-layer__get_workspace_status", "mcp__office-layer__get_index_status"]
---

Adjust Office Layer workspace settings: $ARGUMENTS

Steps:
1. If no workspace given, list and ask.
2. Currently editable from this command:
   - policy → `update_workspace_policy(workspace_id, "read-only" | "draft-write" | "full-write")`
   - semantic search → `set_workspace_vector_search(workspace_id, true|false)`. After flipping ON, prompt the user to re-run `/office-index <workspace_id>` so existing chunks get embedded. If `get_index_status()` reports `semantic_ready=false`, tell the user the backend is missing (`pip install 'claude-code-office-layer[vec-sqlite]' fastembed`).
3. Confirm changes back to the user explicitly. Read-only is the safe default; warn before moving any workspace to `full-write`.
