"""Local Engine — orchestrates discovery, extraction, indexing, search.

Per spec §9.8 / §10.3, the Engine is the only piece that owns process state
(SQLite handle, watcher threads, in-memory caches). MCP tools call thin
methods on the Engine; the Engine fans out to adapters via the registry.
"""

from .engine import Engine, get_engine

__all__ = ["Engine", "get_engine"]
