"""Tests for the PII detector — Phase 3 §13.

The detector has three buckets ranked by leak pain:

1. ``mynumber`` — strict, keyword-anchored. False positives are the
   easiest to suppress (12-digit runs are rare), and missing one is the
   worst leak (PPC-reportable).
2. ``credit_card`` — Luhn-validated + context blacklist. The blacklist
   is the bit that pays for itself: invoices routinely carry 13-16
   digit codes (JAN, ISBN-13, 法人番号, tracking) that naïve regexes
   surface constantly.
3. ``phone`` — JP shapes only, ≥10-digit floor. Postal codes (7
   digits) must not fire.

Each test names the priority bucket it covers so it's obvious from the
suite read-out which guard regressed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.safety.pii import PIIHit, scan  # noqa: E402


# -- mynumber (rank 1) --------------------------------------------------------


def test_mynumber_hit_requires_keyword_anchor():
    """A bare 12-digit run with no JP context must NOT fire — most
    long digit runs in invoices are non-PII codes."""
    text = "請求書番号: 123456789012\n発行日: 2026/04/01"
    assert scan(text) == []


def test_mynumber_hit_fires_with_my_number_keyword():
    text = "マイナンバー: 123456789012\n所属: 経理部"
    hits = scan(text)
    assert any(h.kind == "mynumber" and h.value == "123456789012" for h in hits)


def test_mynumber_hit_fires_with_kojin_bango_keyword():
    text = "個人番号 (マイナンバー): 123456789012"
    kinds = [h.kind for h in scan(text)]
    assert kinds.count("mynumber") == 1


def test_mynumber_negative_when_only_houjin_bango_context():
    """法人番号 (corporate number) is 13 digits and public; a 12-digit
    run next to it must not be misread as 個人番号."""
    text = "法人番号: 1234567890123\n参考番号: 123456789012"
    assert all(h.kind != "mynumber" for h in scan(text))


def test_mynumber_does_not_fire_on_13_digit_neighbour():
    """法人番号 itself is 13 digits — the 12-digit regex must reject it
    via the digit-boundary lookarounds."""
    text = "マイナンバー: 1234567890123"  # 13 digits, not 12
    assert all(h.kind != "mynumber" for h in scan(text))


# -- credit card (rank 2) -----------------------------------------------------


def test_credit_card_hit_on_valid_luhn():
    """Visa test number 4111-1111-1111-1111 passes Luhn → must fire."""
    hits = scan("Card: 4111-1111-1111-1111")
    kinds = [h.kind for h in hits]
    assert "credit_card" in kinds


def test_credit_card_negative_on_invalid_luhn_16_digits():
    """Tracking number 1234-5678-9012-3456 has 16 digits but fails Luhn
    → must NOT fire. This is the canonical 'looks like a card but isn't'
    pattern in our probe fixture."""
    text = "追跡番号: 1234-5678-9012-3456"
    assert all(h.kind != "credit_card" for h in scan(text))


def test_credit_card_negative_on_invoice_id_with_separators():
    """INV-1234-5678 is 8 digits total — too short for a card. Even if
    we had a 16-digit invoice ID, the upstream ``INV`` token must block
    it. The probe fixture used this exact shape."""
    assert all(h.kind != "credit_card" for h in scan("請求書番号: INV-1234-5678"))


def test_credit_card_negative_on_jan_code():
    """JAN code 4901234567890 (13 digits) must not be flagged — invoices
    routinely include product codes."""
    text = "商品コード: 4901234567890"
    assert all(h.kind != "credit_card" for h in scan(text))


def test_credit_card_negative_on_isbn_13():
    """ISBN-13 starts with 978/979 — must not be flagged."""
    text = "書籍 ISBN: 9784123456789"
    assert all(h.kind != "credit_card" for h in scan(text))


def test_credit_card_negative_on_houjin_bango():
    """法人番号 is 13 digits and might pass Luhn by chance — context
    token must suppress it."""
    text = "法人番号: 1234567890123"
    assert all(h.kind != "credit_card" for h in scan(text))


def test_credit_card_negative_on_bank_account_context():
    """7-digit bank account numbers (often paired with 普通/当座) are
    nowhere near card-shape, but if the parser ever joins them with a
    branch code, the 口座 context blocks it."""
    text = "振込先: みずほ銀行 新宿支店 普通 1234567 スズキ"
    assert all(h.kind != "credit_card" for h in scan(text))


# -- phone (rank 3) -----------------------------------------------------------


def test_phone_hit_on_jp_landline():
    hits = scan("代表電話: 03-1234-5678")
    assert any(h.kind == "phone" and h.value == "03-1234-5678" for h in hits)


def test_phone_hit_on_jp_mobile():
    hits = scan("携帯: 090-1234-5678")
    assert any(h.kind == "phone" and h.value == "090-1234-5678" for h in hits)


def test_phone_hit_on_intl_jp():
    hits = scan("Tel: +81 3 4567 8910")
    assert any(h.kind == "phone" for h in hits)


def test_phone_negative_on_postal_code():
    """123-4567 is a postal code (7 digits total) — phone regex's ≥10
    digit floor rejects it."""
    assert all(h.kind != "phone" for h in scan("郵便番号: 123-4567"))


def test_phone_negative_on_date_string():
    """ISO dates like 2026-02-15 must not be misread as phones."""
    assert all(h.kind != "phone" for h in scan("発行日: 2026-02-15"))


def test_phone_fax_line_still_fires_as_phone():
    """FAX numbers are phone-shaped — flagging them as phone is the
    right behaviour even though the label says FAX. The user sees the
    evidence window and decides."""
    hits = scan("FAX: 03-1234-9999")
    assert any(h.kind == "phone" for h in hits)


# -- ordering + invariants ----------------------------------------------------


def test_hits_sorted_by_start_offset():
    """Diffability: same input must produce the same checklist order."""
    text = (
        "マイナンバー: 123456789012\n"
        "電話: 03-1234-5678\n"
        "Card: 4111-1111-1111-1111\n"
    )
    hits = scan(text)
    offsets = [h.start for h in hits]
    assert offsets == sorted(offsets)


def test_empty_input_returns_empty_list():
    assert scan("") == []
    assert scan(None) == []  # type: ignore[arg-type]


def test_evidence_window_is_single_line():
    """The evidence string must not contain raw newlines — it goes into
    a single markdown checklist row."""
    text = "前段の文章\n電話: 03-1234-5678\n後段の文章"
    hits = scan(text)
    phone = next(h for h in hits if h.kind == "phone")
    assert "\n" not in phone.evidence
