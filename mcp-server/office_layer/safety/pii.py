"""PII detector — Phase 3 §13.

Lightweight, offline, regex-only pass over text that is about to be
staged in a user-visible artifact (currently only the email draft, but
designed so other write tools can opt in cheaply). Surfaces three
buckets ranked by leak pain rather than detection difficulty:

1. ``mynumber`` — 個人番号 (12 digits). Legal exposure unique (PPC
   reportable if it leaves the company), so the rarest signal gets the
   strictest context check: requires a nearby ``マイナンバー`` /
   ``個人番号`` keyword and explicitly rejects when the upstream
   context says ``法人番号`` (corporate number, 13 digits, public).

2. ``credit_card`` — 13-19 digit run, separator-tolerant. Validated by
   Luhn AND a context blacklist (``INV`` / ``伝票`` / ``追跡`` /
   ``JAN`` / ``ISBN`` / ``978`` / ``979`` / ``法人番号``) within 20
   chars upstream. The blacklist matters because invoices routinely
   carry 13-16 digit codes (JAN, ISBN-13, 法人番号, internal IDs) that
   are NOT cards but trip naïve regexes constantly.

3. ``phone`` — JP shapes only (``0X-XXXX-XXXX`` fixed-line, ``0X0-XXXX
   -XXXX`` mobile, ``+81`` international). Length floor 10 digits so a
   7-digit postal code ``123-4567`` is rejected.

Anything that doesn't fit a real PII category but looks PII-shaped
(bank account numbers, postal codes, JAN codes, ISBN-13, tracking
numbers) is intentionally NOT scanned — they are public-by-design and
flagging them would noise the "before sending" checklist into
uselessness.

The scanner returns each hit with a `kind`, the matched span, and a
short `evidence` window so the calling workflow can render a
``Verify <kind> at <evidence>`` checklist row that points the human at
the exact spot to inspect before they hit send.

Returns are sorted ``(start, kind)`` so the same input always produces
the same ordering — checklist diffability matters when the draft is
regenerated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# --- public dataclass -------------------------------------------------------


@dataclass(frozen=True)
class PIIHit:
    """A single PII-shaped span detected in a body of text.

    ``kind`` is one of ``"mynumber"`` / ``"credit_card"`` / ``"phone"``.
    ``value`` preserves the original separators so the user can locate
    the string in the source. ``evidence`` is a short surrounding window
    rendered into the checklist row.
    """

    kind: str
    value: str
    start: int
    end: int
    evidence: str


# --- regex building blocks --------------------------------------------------

# 12-digit run with no adjacent digit on either side (so a 13-digit
# 法人番号 doesn't accidentally satisfy a 12-digit slice).
_MYNUMBER_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")

# Keywords that, when found within ±30 chars, confirm a 12-digit run
# really is 個人番号 rather than some other 12-digit code.
_MYNUMBER_KEYWORDS = ("マイナンバー", "個人番号", "個人No", "個人 No", "My Number", "MyNumber")

# Keyword that, when found within ±30 chars upstream, says "this is a
# 法人番号 (or similar public org ID), not 個人番号" — strong negative.
_CORP_NUMBER_NEGATIVES = ("法人番号", "事業者番号", "Corporate Number")

# 13-19 digit credit-card run, separator-tolerant. Allowed separators
# are space and hyphen. We intentionally match digit-only too because
# real cards in invoice PDFs sometimes lose their separators on text
# extraction.
_CREDIT_CARD_RE = re.compile(
    r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"
)

# Tokens that, within 20 chars upstream of a digit run, mean "this is
# definitely NOT a card" (it's a code that just happens to look like a
# 13-16 digit number). Case-insensitive.
_CC_NEGATIVE_TOKENS = (
    "INV",
    "伝票",
    "追跡",
    "JAN",
    "ISBN",
    "978",
    "979",
    "法人番号",
    "事業者番号",
    "商品コード",
    "tracking",
    "コード",
    "口座",  # bank account number context
    "普通",
    "当座",
)

# JP fixed-line: leading 0, then 1-4 area-code digits, separator, 4
# digits, separator, 4 digits. Strict on the digit grouping so we don't
# false-pos on `123-4567` (postal — total 7 digits) or `12-3456` (any
# 6-digit run).
_PHONE_JP_LANDLINE_RE = re.compile(
    r"(?<!\d)0\d{1,4}[- ]\d{1,4}[- ]\d{3,4}(?!\d)"
)
# JP mobile/IP: 070/080/090/050 prefix, separator, 4 digits, separator,
# 4 digits.
_PHONE_JP_MOBILE_RE = re.compile(
    r"(?<!\d)0[578]0[- ]\d{4}[- ]\d{4}(?!\d)"
)
# International +81 form. Allows space- or hyphen-separated groups.
_PHONE_INTL_RE = re.compile(
    r"(?<!\d)\+81[ -]?\d{1,4}[ -]?\d{1,4}[ -]?\d{3,4}(?!\d)"
)

# Phone digit-count floor — anything below this is too short to be a
# real phone (postal codes are 7 digits, JAN-8 is 8, etc.).
_PHONE_MIN_DIGITS = 10


# --- low-level helpers ------------------------------------------------------


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over the digit-only string."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total > 0 and total % 10 == 0


def _evidence(text: str, start: int, end: int, *, pad: int = 12) -> str:
    """Short window around the hit. Newlines collapsed so the checklist
    row stays single-line, and a leading/trailing ellipsis is added
    when the window is clipped."""
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    window = text[lo:hi].replace("\n", " ").strip()
    if lo > 0:
        window = "…" + window
    if hi < len(text):
        window = window + "…"
    return window


def _context_before(text: str, start: int, *, span: int) -> str:
    return text[max(0, start - span) : start]


def _context_window(text: str, start: int, end: int, *, span: int) -> str:
    lo = max(0, start - span)
    hi = min(len(text), end + span)
    return text[lo:hi]


# --- per-kind scanners ------------------------------------------------------


def _scan_mynumber(text: str) -> Iterable[PIIHit]:
    for m in _MYNUMBER_RE.finditer(text):
        window = _context_window(text, m.start(), m.end(), span=30)
        # Reject 12-digit runs that show up *next to* 法人番号 — that
        # context says the surrounding span is enumerating IDs of
        # multiple kinds, so the 12-digit one might be 個人番号, but
        # only if a positive mynumber keyword also appears.
        if any(neg in window for neg in _CORP_NUMBER_NEGATIVES) and not any(
            kw in window for kw in _MYNUMBER_KEYWORDS
        ):
            continue
        if not any(kw in window for kw in _MYNUMBER_KEYWORDS):
            continue
        yield PIIHit(
            kind="mynumber",
            value=m.group(0),
            start=m.start(),
            end=m.end(),
            evidence=_evidence(text, m.start(), m.end()),
        )


def _scan_credit_card(text: str) -> Iterable[PIIHit]:
    for m in _CREDIT_CARD_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[ -]", "", raw)
        if not (13 <= len(digits) <= 19):
            continue
        if not _luhn_ok(digits):
            continue
        # Context blacklist — both upstream and the immediate downstream
        # phrase are inspected because invoice rows sometimes spell
        # `4901234567890 JAN` (label trailing).
        upstream = _context_before(text, m.start(), span=20).lower()
        downstream = text[m.end() : m.end() + 8].lower()
        haystack = upstream + " " + downstream
        if any(tok.lower() in haystack for tok in _CC_NEGATIVE_TOKENS):
            continue
        yield PIIHit(
            kind="credit_card",
            value=raw,
            start=m.start(),
            end=m.end(),
            evidence=_evidence(text, m.start(), m.end()),
        )


def _scan_phone(text: str) -> Iterable[PIIHit]:
    seen: set[tuple[int, int]] = set()
    for regex in (_PHONE_JP_MOBILE_RE, _PHONE_JP_LANDLINE_RE, _PHONE_INTL_RE):
        for m in regex.finditer(text):
            span = (m.start(), m.end())
            if span in seen:
                continue
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) < _PHONE_MIN_DIGITS:
                continue
            seen.add(span)
            yield PIIHit(
                kind="phone",
                value=m.group(0),
                start=m.start(),
                end=m.end(),
                evidence=_evidence(text, m.start(), m.end()),
            )


# --- public entry point -----------------------------------------------------


def scan(text: str) -> list[PIIHit]:
    """Return all PII-shaped spans in ``text``, sorted by ``(start, kind)``.

    Empty or non-string input → empty list. Detector is offline,
    deterministic, and reentrant — safe to call repeatedly from a
    workflow's body-assembly step.
    """
    if not text:
        return []
    hits: list[PIIHit] = []
    hits.extend(_scan_mynumber(text))
    hits.extend(_scan_credit_card(text))
    hits.extend(_scan_phone(text))
    hits.sort(key=lambda h: (h.start, h.kind))
    return hits


__all__ = ["PIIHit", "scan"]
