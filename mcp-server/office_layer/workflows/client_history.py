"""Client history — Phase 2 stub.

Plan:
- Expand client name into query variants (略称 / English equivalent / email
  domain), pulling from a small alias table the user maintains in
  workspace metadata.
- Search across all workspaces.
- Bucket by kind (契約 / 見積 / 請求 / 議事録 / メール / その他).
- Build per-bucket Evidence Packets.
- Emit a chronological Markdown summary with gap-flags
  ("見積はあるが対応する請求書はない" → 未請求候補).
"""

from __future__ import annotations

from typing import Any


def build(client_name: str, *, aliases: list[str] | None = None) -> dict[str, Any]:  # pragma: no cover
    raise NotImplementedError("Phase 2 — see plugin/commands/office-client-history.md")
