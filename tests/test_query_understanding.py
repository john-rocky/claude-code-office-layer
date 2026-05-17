"""Query-understanding parser tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.engine.query_understanding import parse  # noqa: E402
from office_layer.models import DocumentKind  # noqa: E402


NOW = datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_kind_invoice_japanese() -> None:
    eq = parse("A社の請求書をまとめて", now=NOW)
    assert DocumentKind.PDF in eq.kinds


def test_kind_contract_english() -> None:
    eq = parse("show me last year's NDA", now=NOW)
    assert DocumentKind.PDF in eq.kinds


def test_kind_spreadsheet() -> None:
    eq = parse("マーケのスプレッドシートを開いて", now=NOW)
    assert DocumentKind.XLSX in eq.kinds


def test_period_last_month() -> None:
    eq = parse("先月の請求", now=NOW)
    # NOW=2026-05-17 → 先月 = 2026-04-01..2026-04-30
    assert eq.date_from is not None
    assert eq.date_from.year == 2026
    assert eq.date_from.month == 4
    assert eq.date_to is not None
    assert eq.date_to.month == 4


def test_period_last_year() -> None:
    eq = parse("去年の契約書", now=NOW)
    assert eq.date_from is not None
    assert eq.date_from.year == 2025
    assert eq.date_to is not None
    assert eq.date_to.year == 2025


def test_period_quarter() -> None:
    eq = parse("Q3 2025 の議事録", now=NOW)
    assert eq.date_from is not None and eq.date_from.year == 2025 and eq.date_from.month == 7
    assert eq.date_to is not None and eq.date_to.month == 9


def test_period_english_month() -> None:
    eq = parse("April 2025 invoice from ACME", now=NOW)
    assert eq.date_from is not None and eq.date_from.year == 2025 and eq.date_from.month == 4


def test_period_yyyymm_jp() -> None:
    eq = parse("2025年4月のミーティング", now=NOW)
    assert eq.date_from is not None and eq.date_from.year == 2025 and eq.date_from.month == 4


def test_period_year_only() -> None:
    eq = parse("2024の請求まとめ", now=NOW)
    assert eq.date_from is not None and eq.date_from.year == 2024
    assert eq.date_to is not None and eq.date_to.year == 2024
