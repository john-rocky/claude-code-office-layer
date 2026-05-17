"""Contract diff — Phase 2.

Pairs clauses across two indexed contract documents and classifies each
pair as ``identical`` / ``wording`` / ``substantive`` / ``added`` /
``removed``. Builds on :mod:`workflows.contract_sections` for the heading
detection so the two tools stay aligned on what counts as a clause.

Pairing strategy (cheap, deterministic, no LLM):

1. **Ordinal match.** A section whose parsed ordinal exists in both
   documents is paired up front. Re-numbered drafts rarely happen in
   practice and the NDA shapes we ship as fixtures hit this fast path.
2. **Title fallback.** Whatever the ordinal pass left unmatched is paired
   greedily by lowest title edit-distance (normalised char ratio) across
   the remaining pool, with a hard ceiling so unrelated clauses do not
   get glued together. Anything still unmatched after that surfaces as
   ``added`` (in B only) or ``removed`` (in A only).

Status thresholds operate on the clause **body** (heading + title are
expected to vary trivially across versions):

* ``identical``    — bodies match byte-for-byte after whitespace strip.
* ``wording``      — char-level edit ratio < 0.10 (typo fixes,
  punctuation, formatting tweaks).
* ``substantive``  — anything else; the diff is likely a real change of
  obligation (term, amount, liability cap, …).

The output is intentionally a plain dict (not a Pydantic model) — the
caller is the MCP layer which serialises straight to JSON. We do *not*
persist diff hunks: the comparison is a one-shot artifact, the inputs
already live in storage via ``extract_contract_sections``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from ..engine import get_engine
from ..models import Document, DocumentChunk
from .contract_sections import Section, extract_sections_from_text


# -- thresholds ---------------------------------------------------------------


# Edit ratio below this counts as a "wording" change (typo, punctuation)
# UNLESS a substantive marker check (digit set, negation flip) overrides.
_WORDING_RATIO = 0.10

# Title similarity floor for *any* pair — applies to both the ordinal
# match (to reject "ordinal collided but titles are unrelated", e.g. a
# new clause inserted upstream pushing the rest of the numbering down)
# and the title fallback (to reject "no real pair, just two leftovers").
_MIN_TITLE_SIM = 0.40

# Unified diff context size. Three lines either side reads comfortably for
# a clause body of a few dozen lines.
_DIFF_CONTEXT = 3

# Tokens that, when their set differs between the two clause bodies,
# always make the diff substantive even if the edit ratio is low.
# Numbers carry contractual weight ("3 年間" → "5 年間" is the canonical
# real change). Negations flip an obligation outright.
_DIGIT_RUN = re.compile(r"\d+(?:[.,]\d+)*")
_NEGATION_TOKENS = ("なし", "無し", "無", "ない")


# -- helpers ------------------------------------------------------------------


def _norm_body(body: str) -> str:
    """Normalise a clause body for byte-for-byte comparison: trim each
    line and drop blank lines, but keep order and inline punctuation. This
    is forgiving enough that re-saving a doc with mixed trailing
    whitespace does not flip every clause to ``wording``."""
    return "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())


def _edit_ratio(a: str, b: str) -> float:
    """Symmetric char-edit ratio in [0, 1]. 0 = identical, 1 = nothing in
    common. Uses :class:`difflib.SequenceMatcher` so the cost is O(n*m)
    but n, m are clause lengths (a few hundred chars at most) — fine."""
    if not a and not b:
        return 0.0
    sim = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return max(0.0, 1.0 - sim)


def _title_similarity(a: str, b: str) -> float:
    """Title similarity in [0, 1]. 1 = identical. Lowercase + strip so a
    cosmetic case change does not block a title-fallback pair."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _diff_hunks(body_a: str, body_b: str) -> list[str]:
    """Return the unified diff between the two bodies as a list of hunk
    blocks. Each block is a multi-line string starting with ``@@``. Empty
    list means the two bodies are byte-identical after normalisation."""
    a_lines = _norm_body(body_a).splitlines()
    b_lines = _norm_body(body_b).splitlines()
    if a_lines == b_lines:
        return []
    diff_iter = difflib.unified_diff(
        a_lines, b_lines, lineterm="", n=_DIFF_CONTEXT
    )
    lines = list(diff_iter)
    if not lines:
        return []
    # Drop the file headers (`--- ` / `+++ `) — the caller already knows
    # which doc is which.
    if lines and lines[0].startswith("---"):
        lines = lines[1:]
    if lines and lines[0].startswith("+++"):
        lines = lines[1:]
    # Split into hunk blocks (one block per ``@@`` header).
    hunks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if ln.startswith("@@"):
            if cur:
                hunks.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        hunks.append(cur)
    return ["\n".join(h) for h in hunks]


