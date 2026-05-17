"""Background incremental indexing — watchdog Observer + debounced re-index.

The MCP layer can start / stop a watcher per workspace. While running, a
worker thread debounces file-system events (2s window) and dispatches
``Indexer.reindex_path`` or ``Storage.delete_document`` accordingly.

Watchers are off by default. Claude / users opt in via ``start_watch``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..adapters import AdapterRegistry
from ..models import Document
from ..storage import Storage
from .indexer import EXTENSION_KIND, Indexer

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0


@dataclass
class WatchHandle:
    workspace_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observer: object | None = None
    worker: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _pending: dict[str, str] = field(default_factory=dict)  # path -> event_type
    _lock: threading.Lock = field(default_factory=threading.Lock)
    events_processed: int = 0

    def stop(self) -> None:
        self._stop.set()
        if self.observer is not None:
            try:
                self.observer.stop()
                self.observer.join(timeout=5.0)
            except Exception:
                log.exception("observer stop failed")
        if self.worker is not None:
            self.worker.join(timeout=5.0)


class BackgroundIndexer:
    """One BackgroundIndexer per Engine; tracks watcher threads per workspace."""

    def __init__(self, storage: Storage, indexer: Indexer, registry: AdapterRegistry):
        self.storage = storage
        self.indexer = indexer
        self.registry = registry
        self._watches: dict[str, WatchHandle] = {}
        self._lock = threading.Lock()

    def is_supported(self) -> bool:
        return self.registry.file_watcher is not None and self.registry.file_watcher.is_available()

    def list_watches(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "workspace_id": h.workspace_id,
                    "started_at": h.started_at.isoformat(),
                    "events_processed": h.events_processed,
                    "alive": h.observer is not None,
                }
                for h in self._watches.values()
            ]

    def start(self, workspace_id: str) -> WatchHandle:
        if not self.is_supported():
            raise RuntimeError(
                "no FileWatcher adapter available — install watchdog (`pip install watchdog`)"
            )
        ws = self.storage.get_workspace(workspace_id)
        if ws is None:
            raise ValueError(f"workspace '{workspace_id}' not found")

        with self._lock:
            existing = self._watches.get(workspace_id)
            if existing is not None and existing.observer is not None:
                return existing

            handle = WatchHandle(workspace_id=workspace_id)

            def on_event(event) -> None:
                # watchdog FileSystemEvent has src_path / dest_path / event_type / is_directory
                if getattr(event, "is_directory", False):
                    return
                src = getattr(event, "src_path", None)
                etype = getattr(event, "event_type", "modified")
                if not src:
                    return
                p = Path(src)
                if p.suffix.lower() not in EXTENSION_KIND:
                    return
                with handle._lock:
                    handle._pending[str(p)] = etype
                    if etype == "moved":
                        dest = getattr(event, "dest_path", None)
                        if dest:
                            handle._pending[dest] = "created"

            handle.observer = self.registry.file_watcher.watch(Path(ws.root_path), on_event)
            handle.worker = threading.Thread(
                target=self._loop, args=(handle,), name=f"office-watcher-{workspace_id[:8]}",
                daemon=True,
            )
            handle.worker.start()
            self._watches[workspace_id] = handle
            log.info("started watcher for workspace %s @ %s", workspace_id, ws.root_path)
            return handle

    def stop(self, workspace_id: str) -> bool:
        with self._lock:
            handle = self._watches.pop(workspace_id, None)
        if handle is None:
            return False
        handle.stop()
        log.info("stopped watcher for workspace %s", workspace_id)
        return True

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._watches.keys())
        for wid in ids:
            self.stop(wid)

    # -- internal worker ------------------------------------------------------

    def _loop(self, handle: WatchHandle) -> None:
        while not handle._stop.is_set():
            # Debounce: snapshot pending, sleep DEBOUNCE_SECONDS, snapshot again,
            # only act on paths whose event types have stabilised.
            time.sleep(DEBOUNCE_SECONDS)
            with handle._lock:
                pending = dict(handle._pending)
                handle._pending.clear()
            if not pending:
                continue
            for path_str, event_type in pending.items():
                try:
                    self._handle_event(handle, Path(path_str), event_type)
                except Exception:
                    log.exception("watcher: failed to process %s", path_str)
                else:
                    handle.events_processed += 1

    def _handle_event(self, handle: WatchHandle, path: Path, event_type: str) -> None:
        if event_type == "deleted":
            doc_id = Document.make_id(handle.workspace_id, str(path))
            self.storage.delete_document(doc_id)
            log.info("watcher: deleted %s", path)
            return
        if not path.exists():
            return
        self.indexer.reindex_path(handle.workspace_id, path)
        log.info("watcher: re-indexed %s (%s)", path, event_type)
