"""Client history — Phase 2.

Aggregates a single client's documents (invoices / contracts / emails / notes)
across the indexed workspaces. Pure-Python, no extra deps.

Pipeline:

1. Expand ``client_name`` into spelling variants — corporate-suffix stripped
   stem, email local-part + domain + first-label, plus caller-supplied
   aliases.
2. Recall candidate documents via ``Storage.find_documents_with_entity`` for
   every variant; fall back to a filename / path scan so docs whose entity
   index missed are not silently dropped.
3. Classify each doc as ``invoice`` / ``contract`` / ``email`` / ``note``
   from ``extracted_fields`` presence + path / name regex. The file-format
   ``DocumentKind`` (pdf / docx / markdown / …) is intentionally NOT used
   as the bucket key — an invoice is just as likely to be a markdown file
   or an XLSX as it is a PDF.
4. Aggregate invoice totals per-currency by parsing the persisted ``total``
   ``ExtractedField`` rows written by :mod:`workflows.invoice`.
5. Return a chronological timeline of all hits + a trimmed Evidence Packet.

This is the central tool for the invoice → contract → email follow-up
workflow chain.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..engine import get_engine
from ..models import (
    Document,
    DocumentKind,
    ExtractedField,
    SearchQuery,
    SearchResult,
)

# -- aliases ------------------------------------------------------------------


_JP_CORP_SUFFIXES: tuple[str, ...] = (
    "株式会社",
    "有限会社",
    "合同会社",
    "合名会社",
    "合資会社",
    "社団法人",
    "財団法人",
    "一般社団法人",
    "一般財団法人",
)

# Trailing English corporate suffix. Anchored to end-of-string after a
# separator so "Acme Inc. Tokyo office" still keeps "Acme Inc." intact —
# we only fire when the suffix terminates the name.
_EN_CORP_SUFFIX_RE = re.compile(
    r"""(?ix)
    [,\s]+
    (?:
        Inc\.? | LLC | Ltd\.? | Limited | Corp\.? | Corporation
        | GmbH | S\.A\. | K\.K\. | Co\.,?\s*Ltd\.?
    )
    \s*$
    """
)

_MIN_ALIAS_LEN = 2


def expand_aliases(name: str, extra: list[str] | None = None) -> list[str]:
    """Return spelling variants used for entity / filename recall.

    Variants are deduped case-insensitively and any variant shorter than
    :data:`_MIN_ALIAS_LEN` characters is dropped — a 1-char alias would
    drag in unrelated docs through ``find_documents_with_entity``'s
    substring LIKE match.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(v: str | None) -> None:
        if v is None:
            return
        v = v.strip()
        if len(v) < _MIN_ALIAS_LEN:
            return
        key = v.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    raw: list[str] = [name]
    if extra:
        raw.extend(extra)

    for r in raw:
        if not r:
            continue
        original = r.strip()
        add(original)
        # JP corporate suffix — emit the stem with and without surrounding
        # whitespace so we hit both "ACME株式会社" and "ACME 株式会社" forms.
        for suf in _JP_CORP_SUFFIXES:
            if suf in original:
                stripped = original.replace(suf, "")
                add(stripped.strip())
                add(stripped.replace(" ", "").strip())
        # EN corporate suffix at end of string.
        m = _EN_CORP_SUFFIX_RE.search(original)
        if m:
            add(original[: m.start()].strip())
        # Email → local part + domain + first label.
        if "@" in original:
            local, _, domain = original.partition("@")
            add(local)
            if domain:
                add(domain)
                add(domain.split(".")[0])
        # Bare domain (no spaces, contains a dot) → first label only.
        elif "." in original and " " not in original:
            add(original.split(".")[0])

    return out


# -- amount parsing -----------------------------------------------------------


_AMOUNT_NUM_RE = re.compile(r"\d{1,3}(?:[,，\s]\d{3})*(?:\.\d+)?")

_CURRENCY_SYMBOLS: dict[str, str] = {
    "¥": "JPY",
    "￥": "JPY",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}

_CURRENCY_CODES: tuple[str, ...] = ("JPY", "USD", "EUR", "GBP", "CNY", "KRW")

# Japanese amount suffixes that imply JPY and a unit scaling factor.
_JP_UNIT_SCALES: tuple[tuple[str, int], ...] = (
    ("億円", 100_000_000),
    ("百万円", 1_000_000),
    ("万円", 10_000),
    ("円", 1),
)


