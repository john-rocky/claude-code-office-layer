"""Pre-tool intercept — centralised HIGH-risk refuse for write workflows.

Workflows that produce or overwrite local artifacts (currently
:mod:`workflows.invoices_table` and :mod:`workflows.email_draft`) used to
inline the same three lines: classify the op, check ``risk.level ==
OperationRiskLevel.HIGH``, return a hand-rolled ``{"error": ..., "risk":
...}`` dict. With more write tools landing, that copy-paste was about to
appear a third time, so the refuse contract lives here.

Contract:

* ``intercept(operation, *, targets, workspace, row_count=None, confirm=False)``
  returns an :class:`InterceptResult` with three orthogonal fields: ``risk``
  is always populated, ``refusal`` carries the canonical refusal dict for
  any HIGH classification, ``confirmation`` carries a "needs explicit
  acknowledgement" dict for MEDIUM ops where ``requires_confirmation`` is
  set (today: only ``bulk_modify`` past the row-count threshold). Callers
  branch on ``refused`` first, then ``needs_confirmation``, then proceed.
* The refusal dict's shape (``{"error": "refused: <op> classified as HIGH
  risk (<reasons>)", "risk": <model_dump json>}``) is what the MCP tool
  returns verbatim. It is also what the plugin-side PreToolUse hook
  ships back to Claude Code, so any future change to that shape must be
  made here only.
* The confirmation dict (``{"confirmation_required": true, "reason": ...,
  "row_count": N, "threshold": 5, "how_to_proceed": ...,
  "risk": <model_dump>}``) is the parallel "soft block" shape: the
  workflow stages a preview artifact and surfaces this dict so the
  caller (Claude / CLI) can prompt the user and re-run with
  ``confirm=True``. Refuse path is reserved for genuine HIGH (write-
  outside-workspace, read-only policy) — refusing the legitimate batch
  export would block the tool's purpose.
* No new safety rules. Anything that needs a fresh refuse or
  confirmation trigger belongs in :mod:`safety.risk` so the CLI
  ``office-layer risk`` / ``office-layer risk-intercept`` paths inherit
  it automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import OperationRisk, OperationRiskLevel, Workspace
from .risk import BULK_MODIFY_THRESHOLD, classify_operation


@dataclass(frozen=True)
class InterceptResult:
    """Outcome of a single pre-tool gate check.

    ``risk`` is always populated so the caller can echo it in the
    success path response. ``refusal`` is ``None`` for low/medium ops
    and the MCP-bound refusal dict for HIGH. ``confirmation`` is set
    when the operation classified as MEDIUM but
    ``risk.requires_confirmation`` is true (currently only
    ``bulk_modify`` past the row-count threshold) and the caller has
    not yet acknowledged via ``confirm=True``.
    """

    risk: OperationRisk
    refusal: dict[str, Any] | None
    confirmation: dict[str, Any] | None = None

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    @property
    def needs_confirmation(self) -> bool:
        return self.confirmation is not None


def build_refusal(risk: OperationRisk) -> dict[str, Any]:
    """Format the canonical refusal payload for a HIGH classification."""
    reasons = "; ".join(risk.reasons) if risk.reasons else "no reason given"
    return {
        "error": (
            f"refused: {risk.operation} classified as HIGH risk ({reasons})"
        ),
        "risk": risk.model_dump(mode="json"),
    }


def build_confirmation(
    risk: OperationRisk, *, row_count: int | None
) -> dict[str, Any]:
    """Format the canonical confirmation-required payload.

    Mirrors :func:`build_refusal` in shape (top-level ``risk`` key + a
    human-readable ``reason``) so callers can render either uniformly.
    Adds ``row_count`` / ``threshold`` for the bulk_modify case and a
    ``how_to_proceed`` hint pointing at the ``confirm`` arg / CLI flag.
    """
    if row_count is not None:
        reason = (
            f"{risk.operation} of {row_count} rows exceeds the "
            f"{BULK_MODIFY_THRESHOLD}-row safety threshold"
        )
    else:
        reason = f"operation '{risk.operation}' requires explicit confirmation"
    return {
        "confirmation_required": True,
        "reason": reason,
        "row_count": row_count,
        "threshold": BULK_MODIFY_THRESHOLD,
        "how_to_proceed": (
            "review the staged preview, then re-run with confirm=true (MCP) "
            "or --confirm (CLI)"
        ),
        "risk": risk.model_dump(mode="json"),
    }


def intercept(
    operation: str,
    *,
    targets: list[str] | None = None,
    workspace: Workspace | None = None,
    row_count: int | None = None,
    confirm: bool = False,
) -> InterceptResult:
    """Classify an operation and produce a refusal or confirmation payload.

    Workflow callers spell this as::

        result = pretool.intercept(
            "bulk_modify",
            targets=[str(p)],
            workspace=ws,
            row_count=len(rows),
            confirm=confirm,
        )
        if result.refusal is not None:
            return result.refusal  # HIGH — bail
        if result.confirmation is not None:
            preview_path = _write_preview(...)
            return {**result.confirmation, "preview_path": str(preview_path)}
        # ... success path uses result.risk ...

    ``confirm=True`` skips the confirmation gate (caller has
    acknowledged) but does NOT downgrade a genuine HIGH refusal — that
    would defeat the purpose of the classifier.
    """
    risk = classify_operation(
        operation, targets=targets, workspace=workspace, row_count=row_count
    )
    if risk.level == OperationRiskLevel.HIGH:
        return InterceptResult(
            risk=risk, refusal=build_refusal(risk), confirmation=None
        )
    if risk.requires_confirmation and not confirm:
        return InterceptResult(
            risk=risk,
            refusal=None,
            confirmation=build_confirmation(risk, row_count=row_count),
        )
    return InterceptResult(risk=risk, refusal=None, confirmation=None)
