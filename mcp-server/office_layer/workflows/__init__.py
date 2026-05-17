"""Workflow templates — §9.11 + §17.7 independent value.

Phase 0 ships `folder_summary` (wired into the MCP server already). The rest
live as module stubs here so the namespace + import paths are stable from
the start. Phase 2 fills the rest.
"""

from .client_history import build_client_history
from .folder_summary import summarize_folder
from .invoice import extract_fields_from_text, extract_invoice_fields

__all__ = [
    "summarize_folder",
    "extract_invoice_fields",
    "extract_fields_from_text",
    "build_client_history",
]
