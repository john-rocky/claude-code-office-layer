"""Contract section extractor — Phase 2.

Cuts a contract document into its clauses (`第N条` / `Section N` / `Article N`
/ numbered `N.` markdown headings) so downstream tools can pair clauses
across versions (:mod:`workflows.contract_diff`) or surface a specific
clause body in an evidence packet.

Pure-Python, regex-only. No LLM call, no extra deps.

Public surface:

- :func:`extract_sections_from_text` — pure function over a single text
  blob; returns `(sections, preamble)` for easy unit-testing.
- :func:`extract_contract_sections` — document-level entrypoint that loads
  chunks from storage, concatenates them in ordinal order, runs the
  extractor, optionally persists section title/body as ``ExtractedField``
  rows under the ``section.{ordinal}.*`` key namespace.

Persistence shape (when ``persist=True``):

* ``section.{n}.title``   — parsed clause title (the part after `第N条`)
* ``section.{n}.heading`` — full original heading line (sans markdown prefix)
* ``section.{n}.body``    — clause body, capped at ``body_cap`` chars
* ``section.{n}.body_truncated`` — written only when body was cut

The merge writer leaves non-``section.*`` extracted fields alone so a doc
that was previously :mod:`workflows.invoice`-extracted does not lose its
invoice fields when the sectioner runs over it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..engine import get_engine
from ..models import DocumentChunk, ExtractedField


# -- heading detection --------------------------------------------------------


# Markdown heading: leading #+ and at least one space.
_MD_PREFIX_RE = re.compile(r"^(#+)\s+")

# Clause markers — applied AFTER stripping any markdown prefix.
# JP: 第N条 (with optional full-width / half-width spacing inside).
_JP_CLAUSE_RE = re.compile(r"^\s*第\s*(\d+)\s*条\b\s*(.*)$")
# EN: Section N / Article N — optional separator, then title.
_EN_CLAUSE_RE = re.compile(
    r"^\s*(?:Section|Article|Clause)\s+(\d+)\b\s*[\.\):\-—–]?\s*(.*)$",
    re.IGNORECASE,
)
# Generic numbered heading: `1.` / `1)` / `1 -`. Tighter than the clause
# patterns because plain `1.` is a common list marker — accept only when
# followed by a title (not bare).
_NUM_HEAD_RE = re.compile(r"^\s*(\d+)\s*[\.\)]\s+(\S.+)$")

# Sentence terminator that means "this line is prose, not a heading".
# Applied to bare (non-markdown) clause-marker lines only.
_PROSE_TAIL = re.compile(r"[。．.]\s*\S")

# Heuristic max length for a bare-line heading. Beyond this we assume the
# line is prose that happens to start with `第N条`.
_MAX_BARE_HEADING_LEN = 60

# Strip noise around the title — parens, leading separator chars, etc.
_TITLE_NOISE_RE = re.compile(r"^[\s\(（【「『〈《\[]+|[\s\)）】」』〉》\]]+$")


@dataclass(frozen=True)
class Section:
    ordinal: int
    title: str
    heading: str
    body: str
    char_offset: int


def _clean_title(raw: str) -> str:
    s = raw.strip()
    # Repeatedly strip surrounding bracket/paren/separator noise.
    prev = None
    while prev != s:
        prev = s
        s = _TITLE_NOISE_RE.sub("", s).strip()
    return s


def _match_clause(content: str) -> tuple[int, str] | None:
    """Try every clause pattern against ``content`` (the line text with any
    markdown ``#+`` prefix already stripped). Returns ``(ordinal, title)``
    or None."""
    for pat in (_JP_CLAUSE_RE, _EN_CLAUSE_RE, _NUM_HEAD_RE):
        m = pat.match(content)
        if m:
            try:
                ordinal = int(m.group(1))
            except (TypeError, ValueError):
                continue
            title = _clean_title(m.group(2) or "")
            return ordinal, title
    return None


def _detect_heading(line: str) -> tuple[int, str, str] | None:
    """Classify a single line.

    Returns ``(ordinal, title, heading_text)`` if the line is a clause
    heading, else None. ``heading_text`` is the line with the markdown
    ``#+`` prefix stripped (kept verbatim for the section's ``heading``
    field so the caller still sees ``"第1条 (定義)"`` rather than just
    ``"定義"``).
    """
    md = _MD_PREFIX_RE.match(line)
    if md:
        inner = line[md.end():].rstrip()
        hit = _match_clause(inner)
        if hit is None:
            return None
        ordinal, title = hit
        return ordinal, title, inner

    # Bare line: be conservative. A heading-shaped line is short and has
    # no prose sentence terminator before the end.
    stripped = line.strip()
    if len(stripped) > _MAX_BARE_HEADING_LEN:
        return None
    if _PROSE_TAIL.search(stripped):
        return None
    hit = _match_clause(stripped)
    if hit is None:
        return None
    ordinal, title = hit
    return ordinal, title, stripped


# -- text-level extractor -----------------------------------------------------


def extract_sections_from_text(text: str) -> tuple[list[Section], str]:
    """Cut ``text`` into clauses.

    Returns ``(sections, preamble)``. ``preamble`` is the text before the
    first detected clause heading; empty when the document opens with a
    clause. ``sections`` are in document order — the ``ordinal`` field
    carries the *parsed* clause number (which usually matches but doesn't
    have to, e.g. an addendum that jumps from 第3条 to 第5条).
    """
    if not text:
        return [], ""

    lines = text.splitlines(keepends=True)
    # Compute the char offset of each line so the caller can locate a
    # section back into the original concatenated text.
    offsets: list[int] = []
    cur = 0
    for ln in lines:
        offsets.append(cur)
        cur += len(ln)

    headings: list[tuple[int, int, str, str]] = []  # (line_idx, ordinal, title, heading)
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        hit = _detect_heading(ln)
        if hit is None:
            continue
        ordinal, title, heading = hit
        headings.append((i, ordinal, title, heading))

    if not headings:
        return [], text

    preamble_end = offsets[headings[0][0]]
    preamble = text[:preamble_end].rstrip()

    sections: list[Section] = []
    for j, (line_idx, ordinal, title, heading) in enumerate(headings):
        body_start_line = line_idx + 1
        if j + 1 < len(headings):
            body_end_line = headings[j + 1][0]
        else:
            body_end_line = len(lines)
        body = "".join(lines[body_start_line:body_end_line]).strip()
        sections.append(
            Section(
                ordinal=ordinal,
                title=title,
                heading=heading,
                body=body,
                char_offset=offsets[line_idx],
            )
        )
    return sections, preamble


# -- document-level workflow --------------------------------------------------


_SECTION_KEY_PREFIX = "section."


def _merge_persist(storage, document_id: str, new_fields: list[ExtractedField]) -> None:
    """Rewrite ``section.*`` fields without disturbing other extractor's
    rows (e.g. invoice fields written by :mod:`workflows.invoice` on the
    same document)."""
    existing = storage.get_fields(document_id)
    kept = [f for f in existing if not f.key.startswith(_SECTION_KEY_PREFIX)]
    storage.replace_extracted_fields(document_id, kept + new_fields)


def _concat_chunks(chunks: list[DocumentChunk]) -> str:
    """Stitch chunks back into a single text in ordinal order. A blank
    line between chunks keeps a heading at a chunk boundary from being
    glued to the previous chunk's last paragraph."""
    parts = [c.text for c in sorted(chunks, key=lambda c: c.ordinal)]
    return "\n\n".join(p for p in parts if p)


