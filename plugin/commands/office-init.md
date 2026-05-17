---
description: Initialise the Office Layer — pick the folders you want Claude to search.
allowed-tools: ["mcp__office-layer__list_workspaces", "mcp__office-layer__add_workspace", "mcp__office-layer__get_index_status"]
---

You are bootstrapping the Office Layer for the user. Goal: leave them with at least one indexable workspace and a clear next-step.

Steps:
1. Call `get_index_status` to see what adapters / workspaces exist.
2. If no workspaces exist, ask the user which folder to register (suggest `~/Documents`, `~/Desktop`, `~/Downloads`, or a per-project folder). Default policy: `read-only`.
3. Call `add_workspace` with the chosen path. Confirm the workspace ID.
4. Tell the user the next command to run: `/office-index <workspace_id>`.
5. If any adapter is in degraded mode, surface the `install_hint` so the user knows what to `pip install` for fuller coverage.

User instruction: $ARGUMENTS
