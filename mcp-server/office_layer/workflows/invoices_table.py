"""Invoices-to-table — Phase 2.

Workspace-scoped batch counterpart to :mod:`workflows.invoice`. For every
invoice-shaped document already indexed under a workspace, run the field
extractor, then materialise a 12-column CSV that an accounting team can hand
off to a spreadsheet without further cleanup.

Pipeline:

1. Resolve the workspace and run the safety classifier on
   ``("export_csv", [output_path], workspace)``. ``read-only`` workspaces +
   targets outside the workspace root both escalate to HIGH risk and are
   refused. The actual file write only happens when the classifier returns
   LOW or MEDIUM.
2. List documents in the workspace; keep ``kind ∈ {PDF, MARKDOWN, XLSX,
   DOCX}``. (Other kinds — txt receipts, csv, json — never produce a usable
   invoice number under the regex extractor and would only pollute the
   CSV.)
3. Run :func:`workflows.invoice.extract_invoice_fields` (persisting back to
   storage) over each candidate, unless ``run_extractor=False`` — the
   no-extract path is for callers that already populated the fields and
   just want to project them into a CSV.
4. Drop documents that did not produce ``invoice_number`` after extraction.
   This is the canonical "this is actually an invoice" signal — the field
   only gets written when one of the three invoice strategies fired
   (label-anchored, section-header, or inline ID hint).
5. Emit a CSV at ``output_path`` with a timestamp suffix appended so the
   call is idempotent and never silently overwrites a prior export. Caller
   gets the resolved final path back in the return dict.

Public surface:

- :func:`extract_invoices_to_table` — the MCP / CLI entry point.
- :func:`build_invoice_rows` — pure function (no I/O) for unit-testing the
  field-projection step independently of the safety + write path.

Columns:

    invoice_number, issue_date, due_date, issuer, recipient,
    subtotal, tax, total, currency, payment_account,
    source_path, confidence_avg

``currency`` is derived from the parsed ``total`` (``parse_amount`` in
:mod:`workflows.client_history`) — empty string when the total is missing
or unparseable. ``confidence_avg`` averages over the fields that were
actually present on this row (not over the 10 invoice keys), so a sparse
extraction is not penalised twice.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..engine import get_engine
from ..models import Document, DocumentKind, ExtractedField, OperationRiskLevel
from ..safety import classify_operation
from .client_history import parse_amount
from .invoice import extract_invoice_fields

# -- constants ----------------------------------------------------------------

# Kinds the invoice extractor has a realistic shot at. Everything else
# (txt receipts, csv ledgers, json dumps) gets skipped — those formats
# never produce a real invoice_number under the current regex set and
# would only add noise rows.
_INVOICE_KINDS: frozenset[DocumentKind] = frozenset(
    {DocumentKind.PDF, DocumentKind.MARKDOWN, DocumentKind.XLSX, DocumentKind.DOCX}
)

# Column order ships in the CSV header and the dict rows. Keep these two
# in lockstep — `build_invoice_rows` keys the dict by these names so a
# downstream caller can `dict_writer.writerow(row)` without remapping.
COLUMNS: tuple[str, ...] = (
    "invoice_number",
    "issue_date",
    "due_date",
    "issuer",
    "recipient",
    "subtotal",
    "tax",
    "total",
    "currency",
    "payment_account",
    "source_path",
    "confidence_avg",
)

# Fields participating in confidence_avg. source_path / confidence_avg /
# currency are projection-only and never carry their own confidence.
_FIELD_KEYS: tuple[str, ...] = (
    "invoice_number",
    "issue_date",
    "due_date",
    "issuer",
    "recipient",
    "subtotal",
    "tax",
    "total",
    "payment_account",
)

_LOW_CONFIDENCE_FLOOR = 0.70


# -- row building --------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceRow:
    """Per-document projection of the invoice fields into table shape."""

    document_id: str
    values: dict[str, str]
    confidence_avg: float


def build_invoice_rows(
    docs_with_fields: list[tuple[Document, list[ExtractedField]]],
) -> list[InvoiceRow]:
    """Project ``(doc, fields)`` pairs into table rows.

    Pure function — no storage, no engine. Only documents whose field set
    contains ``invoice_number`` are emitted; the rest are silently dropped
    so the caller can decide what to do with the count.
    """
    rows: list[InvoiceRow] = []
    for doc, fields in docs_with_fields:
        by_key = {f.key: f for f in fields}
        if "invoice_number" not in by_key:
            continue
        values: dict[str, str] = {col: "" for col in COLUMNS}
        for key in _FIELD_KEYS:
            f = by_key.get(key)
            if f is not None:
                values[key] = f.value
        total_field = by_key.get("total")
        if total_field is not None:
            parsed = parse_amount(total_field.value)
            if parsed is not None:
                values["currency"] = parsed[0]
        values["source_path"] = doc.path
        present = [by_key[k].confidence for k in _FIELD_KEYS if k in by_key]
        conf_avg = round(sum(present) / len(present), 3) if present else 0.0
        values["confidence_avg"] = f"{conf_avg:.3f}"
        rows.append(
            InvoiceRow(document_id=doc.id, values=values, confidence_avg=conf_avg)
        )
    return rows


# -- output path safety -------------------------------------------------------


def _resolve_output_path(workspace_root: Path, output_path: str | Path) -> Path:
    """Treat relative paths as workspace-relative; expand `~`."""
    p = Path(output_path).expanduser()
    if not p.is_absolute():
        p = workspace_root / p
    return p


def _timestamped(path: Path) -> Path:
    """Append ``-YYYYMMDD-HHMMSS`` before the suffix.

    Prevents silently overwriting a prior export. The full path is returned
    to the caller so they can locate the file we just wrote.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}-{ts}{path.suffix or '.csv'}")


