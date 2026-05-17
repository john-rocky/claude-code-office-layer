"""Smoke tests for the safety risk classifier."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.models import OperationRiskLevel, Workspace, WorkspacePolicy  # noqa: E402
from office_layer.safety import classify_operation  # noqa: E402


def test_search_is_low() -> None:
    risk = classify_operation("search")
    assert risk.level == OperationRiskLevel.LOW
    assert risk.requires_confirmation is False


def test_send_email_is_high() -> None:
    risk = classify_operation("send_email")
    assert risk.level == OperationRiskLevel.HIGH
    assert risk.requires_confirmation is True


def test_csv_export_is_medium() -> None:
    risk = classify_operation("export_csv")
    assert risk.level == OperationRiskLevel.MEDIUM


def test_target_outside_workspace_escalates(tmp_path: Path) -> None:
    ws = Workspace(
        name="docs",
        root_path=str(tmp_path),
        policy=WorkspacePolicy.DRAFT_WRITE,
    )
    risk = classify_operation(
        "copy",
        targets=["/etc/passwd"],
        workspace=ws,
    )
    assert risk.level == OperationRiskLevel.HIGH
    assert any("outside workspace" in r for r in risk.reasons)


def test_read_only_workspace_blocks_medium_op(tmp_path: Path) -> None:
    ws = Workspace(name="docs", root_path=str(tmp_path), policy=WorkspacePolicy.READ_ONLY)
    risk = classify_operation("export_csv", targets=[str(tmp_path / "out.csv")], workspace=ws)
    assert risk.level == OperationRiskLevel.HIGH
    assert any("read-only" in r for r in risk.reasons)
