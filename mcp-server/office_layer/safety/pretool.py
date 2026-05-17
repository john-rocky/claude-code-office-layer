"""Pre-tool intercept — centralised HIGH-risk refuse for write workflows.

Workflows that produce or overwrite local artifacts (currently
:mod:`workflows.invoices_table` and :mod:`workflows.email_draft`) used to
inline the same three lines: classify the op, check ``risk.level ==
OperationRiskLevel.HIGH``, return a hand-rolled ``{"error": ..., "risk":
...}`` dict. With more write tools landing, that copy-paste was about to
appear a third time, so the refuse contract lives here.

Contract:

* ``intercept(operation, *, targets, workspace)`` returns ``(risk,
  refusal)`` where ``refusal`` is ``None`` for any non-HIGH result and a
  fully-formed JSON-shaped dict for HIGH. Callers branch on ``refusal is
  not None`` and otherwise keep ``risk`` to include in their success
  payload — so :func:`classify_operation` runs exactly once per call.
* The refusal dict's shape (``{"error": "refused: <op> classified as HIGH
  risk (<reasons>)", "risk": <model_dump json>}``) is what the MCP tool
  returns verbatim. It is also what the plugin-side PreToolUse hook
  ships back to Claude Code, so any future change to that shape must be
  made here only.
* No new safety rules. Anything that needs a fresh refuse trigger
  belongs in :mod:`safety.risk` so the CLI ``office-layer risk`` /
  ``office-layer risk intercept`` paths inherit it automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import OperationRisk, OperationRiskLevel, Workspace
from .risk import classify_operation


@dataclass(frozen=True)
class InterceptResult:
    """Outcome of a single pre-tool gate check.

    ``risk`` is always populated so the caller can echo it in the
    success path response. ``refusal`` is ``None`` for low/medium ops
    and the MCP-bound refusal dict for HIGH.
    """

    risk: OperationRisk
    refusal: dict[str, Any] | None

    @property
    def refused(self) -> bool:
        return self.refusal is not None


def build_refusal(risk: OperationRisk) -> dict[str, Any]:
    """Format the canonical refusal payload for a HIGH classification."""
    reasons = "; ".join(risk.reasons) if risk.reasons else "no reason given"
    return {
        "error": (
            f"refused: {risk.operation} classified as HIGH risk ({reasons})"
        ),
        "risk": risk.model_dump(mode="json"),
    }


def intercept(
    operation: str,
    *,
    targets: list[str] | None = None,
    workspace: Workspace | None = None,
) -> InterceptResult:
    """Classify an operation and produce a refusal payload if HIGH.

    Workflow callers spell this as::

        result = pretool.intercept("new_draft", targets=[str(p)], workspace=ws)
        if result.refusal is not None:
            return result.refusal
        # ... success path uses result.risk ...
    """
    risk = classify_operation(operation, targets=targets, workspace=workspace)
    refusal = build_refusal(risk) if risk.level == OperationRiskLevel.HIGH else None
    return InterceptResult(risk=risk, refusal=refusal)
