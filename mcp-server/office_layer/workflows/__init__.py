"""Workflow templates — §9.11 + §17.7 independent value.

Phase 0 ships `folder_summary` (wired into the MCP server already). The rest
live as module stubs here so the namespace + import paths are stable from
the start. Phase 2 fills the rest.
"""

from .client_history import build_client_history
from .contract_diff import compare_contracts, compare_sections
from .contract_sections import (
    extract_contract_sections,
    extract_sections_from_text,
)
from .folder_summary import summarize_folder
from .invoice import extract_fields_from_text, extract_invoice_fields
from .invoices_table import (
    COLUMNS as INVOICE_TABLE_COLUMNS,
    build_invoice_rows,
    extract_invoices_to_table,
)

__all__ = [
    "summarize_folder",
    "extract_invoice_fields",
    "extract_fields_from_text",
    "build_client_history",
    "extract_contract_sections",
    "extract_sections_from_text",
    "compare_contracts",
    "compare_sections",
    "extract_invoices_to_table",
    "build_invoice_rows",
    "INVOICE_TABLE_COLUMNS",
]
