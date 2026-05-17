"""Operation risk classifier (§9.10.1).

Operations the Office Layer recognises:
- ``search`` / ``read`` / ``extract`` / ``summarize`` / ``draft``    → low
- ``copy`` / ``export_csv`` / ``new_file_in_drafts``                 → medium
- ``overwrite`` / ``delete`` / ``send_email`` / ``upload_external``  → high
- ``bulk_modify`` / ``write_outside_workspace``                      → high
"""

from __future__ import annotations

from pathlib import Path

from ..models import OperationRisk, OperationRiskLevel, Workspace, WorkspacePolicy


LOW_OPS = {"search", "read", "extract", "summarize", "draft", "list", "describe"}
MEDIUM_OPS = {"copy", "export_csv", "export_markdown", "export_json", "new_draft", "rename_proposal"}
HIGH_OPS = {"overwrite", "delete", "send_email", "upload_external", "bulk_modify"}


def classify_operation(
    operation: str,
    *,
    targets: list[str] | None = None,
    workspace: Workspace | None = None,
) -> OperationRisk:
    op = operation.lower().strip()
    targets = targets or []
    reasons: list[str] = []
    suggested: str | None = None

    if op in HIGH_OPS:
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
        requires_confirmation=(level == OperationRiskLevel.HIGH),
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
    ) -> OperationRisk:
        return classify_operation(operation, targets=targets, workspace=workspace)
