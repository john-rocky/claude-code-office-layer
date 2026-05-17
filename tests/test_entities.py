"""Regex entity extractor tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.engine.entities import extract_entities  # noqa: E402


def kinds(es) -> set[str]:
    return {e.kind for e in es}


def values(es, kind: str) -> list[str]:
    return [e.text for e in es if e.kind == kind]


def test_money_yen() -> None:
    es = extract_entities("合計 ¥748,000 (税込)", document_id="d1")
    assert "money" in kinds(es)
    assert any("748,000" in v for v in values(es, "money"))


def test_money_man_yen() -> None:
    es = extract_entities("予算は 50万円 までで", document_id="d1")
    assert any("50万円" in v for v in values(es, "money"))


def test_money_usd() -> None:
    es = extract_entities("Cost is $4.99 per seat.", document_id="d1")
    assert any("$4.99" in v for v in values(es, "money"))


def test_date_jp() -> None:
    es = extract_entities("支払期限 2025/04/30 まで", document_id="d1")
    assert "date" in kinds(es)
    assert any("2025/04/30" in v for v in values(es, "date"))


def test_date_iso() -> None:
    es = extract_entities("Effective: 2026-05-17", document_id="d1")
    assert any("2026-05-17" in v for v in values(es, "date"))


def test_email() -> None:
    es = extract_entities("Contact us at hello@acme.co.jp anytime.", document_id="d1")
    assert "hello@acme.co.jp" in values(es, "email")


def test_phone_jp() -> None:
    es = extract_entities("Tel: 03-1234-5678", document_id="d1")
    assert any("1234-5678" in v for v in values(es, "phone"))


def test_url() -> None:
    es = extract_entities("See https://example.com/docs for details", document_id="d1")
    assert any("https://example.com" in v for v in values(es, "url"))


def test_org_jp() -> None:
    es = extract_entities("取引先: ACME 株式会社  住所: 東京", document_id="d1")
    assert any("株式会社" in v for v in values(es, "org"))


def test_org_en() -> None:
    es = extract_entities("Vendor: Apple Inc., Cupertino", document_id="d1")
    assert any("Apple Inc" in v for v in values(es, "org"))


def test_dedup() -> None:
    es = extract_entities("¥1,000 ¥1,000 ¥1,000", document_id="d1")
    # Same money string three times → one entity.
    assert len(values(es, "money")) == 1
