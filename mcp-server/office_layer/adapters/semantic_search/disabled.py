"""No-op semantic search adapter — used in degraded mode."""

from __future__ import annotations


class DisabledSemanticAdapter:
    name = "disabled"

    def is_available(self) -> bool:
        return False

    def add(self, chunk_id: str, text: str, metadata: dict | None = None) -> None:
        return

    def query(
        self,
        text: str,
        *,
        workspace_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[tuple[str, float]]:
        return []

    def remove(self, chunk_id: str) -> None:
        return
