"""Query understanding — natural-language → structured SearchQuery.

Phase 1 (§9.4.3). Pure-Python, regex + small calendar arithmetic. No LLM
call — the goal is to enrich the query *before* it hits the index, so we
do not burn Claude tokens on lookups Claude could not improve.

What we parse out:
- Period hints: 先月 / 去年 / 今年 / Q3 / 2025年4月 / 2025-04 / April 2025 / 2024
- Kind hints: 請求書 / 見積 / 契約 / 議事録 / spreadsheet / etc.
- Currency mentions are left in the text so FTS still hits them — only the
  *kind* + *date* fields move into structured filters.

Output: ``EnrichedQuery`` carrying both the original text and the inferred
filters. The hybrid searcher merges those into the SearchQuery it dispatches
to storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..models import DocumentKind, SearchQuery


# -- patterns -----------------------------------------------------------------

# Order matters: more specific patterns first.
_KIND_PATTERNS: list[tuple[re.Pattern, DocumentKind]] = [
    (re.compile(r"請求書|invoice|billing\s+statement", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"領収書|receipt", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"見積書?|estimate|quote(?:s|tation)?", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"契約書?|contract|agreement|NDA", re.IGNORECASE), DocumentKind.PDF),
    (re.compile(r"議事録|meeting\s*(?:notes|minutes)", re.IGNORECASE), DocumentKind.DOCX),
    (re.compile(r"提案書|proposal", re.IGNORECASE), DocumentKind.PPTX),
    (re.compile(r"プレゼン|スライド|slide(?:s|deck)?|powerpoint", re.IGNORECASE), DocumentKind.PPTX),
    (re.compile(r"スプレッドシート|spreadsheet|excel|エクセル|集計表", re.IGNORECASE), DocumentKind.XLSX),
    (re.compile(r"報告書|レポート|report\b", re.IGNORECASE), DocumentKind.DOCX),
]


_ABS_DATE_PATTERNS = [
    # 2025年4月 / 2025/04 / 2025-04 (year + month)
    re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})(?:月)?"),
    # 2025年 / 2025 alone — use lookarounds rather than \b so Japanese
    # follow-on chars (の, は, に…) don't suppress the match.
    re.compile(r"(?<!\d)(20\d{2})(?:年)?(?!\d)(?!\s*[-/]\s*\d)"),
]

_RELATIVE_PATTERNS = [
    re.compile(r"先月|last\s+month"),
    re.compile(r"今月|this\s+month"),
    re.compile(r"先週|last\s+week"),
    re.compile(r"今週|this\s+week"),
    re.compile(r"去年|昨年|last\s+year"),
    re.compile(r"今年|this\s+year"),
    re.compile(r"先々月|two\s+months\s+ago"),
]

_QUARTER_PATTERN = re.compile(r"\b(?:Q([1-4])|第([1-4])四半期)(?:\s*(\d{4}))?", re.IGNORECASE)

_EN_MONTH_PATTERN = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)

_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


# -- output -------------------------------------------------------------------


@dataclass
class EnrichedQuery:
    original: str
    cleaned_text: str
    kinds: list[DocumentKind] = field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def apply_to(self, query: SearchQuery) -> SearchQuery:
        """Return a copy of ``query`` with structured period filled in.

        Kind hints stay *soft* — they live in ``EnrichedQuery.kinds`` so the
        ranker can boost them, but they do NOT promote to ``query.kinds``
        (which is a hard FTS filter). Callers wanting a hard kind filter
        pass it explicitly.

        Date range, by contrast, IS a hard filter — when the user says
        "先月" we trust them and exclude documents outside that window.
        """
        return SearchQuery(
            text=query.text or self.cleaned_text,
            workspace_ids=query.workspace_ids,
            mode=query.mode,
            kinds=query.kinds,
            date_from=query.date_from or self.date_from,
            date_to=query.date_to or self.date_to,
            entities=query.entities,
            limit=query.limit,
            include_chunks=query.include_chunks,
        )


# -- parser -------------------------------------------------------------------


def parse(text: str, *, now: datetime | None = None) -> EnrichedQuery:
    """Pull period + kind hints out of a natural-language query."""
    now = now or datetime.now(timezone.utc)
    out = EnrichedQuery(original=text, cleaned_text=text)
    if not text:
        return out

    # Kinds — collect uniquely-ordered.
    seen: set[DocumentKind] = set()
    for pat, kind in _KIND_PATTERNS:
        if pat.search(text) and kind not in seen:
            out.kinds.append(kind)
            seen.add(kind)
            out.notes.append(f"kind hint: {pat.pattern} -> {kind.value}")

    # Period — first match wins; multiple period mentions are rare in office queries.
    period = _extract_period(text, now=now)
    if period:
        out.date_from, out.date_to, note = period
        out.notes.append(note)

    return out


def _extract_period(text: str, *, now: datetime) -> tuple[datetime, datetime, str] | None:
    """Return (from, to, human-note) or None."""
    # Relative phrases first (more specific than bare years).
    if re.search(r"先月|last\s+month", text, re.IGNORECASE):
        start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
        end = now.replace(day=1) - timedelta(seconds=1)
        return _utc_floor(start), _utc_ceil(end), "period: last month"
    if re.search(r"今月|this\s+month", text, re.IGNORECASE):
        start = now.replace(day=1)
        end = _end_of_month(start)
        return _utc_floor(start), _utc_ceil(end), "period: this month"
    if re.search(r"先々月|two\s+months\s+ago", text, re.IGNORECASE):
        first_of_now = now.replace(day=1)
        end = first_of_now - timedelta(seconds=1)
        last_month_first = end.replace(day=1)
        start = (last_month_first - timedelta(days=1)).replace(day=1)
        return _utc_floor(start), _utc_ceil(last_month_first - timedelta(seconds=1)), "period: two months ago"
    if re.search(r"先週|last\s+week", text, re.IGNORECASE):
        start_of_this_week = now - timedelta(days=now.weekday())
        end = start_of_this_week - timedelta(seconds=1)
        start = end - timedelta(days=6)
        return _utc_floor(start), _utc_ceil(end), "period: last week"
    if re.search(r"今週|this\s+week", text, re.IGNORECASE):
        start = now - timedelta(days=now.weekday())
        end = start + timedelta(days=6)
        return _utc_floor(start), _utc_ceil(end), "period: this week"
    if re.search(r"去年|昨年|last\s+year", text, re.IGNORECASE):
        y = now.year - 1
        return _utc_floor(datetime(y, 1, 1)), _utc_ceil(datetime(y, 12, 31)), f"period: year {y}"
    if re.search(r"今年|this\s+year", text, re.IGNORECASE):
        y = now.year
        return _utc_floor(datetime(y, 1, 1)), _utc_ceil(datetime(y, 12, 31)), f"period: year {y}"

    # Quarter
    qm = _QUARTER_PATTERN.search(text)
    if qm:
        q = int(qm.group(1) or qm.group(2))
        y = int(qm.group(3)) if qm.group(3) else now.year
        start_month = (q - 1) * 3 + 1
        start = datetime(y, start_month, 1)
        end_month = start_month + 2
        end = _end_of_month(datetime(y, end_month, 1))
        return _utc_floor(start), _utc_ceil(end), f"period: Q{q} {y}"

    # English month [+ year]
    em = _EN_MONTH_PATTERN.search(text)
    if em:
        month = _EN_MONTHS[em.group(1).lower()]
        y = int(em.group(2)) if em.group(2) else now.year
        start = datetime(y, month, 1)
        end = _end_of_month(start)
        return _utc_floor(start), _utc_ceil(end), f"period: {em.group(1)} {y}"

    # Year + month JP: 2025年4月 / 2025-04 / 2025/04
    for pat in _ABS_DATE_PATTERNS[:1]:
        m = pat.search(text)
        if m:
            y = int(m.group(1))
            mo = int(m.group(2))
            if 1 <= mo <= 12:
                start = datetime(y, mo, 1)
                end = _end_of_month(start)
                return _utc_floor(start), _utc_ceil(end), f"period: {y}-{mo:02d}"

    # Year only.
    m = _ABS_DATE_PATTERNS[1].search(text)
    if m:
        y = int(m.group(1))
        return _utc_floor(datetime(y, 1, 1)), _utc_ceil(datetime(y, 12, 31)), f"period: year {y}"

    return None


def _end_of_month(d: datetime) -> datetime:
    if d.month == 12:
        next_first = datetime(d.year + 1, 1, 1)
    else:
        next_first = datetime(d.year, d.month + 1, 1)
    return next_first - timedelta(seconds=1)


def _utc_floor(d: datetime) -> datetime:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _utc_ceil(d: datetime) -> datetime:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d
