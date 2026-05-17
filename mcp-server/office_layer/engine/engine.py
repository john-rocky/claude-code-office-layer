"""Engine — singleton coordinator for storage + adapters + workflows."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..adapters import AdapterRegistry, get_registry
from ..paths import db_path
from ..storage import Storage
from .embedder import Embedder, select_embedder
from .indexer import Indexer
from .search import HybridSearcher
from .semantic import SemanticIndex
from .watcher import BackgroundIndexer
from .workspace_manager import WorkspaceManager

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        *,
        storage: Storage | None = None,
        registry: AdapterRegistry | None = None,
        embedder: Embedder | None = None,
    ):
        self.storage = storage or Storage(db_path())
        self.registry = registry or get_registry()
        self.embedder = embedder or select_embedder()
        self.semantic = SemanticIndex(self.storage, self.embedder)
        self.workspaces = WorkspaceManager(self.storage)
        self.indexer = Indexer(self.storage, self.registry, self.semantic)
        self.search = HybridSearcher(self.storage, self.registry, self.semantic)
        self.background = BackgroundIndexer(self.storage, self.indexer, self.registry)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.background.stop_all()
        self.storage.close()


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.close()
        _engine = None
