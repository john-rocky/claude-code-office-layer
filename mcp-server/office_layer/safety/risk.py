"""Operation risk classifier (§9.10.1).

Operations the Office Layer recognises:
- ``search`` / ``read`` / ``extract`` / ``summarize`` / ``draft``    → low
- ``copy`` / ``export_csv`` / ``new_file_in_drafts``                 → medium
- ``overwrite`` / ``delete`` / ``send_email`` / ``upload_external``  → high
- ``bulk_modify``                                                    → medium
  + ``row_count > BULK_MODIFY_THRESHOLD`` → ``requires_confirmation``
- ``confirmable_overwrite``                                          → medium
  + always ``requires_confirmation`` (caller opts in explicitly)
- ``write_outside_workspace``                                        → high
"""

from __future__ import annotations

from pathlib import Path

from ..models import OperationRisk, OperationRiskLevel, Workspace, WorkspacePolicy


LOW_OPS = {"search", "read", "extract", "summarize", "draft", "list", "describe"}
MEDIUM_OPS = {"copy", "export_csv", "export_markdown", "export_json", "new_draft", "rename_proposal"}
HIGH_OPS = {"overwrite", "delete", "send_email", "upload_external"}

# Row count above which ``bulk_modify`` requires explicit caller
# confirmation. Kept here (single source) so the guard threshold is
# discoverable from one place; the workflow that surfaces a preview
# imports this constant.
BULK_MODIFY_THRESHOLD = 5


def classify_operation(
    operation: str,
    *,
    targets: list[str] | None = None,
    workspace: Workspace | None = None,
    row_count: int | None = None,
) -> OperationRisk:
    op = operation.lower().strip()
    targets = targets or []
    reasons: list[str] = []
    suggested: str | None = None
    confirmation_tripped = False

    if op == "bulk_modify":
        # Stays MEDIUM regardless of row_count. Refusing the natural batch
        # path would block the legitimate use case the tool exists for
        # (e.g. invoices_table aggregating N source documents into one
        # CSV). Instead, > BULK_MODIFY_THRESHOLD rows demand an explicit
        # caller acknowledgement via ``requires_confirmation`` — the
        # workflow writes a preview artifact and asks the caller to
        # re-run with confirm=True. Workspace-policy / outside-workspace
        # escalation below can still bump this to HIGH for the usual
        # reasons (read-only workspace, target outside the root).
        level = OperationRiskLevel.MEDIUM
        reasons.append(
            "operation 'bulk_modify' aggregates changes from multiple sources"
        )
        suggested = (
            "preview the staged rows, then re-run with explicit confirmation"
        )
        if row_count is not None and row_count > BULK_MODIFY_THRESHOLD:
            confirmation_tripped = True
            reasons.append(
                f"{row_count} rows exceeds the {BULK_MODIFY_THRESHOLD}-row safety threshold"
            )
    elif op == "confirmable_overwrite":
        # Caller is explicitly opting into the destructive-write path
        # (e.g. ``extract_invoices_to_table(..., overwrite=True)`` on a
        # target file that already exists). The bare ``overwrite`` op
        # stays in HIGH_OPS — it has no soft path — so callers signal
        # the explicit-consent variant with this separate op key.
        # Always MEDIUM + requires_confirmation: the workflow only ever
        # invokes this op when there is something to overwrite, and the
        # confirmation gate forces a two-step (preview → re-call with
        # confirm=True) flow before the destructive write happens.
        # Workspace-policy / outside-workspace escalation below still
        # bumps this to HIGH for the usual reasons — confirm=True
        # cannot cross those hard rules.
        level = OperationRiskLevel.MEDIUM
        reasons.append(
            "operation 'confirmable_overwrite' would replace an existing file"
        )
        suggested = (
            "review the staged diff, then re-run with explicit confirmation"
        )
        confirmation_tripped = True
    elif op in HIGH_OPS:
        level = OperationRiskLevel.HIGH
        reasons.append(f"operation '{op}' is high-risk")
        suggested = "split into a draft step + a separate explicit approval"
    elif op in MEDIUM_OPS:
        level = OperationRiskLevel.MEDIUM
        reasons.append(f"operation '{op}' creates new artifacts")
        suggested = "save to drafts/ rather than overwriting in place"
    elif op in LOW_OPS:
        level = OperationRiskLevel.LOW
    else:
        # Unknown operation → assume medium so user is asked.
        level = OperationRiskLevel.MEDIUM
        reasons.append(f"operation '{op}' is unknown; defaulting to medium risk")

    # Workspace policy escalates risk: read-only workspaces upgrade any write op.
    if workspace is not None and level != OperationRiskLevel.LOW:
        if workspace.policy in (WorkspacePolicy.READ_ONLY, "read-only") and op not in LOW_OPS:
            level = OperationRiskLevel.HIGH
            reasons.append(f"workspace '{workspace.name}' is read-only")

    # Target outside workspace root → escalate.
    if workspace is not None:
        root = Path(workspace.root_path).resolve()
        for t in targets:
            try:
                tp = Path(t).resolve()
                tp.relative_to(root)
            except ValueError:
                level = OperationRiskLevel.HIGH
                reasons.append(f"target '{t}' is outside workspace '{workspace.name}'")
                break
            except Exception:
                pass

    return OperationRisk(
        level=level,
        operation=op,
        targets=targets,
        reasons=reasons,
        requires_confirmation=(
            level == OperationRiskLevel.HIGH or confirmation_tripped
        ),
        suggested_safer_alternative=suggested,
    )


class RiskClassifier:
    """Stateless façade over :func:`classify_operation` for DI."""

    def classify(
        self,
        operation: str,
        *,
        targets: list[str] | None = None,
        workspace: Workspace | None = None,
        row_count: int | None = None,
    ) -> OperationRisk:
        return classify_operation(
            operation, targets=targets, workspace=workspace, row_count=row_count
        )