def _has_substantive_marker(a: str, b: str) -> bool:
    """Return True when the diff between ``a`` and ``b`` touches a kind of
    token that we never want to dismiss as "wording": digit runs and
    negation words. The cheap signal is "the SET of digit runs differs"
    or "the count of negation tokens differs" — easier than diffing word
    by word and good enough for clause-length bodies."""
    digits_a = set(_DIGIT_RUN.findall(a))
    digits_b = set(_DIGIT_RUN.findall(b))
    if digits_a != digits_b:
        return True
    for token in _NEGATION_TOKENS:
        if a.count(token) != b.count(token):
            return True
    return False


def _classify(body_a: str, body_b: str) -> tuple[str, float]:
    """Return ``(status, edit_ratio)`` for two paired clause bodies."""
    a = _norm_body(body_a)
    b = _norm_body(body_b)
    if a == b:
        return "identical", 0.0
    ratio = _edit_ratio(a, b)
    if _has_substantive_marker(a, b):
        return "substantive", ratio
    if ratio < _WORDING_RATIO:
        return "wording", ratio
    return "substantive", ratio


@dataclass(frozen=True)
class _Pair:
    a: Section | None
    b: Section | None


def _pair_sections(
    sections_a: list[Section], sections_b: list[Section]
) -> list[_Pair]:
    """Pair clauses across two documents. Result is ordered:

    1. ordinal matches in A's order
    2. title-fallback matches in A's order
    3. unmatched A (``removed``)
    4. unmatched B (``added``) in B's order

    Anything not paired by step 1 or 2 becomes a half-pair with None on
    the missing side, which the caller renders as ``added`` / ``removed``.
    """
    by_ord_b = {s.ordinal: s for s in sections_b}

    paired_a: set[int] = set()
    paired_b: set[int] = set()
    ordinal_pairs: list[_Pair] = []
    for s in sections_a:
        cand = by_ord_b.get(s.ordinal)
        if cand is None:
            continue
        # Reject the ordinal pair if titles disagree wildly — a renumbered
        # draft that inserted a new clause upstream will have a different
        # clause sitting at the same ordinal, and we want the title
        # fallback to do the work below rather than glue the wrong pair.
        if _title_similarity(s.title, cand.title) < _MIN_TITLE_SIM:
            continue
        ordinal_pairs.append(_Pair(s, cand))
        paired_a.add(s.ordinal)
        paired_b.add(s.ordinal)

    remaining_a = [s for s in sections_a if s.ordinal not in paired_a]
    remaining_b = [s for s in sections_b if s.ordinal not in paired_b]

    # Title fallback: greedy best-match. For each unmatched A clause,
    # find the unmatched B clause with the highest title similarity
    # above the floor, mark both as used, repeat.
    title_pairs: list[_Pair] = []
    pool_b = list(remaining_b)
    for sa in remaining_a:
        best: tuple[float, Section] | None = None
        for sb in pool_b:
            sim = _title_similarity(sa.title, sb.title)
            if sim < _MIN_TITLE_SIM:
                continue
            if best is None or sim > best[0]:
                best = (sim, sb)
        if best is not None:
            title_pairs.append(_Pair(sa, best[1]))
            pool_b.remove(best[1])

    matched_a_in_title = {p.a.ordinal for p in title_pairs if p.a is not None}
    unmatched_a = [s for s in remaining_a if s.ordinal not in matched_a_in_title]
    unmatched_b = [s for s in pool_b]

    return (
        ordinal_pairs
        + title_pairs
        + [_Pair(s, None) for s in unmatched_a]
        + [_Pair(None, s) for s in unmatched_b]
    )


