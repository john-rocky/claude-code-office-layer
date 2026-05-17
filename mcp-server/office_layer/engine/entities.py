"""Regex-baseline entity extractor — Phase 1, no ML dep.

Pulls the entity kinds the office-work flows actually need:
- money (¥ / $ / € / 円 / 万円)
- date (Japanese + ISO + US formats)
- email / phone / url
- company-like names (◯◯株式会社 / ACME, Inc.)

Confidence is intentionally conservative — these are signals for the
hybrid ranker, not load-bearing factual claims. The Phase 2 spaCy /
GLiNER pass will replace this without changing the API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Entity


# -- patterns -----------------------------------------------------------------

_MONEY = re.compile(
    r"""(?xi)
    (?:
        [¥$€]\s?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?     # ¥1,200 / $4.99
        | \d{1,3}(?:[,，]\d{3})*\s*円                # 12,000 円
        | \d+(?:\.\d+)?\s*(?:万円|百万円|億円)        # 50万円 / 1.5億円
        | (?:USD|JPY|EUR|GBP|CNY)\s*\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?
    )
    """
)

_DATE = re.compile(
    r"""(?x)
    (?:
        \d{4}[-/年]\s?\d{1,2}[-/月]\s?\d{1,2}日?      # 2025-04-30 / 2025年4月30日
        | \d{1,2}[/-]\d{1,2}[/-]\d{2,4}              # 04/30/2025
        | \d{4}年\d{1,2}月                            # 2025年4月 (month only)
        | \d{1,2}月\d{1,2}日                          # 4月30日
    )
    """
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_PHONE = re.compile(
    r"""(?x)
    (?:
        \+?\d{1,3}[-\s.]?\(?\d{1,4}\)?[-\s.]?\d{2,4}[-\s.]?\d{3,4}    # +81-3-1234-5678
        | 0\d{1,4}-\d{1,4}-\d{3,4}                                    # 03-1234-5678
        | \(\d{2,4}\)\s?\d{3,4}-?\d{3,4}                              # (03) 1234-5678
    )
    """
)

_URL = re.compile(
    r"""(?ix)
    (?:
        https?://[^\s<>"'()]+
        | www\.[A-Za-z0-9.\-]+\.[A-Za-z]{2,}[^\s<>"'()]*
    )
    """
)

_JP_COMPANY = re.compile(
    r"""(?x)
    (?:
        (?:株式会社|有限会社|合同会社|合資会社|合名会社)\ ?[一-鿿ぁ-ヿ㐀-䶿A-Za-z0-9・\-]+
        | [一-鿿ぁ-ヿ㐀-䶿A-Za-z0-9・\-]+(?:\ )?(?:株式会社|有限会社|合同会社|社団法人)
    )
    """
)

_EN_COMPANY = re.compile(
    r"""(?x)
    \b
    (?:[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3})        # Capitalised words
    \s*
    (?:Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Corporation|GmbH|S\.A\.|Co\.,?\s*Ltd\.?|K\.K\.)
    \b
    """
)


# -- API ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Pattern:
    pattern: re.Pattern
    kind: str
    confidence: float


_ALL: list[_Pattern] = [
    _Pattern(_MONEY, "money", 0.85),
    _Pattern(_DATE, "date", 0.85),
    _Pattern(_EMAIL, "email", 0.95),
    _Pattern(_PHONE, "phone", 0.80),
    _Pattern(_URL, "url", 0.95),
    _Pattern(_JP_COMPANY, "org", 0.80),
    _Pattern(_EN_COMPANY, "org", 0.75),
]


def extract_entities(
    text: str,
    *,
    document_id: str,
    chunk_id: str | None = None,
    max_per_kind: int = 50,
) -> list[Entity]:
    """Return entities found in ``text``.

    Capped at ``max_per_kind`` per category so a 200-page contract does not
    push 10k phone-number hits through the indexer for no gain.
    """
    if not text:
        return []
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    out: list[Entity] = []
    for pat in _ALL:
        for m in pat.pattern.finditer(text):
            value = m.group(0).strip()
            if not value:
                continue
            key = (pat.kind, value.lower())
            if key in seen:
                continue
            seen.add(key)
            counts[pat.kind] = counts.get(pat.kind, 0) + 1
            if counts[pat.kind] > max_per_kind:
                continue
            out.append(
                Entity(
                    document_id=document_id,
                    chunk_id=chunk_id,
                    text=value,
                    kind=pat.kind,  # type: ignore[arg-type]
                    confidence=pat.confidence,
                )
            )
    return out
