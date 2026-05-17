"""sqlite-vec adapter — capability marker for the semantic search backend.

The actual indexing + querying lives in
``office_layer.engine.semantic.SemanticIndex``, which calls Storage's
``vector_upsert`` / ``vector_query`` primitives. This adapter only signals
availability to the registry so ``office-layer status`` and the degraded-mode
banner show the right thing.

Availability has two prerequisites:
1. The ``sqlite-vec`` wheel is importable.
2. The local Python's ``sqlite3`` was built with loadable extensions
   (system Python on some Linux distros disables this).
"""

from __future__ import annotations

import sqlite3


class SqliteVecAdapter:
    name = "sqlite-vec"

    def __init__(self) -> None:
        self._reason: str | None = None
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self._available = False
            self._reason = "sqlite-vec wheel not installed"
            return
        # Loadable-extensions probe — catches Python builds compiled with
        # `--disable-loadable-extensions` (common on system Pythons).
        try:
            probe = sqlite3.connect(":memory:")
            probe.enable_load_extension(True)
            probe.close()
            self._available = True
        except (AttributeError, sqlite3.OperationalError) as exc:
            self._available = False
            self._reason = f"sqlite3 cannot load extensions: {exc}"

    def is_available(self) -> bool:
        return self._available

    @property
    def reason(self) -> str | None:
        return self._reason

    # The methods below exist to satisfy the SemanticSearchAdapter Protocol.
    # They are not called by the engine — SemanticIndex talks to Storage
    # directly, since wiring the embedder + workspace_id flow through a
    # Protocol surface adds plumbing without value.

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