def parse_amount(value: str) -> tuple[str, float] | None:
    """Parse a money string into ``(currency_code, numeric_value)``.

    Supported shapes (covers the atoms emitted by
    :mod:`workflows.invoice` and :mod:`engine.entities`):

    * ``¥748,000`` / ``$7,403.50`` / ``€500`` — symbol-prefixed
    * ``USD 1,200.50`` — ISO-code-prefixed
    * ``12,000 円`` / ``100 万円`` / ``1.5億円`` — JP suffix
    """
    if not value:
        return None
    s = value.strip()

    for sym, code in _CURRENCY_SYMBOLS.items():
        if s.startswith(sym):
            n = _AMOUNT_NUM_RE.search(s[len(sym):])
            if n:
                return code, _to_float(n.group(0))
    upper = s.upper()
    for code in _CURRENCY_CODES:
        if upper.startswith(code):
            n = _AMOUNT_NUM_RE.search(s[len(code):])
            if n:
                return code, _to_float(n.group(0))
    for suf, scale in _JP_UNIT_SCALES:
        if suf in s:
            head = s.split(suf, 1)[0]
            n = _AMOUNT_NUM_RE.search(head)
            if n:
                return "JPY", _to_float(n.group(0)) * scale
    # Bare number — caller is responsible for currency context; we refuse to
    # guess JPY/USD here since the wrong default is worse than no number.
    return None


def _to_float(s: str) -> float:
    return float(s.replace(",", "").replace("，", "").replace(" ", ""))


# -- bucket classification ----------------------------------------------------


_INVOICE_NAME_RE = re.compile(r"請求書|invoice|billing", re.IGNORECASE)
_CONTRACT_NAME_RE = re.compile(r"契約|nda|agreement|contract|覚書", re.IGNORECASE)
_EMAIL_NAME_RE = re.compile(r"\.eml$|\.msg$|mail|メール", re.IGNORECASE)


def classify_doc(doc: Document, field_keys: set[str]) -> str:
    """Bucket key. ``field_keys`` is the set of ``ExtractedField.key`` values
    persisted for this document — having ``invoice_number`` is the strongest
    invoice signal because :mod:`workflows.invoice` only persists it when
    the extractor actually fired."""
    path = doc.path.lower()
    name = doc.name.lower()
    kind = doc.kind if isinstance(doc.kind, str) else doc.kind.value

    if "invoice_number" in field_keys:
        return "invoice"
    if "/invoices/" in path or _INVOICE_NAME_RE.search(name):
        return "invoice"
    if "/contracts/" in path or _CONTRACT_NAME_RE.search(name):
        return "contract"
    if kind == DocumentKind.EMAIL.value or _EMAIL_NAME_RE.search(name):
        return "email"
    return "note"


# -- workflow entrypoint ------------------------------------------------------