# -- workflow entrypoint ------------------------------------------------------


def extract_invoices_to_table(
    workspace_id: str,
    output_path: str | Path,
    *,
    run_extractor: bool = True,
) -> dict[str, Any]:
    """Export every invoice in ``workspace_id`` as a CSV row.

    The output file is written to ``<output_path>`` with a UTC timestamp
    suffix inserted before its extension. Relative paths resolve against
    the workspace root. Writes outside the workspace root are refused.

    ``run_extractor=True`` (default) re-runs :func:`extract_invoice_fields`
    over each candidate document so the CSV always reflects the latest
    state of the source files. Pass ``False`` when you have already
    extracted and just want to materialise the table — the projection
    will then read whatever is in ``extracted_fields`` as-is.
    """
    engine = get_engine()
    ws = engine.workspaces.get(workspace_id)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}

    workspace_root = Path(ws.root_path).resolve()
    target = _resolve_output_path(workspace_root, output_path)
    timestamped = _timestamped(target)

    # Safety gate. The classifier already encodes the two rules we care
    # about: read-only workspaces refuse writes, and targets outside the
    # workspace root escalate to HIGH risk. Reject either way.
    risk = classify_operation(
        "export_csv", targets=[str(timestamped)], workspace=ws
    )
    if risk.level == OperationRiskLevel.HIGH:
        return {
            "error": (
                f"refused: export_csv classified as HIGH risk "
                f"({'; '.join(risk.reasons) or 'no reason given'})"
            ),
            "risk": risk.model_dump(mode="json"),
        }

    # Collect candidate documents.
    docs = engine.storage.list_documents(workspace_id=workspace_id, limit=10_000)
    invoice_kind_values = {k.value for k in _INVOICE_KINDS}
    candidates = [
        d
        for d in docs
        if (d.kind.value if hasattr(d.kind, "value") else d.kind) in invoice_kind_values
    ]
    if not candidates:
        return _empty_result(
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            output_path=None,
            note=(
                "no PDF/MD/XLSX/DOCX documents indexed for this workspace — "
                "run `office-layer index <workspace_id>` first"
            ),
        )

    # Run the per-doc extractor (or skip it on caller request) and load
    # the resulting field set for projection.
    skipped: list[dict[str, str]] = []
    docs_with_fields: list[tuple[Document, list[ExtractedField]]] = []
    for doc in candidates:
        if run_extractor:
            extract_invoice_fields(doc.id, persist=True)
        fields = engine.storage.get_fields(doc.id)
        if not any(f.key == "invoice_number" for f in fields):
            skipped.append(
                {
                    "document_id": doc.id,
                    "path": doc.path,
                    "reason": "no invoice_number after extraction",
                }
            )
            continue
        docs_with_fields.append((doc, fields))

    rows = build_invoice_rows(docs_with_fields)

    # Write the CSV — header + one row per invoice. Always create parent
    # dirs because the workspace may have been added before the user
    # created a drafts/ subfolder; this avoids forcing them to mkdir
    # manually before their first export.
    timestamped.parent.mkdir(parents=True, exist_ok=True)
    with timestamped.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.values)

    low_conf_count = sum(1 for r in rows if r.confidence_avg < _LOW_CONFIDENCE_FLOOR)
    low_conf_paths = [
        r.values["source_path"] for r in rows if r.confidence_avg < _LOW_CONFIDENCE_FLOOR
    ]

    return {
        "workspace_id": workspace_id,
        "output_path": str(timestamped),
        "row_count": len(rows),
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "low_confidence_count": low_conf_count,
        "low_confidence_paths": low_conf_paths,
        "columns": list(COLUMNS),
        "risk": risk.model_dump(mode="json"),
        "ran_extractor": bool(run_extractor),
    }


def _empty_result(
    *,
    workspace_id: str,
    workspace_root: Path,
    output_path: Path | None,
    note: str,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "output_path": None if output_path is None else str(output_path),
        "row_count": 0,
        "candidate_count": 0,
        "skipped_count": 0,
        "skipped": [],
        "low_confidence_count": 0,
        "low_confidence_paths": [],
        "columns": list(COLUMNS),
        "note": note,
        "ran_extractor": False,
    }


__all__ = [
    "extract_invoices_to_table",
    "build_invoice_rows",
    "InvoiceRow",
    "COLUMNS",
]
