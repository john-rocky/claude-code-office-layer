"""Tests for the centralised PreToolUse intercept."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from click.testing import CliRunner  # noqa: E402

from office_layer.cli import main as cli_main  # noqa: E402
from office_layer.models import (  # noqa: E402
    OperationRiskLevel,
    Workspace,
    WorkspacePolicy,
)
from office_layer.safety import build_refusal, intercept  # noqa: E402


# -- pure intercept ----------------------------------------------------------


def test_intercept_low_op_returns_none_refusal() -> None:
    result = intercept("search")
    assert result.refusal is None
    assert result.refused is False
    assert result.risk.level == OperationRiskLevel.LOW


def test_intercept_medium_op_returns_none_refusal(tmp_path: Path) -> None:
    ws = Workspace(name="d", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE)
    result = intercept("new_draft", targets=[str(tmp_path / "x.md")], workspace=ws)
    assert result.refusal is None
    assert result.risk.level == OperationRiskLevel.MEDIUM


def test_intercept_high_op_returns_refusal_payload() -> None:
    result = intercept("send_email")
    assert result.refused is True
    assert result.refusal is not None
    assert result.refusal["error"].startswith("refused: send_email classified as HIGH risk")
    assert result.refusal["risk"]["level"] == "high"


def test_intercept_outside_workspace_escalates_to_refusal(tmp_path: Path) -> None:
    ws = Workspace(name="d", root_path=str(tmp_path), policy=WorkspacePolicy.DRAFT_WRITE)
    result = intercept("new_draft", targets=["/etc/passwd"], workspace=ws)
    assert result.refusal is not None
    assert "outside workspace" in "; ".join(result.risk.reasons)
    assert result.refusal["risk"]["level"] == "high"


def test_intercept_read_only_workspace_refuses_writes(tmp_path: Path) -> None:
    ws = Workspace(name="d", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY)
    result = intercept("export_csv", targets=[str(tmp_path / "x.csv")], workspace=ws)
    assert result.refusal is not None
    assert "read-only" in "; ".join(result.risk.reasons)


def test_build_refusal_format_is_stable() -> None:
    risk = intercept("send_email").risk
    payload = build_refusal(risk)
    assert set(payload) == {"error", "risk"}
    assert payload["error"].startswith("refused: send_email classified as HIGH risk (")
    assert payload["risk"]["operation"] == "send_email"


# Workflow-level integration (read-only workspace + outside-workspace
# refusals) is already covered by tests/test_email_draft.py and
# tests/test_invoices_table.py — those tests exercise the same code
# path post-refactor and assert on the `error` / `risk` shape this
# module now owns. Keeping the unit tests above narrow.


# -- CLI hook entrypoint ------------------------------------------------------


def test_cli_risk_intercept_allows_low_op() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["risk-intercept", "search"])
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_risk_intercept_refuses_high_op_with_exit_2() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["risk-intercept", "send_email"])
    assert result.exit_code == 2
    # In Click 8.4 stderr is separated by default. The refusal JSON
    # should appear on stderr; result.output mirrors stderr when the
    # command writes nothing to stdout.
    err = result.stderr if result.stderr else result.output
    payload = json.loads(err.strip())
    assert payload["error"].startswith("refused: send_email")
    assert payload["risk"]["level"] == "high"


def test_cli_risk_classify_still_works() -> None:
    """The original ``office-layer risk <op>`` (classify-only) keeps working.

    ``risk_cmd`` prints through ``rich.Console`` which can wrap the JSON
    in formatting metadata; this just asserts the high-risk classification
    is surfaced rather than re-parsing the output.
    """
    runner = CliRunner()
    result = runner.invoke(cli_main, ["risk", "send_email"])
    assert result.exit_code == 0
    assert "high" in result.output
    assert "send_email" in result.output


def test_cli_risk_intercept_requires_operation() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["risk-intercept"])
    assert result.exit_code != 0
    assert "operation" in (result.output + (result.stderr or "")).lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
