"""Workflow templates — §9.11 + §17.7 independent value.

Phase 0 ships `folder_summary` (wired into the MCP server already). The rest
live as module stubs here so the namespace + import paths are stable from
the start. Phase 2 fills the rest.
"""

from .folder_summary import summarize_folder

__all__ = ["summarize_folder"]
