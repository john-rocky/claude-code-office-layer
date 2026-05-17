"""watchdog adapter — cross-platform file system events.

Used for incremental indexing. Cheap to keep running because watchdog
chooses OS-native watchers (FSEvents/inotify/ReadDirectoryChangesW) under
the hood.
"""

from __future__ import annotations

from pathlib import Path


class WatchdogAdapter:
    name = "watchdog"

    def __init__(self) -> None:
        try:
            import watchdog  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def watch(self, root: Path, on_event):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:
            raise RuntimeError(f"watchdog missing: {exc}") from exc

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                try:
                    on_event(event)
                except Exception:
                    pass

        observer = Observer()
        observer.schedule(_Handler(), str(root), recursive=True)
        observer.start()
        return observer
