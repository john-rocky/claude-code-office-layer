"""Email draft — Phase 2 stub.

Plan:
- Accept (recipient, intent, packet_id).
- Stage a `drafts/<timestamp>-<recipient>.md` file inside the workspace
  with: Subject, body, citations, "before sending" checklist.
- Never send. The safety-reviewer agent has the explicit rule that
  send_email is HIGH risk.
"""

from __future__ import annotations

from typing import Any


def draft_email_from_evidence(
    packet_id: str, *, recipient: str, intent: str
) -> dict[str, Any]:  # pragma: no cover - stub
    raise NotImplementedError("Phase 2 — see plugin/commands/office-draft-email.md")
