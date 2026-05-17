"""Invoice workflow — Phase 2 stub.

Plan:
1. Identify invoice-shaped documents (filename hint + content heuristic).
2. Extract: issuer, invoice_number, invoice_date, due_date, subtotal, tax,
   total, payment_account, currency.
3. Score each field's confidence by extractor agreement (chunk vs table).
4. Hand back a normalised list of dicts that the parent workflow / CSV
   exporter can format.

Until Phase 2 lands the dedicated MCP tool, the ``office-layer`` CLI / Claude
Code use the Phase 0 building blocks (search + evidence) and let Claude do
the field-mapping inline. See `plugin/commands/office-extract-invoices.md`.
"""

from __future__ import annotations

from typing import Any


def extract_invoices(workspace_id: str) -> list[dict[str, Any]]:  # pragma: no cover - stub
    raise NotImplementedError("Phase 2 — see plugin/commands/office-extract-invoices.md")
