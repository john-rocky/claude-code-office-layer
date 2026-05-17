"""Invoice workflow — Phase 2.

Pure regex + heuristic over already-extracted chunks. No LLM call. The fields
mirror what a Japanese SMB invoice (請求書) actually contains; the extractor
also tolerates English invoices ("Invoice No.", "Total", "Due").

Public surface:

- :func:`extract_invoice_fields` — load chunks for ``document_id``, run the
  extractor, optionally persist via ``Storage.replace_extracted_fields``,
  return a JSON-safe dict for MCP / CLI callers.
- :func:`extract_fields_from_text` — pure function, easy to unit-test without
  spinning up the Engine.

Confidence:

- ``0.95`` when a label keyword anchors the value (``請求日: 2025/03/15``).
- ``0.75`` when the label is a section header and the value is on the next
  non-empty line (markdown ``## 請求日`` form).
- ``0.55`` for fallback patterns (first money-shaped token after the start
  of the document, when no label has fired for ``total``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..engine import get_engine
from ..models import DocumentChunk, ExtractedField


# -- atom patterns ------------------------------------------------------------

_AMOUNT = re.compile(
    r"""(?x)
    (?:
        [¥$€]\s?\d{1,3}(?:[,，]\d{3})*(?:\.\d+)?
        | \d{1,3}(?:[,，]\d{3})*\s*円
        | \d+(?:\.\d+)?\s*(?:万円|百万円|億円)
        | (?:USD|JPY|EUR|GBP|CNY)\s*\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?
        | \d{1,3}(?:[,，]\d{3})+(?:\.\d+)?
    )
    """
)

_DATE = re.compile(
    r"""(?x)
    (?:
        \d{4}[-/年]\s?\d{1,2}[-/月]\s?\d{1,2}日?
        | \d{1,2}[/-]\d{1,2}[/-]\d{2,4}
        | \d{4}年\d{1,2}月\d{1,2}日?
    )
    """
)

_INVOICE_ID = re.compile(
    r"""(?x)
    (?:
        (?:INV|BILL)[-_/][A-Za-z0-9][A-Za-z0-9\-_/]*    # INV-2026-001, BILL-2026-007, INV-XLS-2026-A1
        | №\s?[A-Za-z0-9\-_/]+                          # №12345
        | [A-Z]{2,}-\d{4,}(?:-[A-Za-z0-9]+)*           # generic XYZ-2026-...
    )
    """
)


# -- label specs --------------------------------------------------------------


@dataclass(frozen=True)
class _LabelSpec:
    key: str
    value_type: str
    labels: tuple[str, ...]
    atom: re.Pattern | None
    # If atom is None, value = the rest of the line (or next non-empty line).
    same_line_confidence: float = 0.95
    next_line_confidence: float = 0.75


# Order matters: more specific labels first so "合計" doesn't eat "小計合計".
# Labels are matched as literal strings (not regex) for the anchor, then a
# small window of text around the match is searched for the value atom.
_SPECS: tuple[_LabelSpec, ...] = (
    _LabelSpec(
        key="invoice_number",
        value_type="id",
        labels=("請求書番号", "請求番号", "Invoice No.", "Invoice No", "Invoice #"),
        atom=_INVOICE_ID,
    ),
    _LabelSpec(
        key="issue_date",
        value_type="date",
        labels=("請求日", "発行日", "発行年月日", "Invoice Date", "Issue Date", "Date"),
        atom=_DATE,
    ),
    _LabelSpec(
        key="due_date",
        value_type="date",
        labels=("支払期限", "お支払期限", "お支払い期限", "支払日", "Due Date", "Due", "Payment Due"),
        atom=_DATE,
    ),
    _LabelSpec(
        key="subtotal",
        value_type="amount",
        labels=("小計", "Subtotal"),
        atom=_AMOUNT,
    ),
    _LabelSpec(
        key="tax",
        value_type="amount",
        labels=("消費税", "税額", "Tax", "VAT"),
        atom=_AMOUNT,
    ),
    _LabelSpec(
        # Match 合計 before 小計 only because the SAME_LINE pass picks the
        # nearest atom on the line, and `小計` is anchored by its own spec
        # above before we ever scan `合計`.
        key="total",
        value_type="amount",
        labels=("ご請求金額", "請求金額", "請求合計", "合計金額", "合計", "総額", "Total", "Amount Due", "Grand Total"),
        atom=_AMOUNT,
    ),
    _LabelSpec(
        key="recipient",
        value_type="text",
        labels=("取引先", "宛先", "請求先", "Bill To", "Billed To", "Customer"),
        atom=None,
    ),
    _LabelSpec(
        key="issuer",
        value_type="text",
        labels=("発行元", "発行者", "請求元", "From", "Issued By"),
        atom=None,
    ),
    _LabelSpec(
        key="payment_account",
        value_type="text",
        labels=("振込先", "お振込先", "Payment Account", "Bank"),
        atom=None,
    ),
)


# Strip markdown / list / colon noise that surrounds a value.
_LEADING_NOISE = re.compile(r"^[\s>*\-•・:：\)\]\}]+")
_TRAILING_NOISE = re.compile(r"[\s:：]+$")


def _clean(s: str) -> str:
    s = _LEADING_NOISE.sub("", s)
    s = _TRAILING_NOISE.sub("", s)
    return s.strip()


def _strip_markdown_heading(line: str) -> str:
    return re.sub(r"^#+\s*", "", line).strip()


def _next_nonempty(lines: list[str], start: int) -> tuple[int, str] | None:
    for i in range(start, len(lines)):
        if lines[i].strip():
            return i, lines[i]
    return None


# -- text-level extractor -----------------------------------------------------


def extract_fields_from_text(
    text: str,
    *,
    document_id: str,
    chunk_id: str | None = None,
    page_number: int | None = None,
    cell_range: str | None = None,
) -> list[ExtractedField]:
    """Pure function. Returns one ExtractedField per detected key.

    Multiple chunks may yield the same key; the caller is expected to dedupe
    (see :func:`_dedupe_by_key`). Within a single call we do dedupe per key
    so a long invoice with the label repeated twice doesn't double-emit.
    """
    if not text:
        return []
    lines = text.splitlines()
    out: list[ExtractedField] = []
    seen_keys: set[str] = set()

    for spec in _SPECS:
        field = _extract_one(spec, text, lines, document_id, chunk_id, page_number, cell_range)
        if field is None:
            continue
        if field.key in seen_keys:
            continue
        seen_keys.add(field.key)
        out.append(field)

    # Inline ID hint — invoices often print `# 請求書 INV-2025-03` in the
    # title with no explicit "Invoice No." label. Fire only when the
    # label-anchored pass missed.
    if "invoice_number" not in seen_keys:
        m = _INVOICE_ID.search(text)
        if m and ("請求書" in text or "Invoice" in text or "INVOICE" in text):
            out.append(
                ExtractedField(
                    document_id=document_id,
                    chunk_id=chunk_id,
                    key="invoice_number",
                    value=m.group(0).strip(),
                    value_type="id",
                    confidence=0.65,
                    page_number=page_number,
                    cell_range=cell_range,
                    extractor="invoice.heuristic",
                )
            )

    return out


def _extract_one(
    spec: _LabelSpec,
    text: str,
    lines: list[str],
    document_id: str,
    chunk_id: str | None,
    page_number: int | None,
    cell_range: str | None,
) -> ExtractedField | None:
    for label in spec.labels:
        # Strategy 1: label appears on a line followed by the value on the
        # same line — `請求日: 2025/03/15` / `合計  ¥748,000`.
        same_line = _try_same_line(spec, label, lines)
        if same_line is not None:
            value, line_idx = same_line
            return ExtractedField(
                document_id=document_id,
                chunk_id=chunk_id,
                key=spec.key,
                value=value,
                value_type=spec.value_type,  # type: ignore[arg-type]
                confidence=spec.same_line_confidence,
                page_number=page_number,
                cell_range=cell_range,
                extractor="invoice.label",
            )

        # Strategy 2: label is alone on its line (often a `## ヘッダ`) and
        # the value is on the next non-empty line.
        next_line = _try_next_line(spec, label, lines)
        if next_line is not None:
            value, line_idx = next_line
            return ExtractedField(
                document_id=document_id,
                chunk_id=chunk_id,
                key=spec.key,
                value=value,
                value_type=spec.value_type,  # type: ignore[arg-type]
                confidence=spec.next_line_confidence,
                page_number=page_number,
                cell_range=cell_range,
                extractor="invoice.section",
            )
    return None


def _try_same_line(
    spec: _LabelSpec, label: str, lines: list[str]
) -> tuple[str, int] | None:
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        # Find the label inside the line; the value lives in the tail after
        # the label. Case-sensitive on Japanese, case-insensitive on ASCII.
        if not _line_contains(line, label):
            continue
        tail = _line_tail_after(line, label)
        if not tail:
            continue
        if spec.atom is None:
            cleaned = _clean(tail)
            if cleaned and cleaned != label:
                return cleaned, i
            continue
        m = spec.atom.search(tail)
        if m:
            return m.group(0).strip(), i
    return None


def _try_next_line(
    spec: _LabelSpec, label: str, lines: list[str]
) -> tuple[str, int] | None:
    for i, raw in enumerate(lines):
        stripped = _strip_markdown_heading(raw).strip()
        if not stripped:
            continue
        # The label must BE the line content (heading-style), not just appear
        # inside it — otherwise "ご請求金額は ¥748,000 です" would trigger
        # both strategies and pick the wrong value.
        if stripped != label and _clean(stripped) != label:
            continue
        nxt = _next_nonempty(lines, i + 1)
        if nxt is None:
            continue
        _, candidate = nxt
        candidate_clean = _clean(candidate)
        if spec.atom is None:
            if candidate_clean:
                return candidate_clean, i + 1
            continue
        m = spec.atom.search(candidate_clean)
        if m:
            return m.group(0).strip(), i + 1
    return None


def _ascii_label_re(label: str) -> re.Pattern:
    # Word-boundary so "Total" doesn't match inside "Subtotal".
    return re.compile(r"(?<![A-Za-z])" + re.escape(label) + r"(?![A-Za-z])", re.IGNORECASE)


def _line_contains(line: str, label: str) -> bool:
    if _is_ascii(label):
        return _ascii_label_re(label).search(line) is not None
    return label in line


def _line_tail_after(line: str, label: str) -> str:
    if _is_ascii(label):
        m = _ascii_label_re(label).search(line)
        return line[m.end():] if m else ""
    idx = line.find(label)
    return line[idx + len(label):] if idx >= 0 else ""


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


# -- document-level workflow --------------------------------------------------


def _dedupe_by_key(fields: Iterable[ExtractedField]) -> list[ExtractedField]:
    """Keep the highest-confidence value for each key across chunks."""
    best: dict[str, ExtractedField] = {}
    for f in fields:
        cur = best.get(f.key)
        if cur is None or f.confidence > cur.confidence:
            best[f.key] = f
    # Stable order: known field order first, then anything else alphabetical.
    known_order = [s.key for s in _SPECS]
    known_set = set(known_order)
    ordered = [best[k] for k in known_order if k in best]
    ordered.extend(best[k] for k in sorted(best) if k not in known_set)
    return ordered


def extract_invoice_fields(
    document_id: str,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Extract invoice fields for one indexed document.

    Loads its chunks from storage, runs the regex extractor per chunk,
    dedupes by key (highest confidence wins). When ``persist`` is True the
    fields are also written to ``extracted_fields`` so a follow-up
    ``build_evidence_packet`` or CSV export can pick them up.
    """
    engine = get_engine()
    doc = engine.storage.get_document(document_id)
    if doc is None:
        return {"error": f"document '{document_id}' not found"}
    chunks: list[DocumentChunk] = engine.storage.get_chunks(document_id)
    if not chunks:
        return {
            "document": doc.model_dump(mode="json"),
            "fields": [],
            "note": "no chunks indexed for this document — run `office-layer index` first",
        }

    candidates: list[ExtractedField] = []
    for ch in chunks:
        candidates.extend(
            extract_fields_from_text(
                ch.text,
                document_id=document_id,
                chunk_id=ch.id,
                page_number=ch.page_number,
                cell_range=ch.cell_range,
            )
        )
    fields = _dedupe_by_key(candidates)

    if persist:
        engine.storage.replace_extracted_fields(document_id, fields)

    avg_conf = round(sum(f.confidence for f in fields) / len(fields), 3) if fields else 0.0
    low_conf = [f.key for f in fields if f.confidence < 0.7]
    return {
        "document": doc.model_dump(mode="json"),
        "fields": [f.model_dump(mode="json") for f in fields],
        "field_count": len(fields),
        "avg_confidence": avg_conf,
        "low_confidence_keys": low_conf,
        "persisted": persist,
    }


__all__ = ["extract_invoice_fields", "extract_fields_from_text"]