def extract_contract_sections(
    document_id: str,
    *,
    persist: bool = True,
    body_cap: int = 4096,
) -> dict[str, Any]:
    """Cut one indexed contract document into clauses.

    ``body_cap`` is the per-section body character limit applied before
    persisting + returning. A section that hit the cap gets
    ``body_truncated=True`` and ``body_char_count`` reflects the *original*
    body length so the caller can tell how much was dropped. The default
    4 KB is enough for clauses up to ~2 K characters of Japanese kanji
    (~1 K English words) and keeps the MCP response under a comfortable
    size when 10+ sections are returned.
    """
    engine = get_engine()
    doc = engine.storage.get_document(document_id)
    if doc is None:
        return {"error": f"document '{document_id}' not found"}
    chunks: list[DocumentChunk] = engine.storage.get_chunks(document_id)
    if not chunks:
        return {
            "document": doc.model_dump(mode="json"),
            "preamble": "",
            "sections": [],
            "section_count": 0,
            "note": "no chunks indexed for this document — run `office-layer index` first",
        }

    text = _concat_chunks(chunks)
    sections, preamble = extract_sections_from_text(text)

    out_sections: list[dict[str, Any]] = []
    new_fields: list[ExtractedField] = []
    for sec in sections:
        original_len = len(sec.body)
        if original_len > body_cap:
            body = sec.body[:body_cap]
            truncated = True
        else:
            body = sec.body
            truncated = False
        out_sections.append(
            {
                "ordinal": sec.ordinal,
                "title": sec.title,
                "heading": sec.heading,
                "body": body,
                "char_offset": sec.char_offset,
                "body_char_count": original_len,
                "body_truncated": truncated,
            }
        )
        if persist:
            n = sec.ordinal
            new_fields.append(
                ExtractedField(
                    document_id=document_id,
                    key=f"section.{n}.title",
                    value=sec.title or sec.heading,
                    value_type="text",
                    confidence=0.9,
                    extractor="contract.section",
                )
            )
            new_fields.append(
                ExtractedField(
                    document_id=document_id,
                    key=f"section.{n}.heading",
                    value=sec.heading,
                    value_type="text",
                    confidence=0.9,
                    extractor="contract.section",
                )
            )
            new_fields.append(
                ExtractedField(
                    document_id=document_id,
                    key=f"section.{n}.body",
                    value=body,
                    value_type="text",
                    confidence=0.9,
                    extractor="contract.section",
                )
            )
            if truncated:
                new_fields.append(
                    ExtractedField(
                        document_id=document_id,
                        key=f"section.{n}.body_truncated",
                        value="true",
                        value_type="text",
                        confidence=0.9,
                        extractor="contract.section",
                    )
                )

    if persist:
        _merge_persist(engine.storage, document_id, new_fields)

    return {
        "document": doc.model_dump(mode="json"),
        "preamble": preamble,
        "sections": out_sections,
        "section_count": len(out_sections),
        "persisted": persist,
    }


__all__ = [
    "extract_contract_sections",
    "extract_sections_from_text",
    "Section",
]