# -- pure-function entrypoint -------------------------------------------------


def compare_sections(
    sections_a: list[Section], sections_b: list[Section]
) -> dict[str, Any]:
    """Pair + classify two pre-extracted section lists. Stateless;
    convenient for unit tests that do not want to wire a Storage instance.
    """
    pairs = _pair_sections(sections_a, sections_b)
    out_pairs: list[dict[str, Any]] = []
    summary = {
        "identical": 0,
        "wording": 0,
        "substantive": 0,
        "added": 0,
        "removed": 0,
    }
    for p in pairs:
        if p.a is None and p.b is not None:
            summary["added"] += 1
            out_pairs.append(
                {
                    "ordinal_a": None,
                    "ordinal_b": p.b.ordinal,
                    "title_a": None,
                    "title_b": p.b.title,
                    "heading_a": None,
                    "heading_b": p.b.heading,
                    "status": "added",
                    "edit_ratio": None,
                    "diff_hunks": _diff_hunks("", p.b.body),
                }
            )
            continue
        if p.b is None and p.a is not None:
            summary["removed"] += 1
            out_pairs.append(
                {
                    "ordinal_a": p.a.ordinal,
                    "ordinal_b": None,
                    "title_a": p.a.title,
                    "title_b": None,
                    "heading_a": p.a.heading,
                    "heading_b": None,
                    "status": "removed",
                    "edit_ratio": None,
                    "diff_hunks": _diff_hunks(p.a.body, ""),
                }
            )
            continue
        # Both sides present.
        assert p.a is not None and p.b is not None
        status, ratio = _classify(p.a.body, p.b.body)
        summary[status] += 1
        out_pairs.append(
            {
                "ordinal_a": p.a.ordinal,
                "ordinal_b": p.b.ordinal,
                "title_a": p.a.title,
                "title_b": p.b.title,
                "heading_a": p.a.heading,
                "heading_b": p.b.heading,
                "status": status,
                "edit_ratio": round(ratio, 4),
                "diff_hunks": [] if status == "identical" else _diff_hunks(p.a.body, p.b.body),
            }
        )
    return {"pairs": out_pairs, "summary": summary}


# -- document-level workflow --------------------------------------------------


def _load_sections_for_doc(document_id: str) -> tuple[Document | None, list[Section]]:
    engine = get_engine()
    doc = engine.storage.get_document(document_id)
    if doc is None:
        return None, []
    chunks: list[DocumentChunk] = engine.storage.get_chunks(document_id)
    if not chunks:
        return doc, []
    parts = [c.text for c in sorted(chunks, key=lambda c: c.ordinal)]
    text = "\n\n".join(p for p in parts if p)
    sections, _ = extract_sections_from_text(text)
    return doc, sections


def compare_contracts(doc_a_id: str, doc_b_id: str) -> dict[str, Any]:
    """Diff two indexed contract documents clause-by-clause.

    Returns ``{doc_a, doc_b, pairs, summary, section_count_a,
    section_count_b}``. Pairs are ordered: ordinal matches first, then
    title-fallback matches, then unmatched A (``removed``), then
    unmatched B (``added``).

    Both documents must already be indexed (chunks present). The
    extractor reuses the same heading regex as
    :func:`extract_contract_sections`, so a doc that sectioned cleanly
    there will section cleanly here without persisting anything new.
    """
    doc_a, sections_a = _load_sections_for_doc(doc_a_id)
    if doc_a is None:
        return {"error": f"document '{doc_a_id}' not found"}
    doc_b, sections_b = _load_sections_for_doc(doc_b_id)
    if doc_b is None:
        return {"error": f"document '{doc_b_id}' not found"}

    out = compare_sections(sections_a, sections_b)
    out["doc_a"] = doc_a.model_dump(mode="json")
    out["doc_b"] = doc_b.model_dump(mode="json")
    out["section_count_a"] = len(sections_a)
    out["section_count_b"] = len(sections_b)
    return out


__all__ = [
    "compare_contracts",
    "compare_sections",
]
