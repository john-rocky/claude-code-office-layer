"""Audit log helper.

Wraps Storage.append_audit() with structured args. Every MCP tool call that
touches storage / extracts a file / produces a draft passes through here so
the user has a recoverable trail (§9.10.3).
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import AuditLogEntry, OperationRiskLevel
from ..storage import Storage

log = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, storage: Storage):
        self.storage = storage

    def record(
        self,
        operation: str,
        *,
        tool: str | None = None,
        user_request: str | None = None,
        referenced_files: list[str] | None = None,
        output_files: list[str] | None = None,
        risk: OperationRiskLevel = OperationRiskLevel.LOW,
        user_approved: bool | None = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            operation=operation,
            tool=tool,
            user_request=user_request,
            referenced_files=referenced_files or [],
            output_files=output_files or [],
            risk=risk,
            user_approved=user_approved,
            error=error,
            extra=extra or {},
        )
        try:
            self.storage.append_audit(entry)
        except Exception:
            log.exception("failed to write audit log entry: %s", operation)
        return entry

    def recent(self, limit: int = 50) -> list[AuditLogEntry]:
        return self.storage.recent_audit(limit=limit)
