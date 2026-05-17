"""sqlite-vec adapter — lightweight vector index in the same SQLite file.

Picked as the default vector backend when the user opts in: keeps state in
one file alongside metadata + FTS5, no daemon, no extra storage. Requires
the ``sqlite-vec`` python wheel which loads a SQLite extension.

Embedding model is intentionally NOT bundled — engine accepts arbitrary
``add(chunk_id, embedding)`` style hooks. A separate component (Phase 1)
provides embedding via sentence-transformers if user enables it.
"""

from __future__ import annotations

from typing import Iterable


class SqliteVecAdapter:
    name = "sqlite-vec"

    def __init__(self) -> None:
        try:
            import sqlite_vec  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def add(self, chunk_id: str, text: str, metadata: dict | None = None) -> None:
        # Engine wires the actual sqlite-vec table; this adapter only signals
        # availability. The hybrid ranker calls into Storage.vector_query()
        # directly when this adapter is the chosen one.
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
