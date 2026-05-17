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
5. Run the mass-op gate on ``("bulk_modify", row_count, workspace)``. When
   the row count exceeds ``BULK_MODIFY_THRESHOLD`` and the caller has not
   yet passed ``confirm=True``, stage a ``<stem>.preview.csv`` (first 10
   rows + the same header) and return a ``confirmation_required`` shape so
   the caller can prompt the user and re-run.
6. Emit the full CSV at ``output_path`` with a timestamp suffix appended
   so the call is idempotent and never silently overwrites a prior export.
   Caller gets the resolved final path back in the return dict.

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
from ..models import Document, DocumentKind, ExtractedField
from ..safety import intercept as _pretool_intercept
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


# Rows shown in the preview CSV when the mass-op gate fires. Caller is
# expected to eyeball the head, decide the export is what they wanted,
# and re-run with --confirm. The preview itself is intentionally
# non-timestamped: it's a transient review artifact that should
# overwrite itself on re-runs, unlike the real export.
_PREVIEW_HEAD = 10


def _write_preview_csv(
    target: Path, rows: list["InvoiceRow"], *, head: int = _PREVIEW_HEAD
) -> Path:
    """Write a head-of-table preview next to where the full CSV would land.

    Filename is ``<target_stem>.preview.csv`` (no timestamp) so a re-run
    of the same call overwrites its own previous preview rather than
    leaving a pile of `.preview-YYYY...csv` files around.
    """
    preview = target.with_name(
        f"{target.stem}.preview{target.suffix or '.csv'}"
    )
    preview.parent.mkdir(parents=True, exist_ok=True)
    with preview.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows[:head]:
            writer.writerow(row.values)
    return preview


# -- workflow entrypoint ------------------------------------------------------


def extract_invoices_to_table(
    workspace_id: str,
    output_path: str | Path,
    *,
    run_extractor: bool = True,
    confirm: bool = False,
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

    ``confirm=False`` (default) makes a row count above
    :data:`safety.BULK_MODIFY_THRESHOLD` stage a ``<stem>.preview.csv``
    (first 10 rows + the same header) instead of writing the full file,
    and return a ``confirmation_required`` shape so the caller can show
    the preview to the user and re-run with ``confirm=True``. Pass
    ``True`` to skip the preview gate and write the full export
    directly.
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
    # workspace root escalate to HIGH risk. The refusal shape is owned
    # by ``safety.pretool`` so every write workflow refuses the same way.
    gate = _pretool_intercept(
        "export_csv", targets=[str(timestamped)], workspace=ws
    )
    if gate.refusal is not None:
        return gate.refusal
    risk = gate.risk

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

    # Mass-op gate. Now that we know the actual row count, give the
    # classifier a chance to demand explicit confirmation. We only run
    # this gate when there are rows to write (an empty export already
    # falls below the threshold and produces no preview to look at).
    # Two outcomes worth handling:
    #   * gate.refusal — the bulk_modify path also escalates to HIGH
    #     for outside-workspace / read-only just like export_csv does;
    #     mirror the existing refuse path so the contract is uniform.
    #   * gate.confirmation — row_count > threshold AND caller has not
    #     passed confirm=True yet. Stage a preview CSV and surface a
    #     confirmation_required dict so the caller can show the head
    #     to the user and re-run.
    if rows:
        mass_gate = _pretool_intercept(
            "bulk_modify",
            targets=[str(timestamped)],
            workspace=ws,
            row_count=len(rows),
            confirm=confirm,
        )
        if mass_gate.refusal is not None:
            return mass_gate.refusal
        if mass_gate.confirmation is not None:
            preview_path = _write_preview_csv(target, rows)
            return {
                **mass_gate.confirmation,
                "workspace_id": workspace_id,
                "preview_path": str(preview_path),
                "output_path": None,
                "row_count": len(rows),
                "candidate_count": len(candidates),
                "skipped_count": len(skipped),
                "skipped": skipped,
                "columns": list(COLUMNS),
                "ran_extractor": bool(run_extractor),
            }

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
        "confirmed": bool(confirm),
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