def build_client_history(
    client_name: str,
    *,
    workspace_ids: list[str] | None = None,
    aliases: list[str] | None = None,
    timeline_limit: int = 50,
    evidence_max_sources: int = 5,
) -> dict[str, Any]:
    """Aggregate one client's documents across indexed workspaces.

    ``workspace_ids`` defaults to all workspaces. ``aliases`` lets the caller
    (Claude / a CLI user) inject extra spelling variants the heuristic would
    miss (e.g. a project codename used only in emails).
    """
    name = (client_name or "").strip()
    if not name:
        return {"error": "client_name is required"}

    engine = get_engine()
    storage = engine.storage

    if workspace_ids is None:
        workspaces = storage.list_workspaces()
        ws_ids = [ws.id for ws in workspaces]
    else:
        ws_ids = list(workspace_ids)

    variants = expand_aliases(name, aliases)
    candidate_doc_ids: dict[str, str] = {}  # doc_id → matched_via reason

    for v in variants:
        hits = storage.find_documents_with_entity(
            v, workspace_ids=ws_ids or None, limit=500
        )
        for doc_id in hits:
            candidate_doc_ids.setdefault(doc_id, f"entity:{v}")

    # Filename / path fallback for docs the entity index missed. Important
    # when the regex entity extractor did not classify the name as a
    # company (plain "ACME" without 株式会社 / Inc. trips this).
    doc_pool: list[Document] = []
    if ws_ids:
        for ws_id in ws_ids:
            doc_pool.extend(storage.list_documents(workspace_id=ws_id, limit=2000))
    else:
        doc_pool = storage.list_documents(limit=2000)
    variant_lowers = [v.lower() for v in variants]
    for doc in doc_pool:
        if doc.id in candidate_doc_ids:
            continue
        hay = f"{doc.name}\n{doc.path}".lower()
        for vl in variant_lowers:
            if vl in hay:
                candidate_doc_ids[doc.id] = f"filename:{vl}"
                break

    # Resolve documents in one pass and bucket them.
    docs: list[Document] = []
    for doc_id in candidate_doc_ids:
        d = storage.get_document(doc_id)
        if d is not None:
            docs.append(d)

    # Aggregate.
    buckets: dict[str, list[Document]] = {
        "invoice": [],
        "contract": [],
        "email": [],
        "note": [],
    }
    fields_by_doc: dict[str, list[ExtractedField]] = {}
    for d in docs:
        flist = storage.get_fields(d.id)
        fields_by_doc[d.id] = flist
        bucket = classify_doc(d, {f.key for f in flist})
        buckets[bucket].append(d)

    totals_by_currency: dict[str, float] = {}
    totals_raw: list[str] = []
    for d in buckets["invoice"]:
        for f in fields_by_doc.get(d.id, ()):
            if f.key != "total":
                continue
            parsed = parse_amount(f.value)
            if parsed is None:
                continue
            code, amount = parsed
            totals_by_currency[code] = round(totals_by_currency.get(code, 0.0) + amount, 2)
            totals_raw.append(f.value)
            break  # one total per invoice

    # Timeline: all buckets merged, newest first.
    flat: list[tuple[Document, str]] = []
    for bucket_name, items in buckets.items():
        for d in items:
            flat.append((d, bucket_name))
    flat.sort(key=lambda pair: _mtime_aware(pair[0].mtime), reverse=True)

    timeline = [
        {
            "date": _mtime_aware(d.mtime).isoformat(),
            "kind": bucket_name,
            "doc_id": d.id,
            "name": d.name,
            "path": d.path,
            "file_kind": d.kind if isinstance(d.kind, str) else d.kind.value,
            "matched_via": candidate_doc_ids.get(d.id, "unknown"),
        }
        for d, bucket_name in flat[:timeline_limit]
    ]

    # Trimmed evidence packet — capped so the MCP response stays readable.
    packet = _build_evidence_packet(
        engine,
        name,
        flat,
        max_sources=evidence_max_sources,
    )

    return {
        "client": name,
        "aliases_used": variants,
        "workspace_ids": ws_ids,
        "document_count": len(docs),
        "invoice_count": len(buckets["invoice"]),
        "contract_count": len(buckets["contract"]),
        "email_count": len(buckets["email"]),
        "note_count": len(buckets["note"]),
        "total_invoiced_amount": totals_by_currency,
        "total_invoiced_raw": totals_raw,
        "timeline": timeline,
        "evidence_packet": packet,
    }


def _mtime_aware(mt: datetime) -> datetime:
    return mt if mt.tzinfo else mt.replace(tzinfo=timezone.utc)


def _build_evidence_packet(
    engine,
    client_name: str,
    flat: list[tuple[Document, str]],
    *,
    max_sources: int,
) -> dict[str, Any]:
    """Wrap the existing EvidencePacketBuilder so callers get the same
    Source shape they already consume from search-driven flows. Cap at
    ``max_sources`` so a client with 100+ docs doesn't blow up the MCP
    response."""
    if not flat:
        return {
            "intent": f"client_history:{client_name}",
            "sources": [],
            "workspace_ids": [],
        }
    top = flat[:max_sources]
    results: list[SearchResult] = []
    for idx, (doc, _bucket) in enumerate(top):
        chunks = engine.storage.get_chunks(doc.id)
        chunk = chunks[0] if chunks else None
        results.append(
            SearchResult(
                document=doc,
                chunk=chunk,
                score=float(len(top) - idx),
                reason="client_history",
                matched_terms=[client_name],
            )
        )
    from ..engine.evidence import EvidencePacketBuilder

    query = SearchQuery(text=client_name, limit=max_sources)
    builder = EvidencePacketBuilder(engine.storage)
    packet = builder.build(
        f"client_history:{client_name}",
        results=results,
        query=query,
        max_sources=max_sources,
    )
    return packet.model_dump(mode="json")


__all__ = [
    "build_client_history",
    "expand_aliases",
    "parse_amount",
    "classify_doc",
]
