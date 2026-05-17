"""Workspace lifecycle — register, list, update policy, remove."""

from __future__ import annotations

from pathlib import Path

from ..models import Workspace, WorkspacePolicy, WorkspaceStatus
from ..storage import Storage


DEFAULT_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/.next/**",
    "**/dist/**",
    "**/build/**",
    "**/.DS_Store",
    "**/.office-layer-cache/**",
]


class WorkspaceManager:
    def __init__(self, storage: Storage):
        self.storage = storage

    def add(
        self,
        name: str,
        root_path: str | Path,
        *,
        policy: WorkspacePolicy = WorkspacePolicy.READ_ONLY,
        include_extensions: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        max_file_size_mb: int = 100,
        enable_ocr: bool = False,
        enable_vector_search: bool = False,
        pii_warning: bool = True,
    ) -> Workspace:
        root_path = str(Path(root_path).expanduser().resolve())
        # Idempotent on the same root.
        existing = self.storage.get_workspace_by_path(root_path)
        if existing is not None:
            existing.name = name
            existing.policy = policy
            existing.include_extensions = include_extensions
            existing.exclude_globs = exclude_globs or DEFAULT_EXCLUDES[:]
            existing.max_file_size_mb = max_file_size_mb
            existing.enable_ocr = enable_ocr
            existing.enable_vector_search = enable_vector_search
            existing.pii_warning = pii_warning
            self.storage.upsert_workspace(existing)
            return existing
        ws = Workspace(
            name=name,
            root_path=root_path,
            policy=policy,
            status=WorkspaceStatus.UNINDEXED,
            include_extensions=include_extensions,
            exclude_globs=exclude_globs or DEFAULT_EXCLUDES[:],
            max_file_size_mb=max_file_size_mb,
            enable_ocr=enable_ocr,
            enable_vector_search=enable_vector_search,
            pii_warning=pii_warning,
        )
        self.storage.upsert_workspace(ws)
        return ws

    def list(self) -> list[Workspace]:
        return self.storage.list_workspaces()

    def get(self, ws_id: str) -> Workspace | None:
        return self.storage.get_workspace(ws_id)

    def remove(self, ws_id: str) -> bool:
        return self.storage.delete_workspace(ws_id)

    def update_policy(self, ws_id: str, policy: WorkspacePolicy) -> Workspace | None:
        ws = self.storage.get_workspace(ws_id)
        if ws is None:
            return None
        ws.policy = policy
        self.storage.upsert_workspace(ws)
        return ws

    def set_vector_search(self, ws_id: str, enabled: bool) -> Workspace | None:
        ws = self.storage.get_workspace(ws_id)
        if ws is None:
            return None
        ws.enable_vector_search = enabled
        self.storage.upsert_workspace(ws)
        return ws
