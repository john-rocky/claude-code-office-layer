"""BackgroundIndexer integration test — temp dir, real watchdog events."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from office_layer.adapters.registry import build_registry, reset_registry  # noqa: E402
from office_layer.engine.engine import Engine  # noqa: E402
from office_layer.engine.watcher import DEBOUNCE_SECONDS  # noqa: E402
from office_layer.models import WorkspacePolicy  # noqa: E402
from office_layer.storage import Storage  # noqa: E402


def _make_engine(state_dir: Path) -> Engine:
    reset_registry()
    storage = Storage(state_dir / "office.sqlite")
    return Engine(storage=storage, registry=build_registry())


def test_watcher_picks_up_new_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    (workspace_root / "before.md").write_text("# before\nhello world")

    engine = _make_engine(state_dir)
    try:
        if not engine.background.is_supported():
            pytest.skip("file watcher not available")
        ws = engine.workspaces.add(
            name="t", root_path=str(workspace_root), policy=WorkspacePolicy.READ_ONLY
        )
        progress = engine.indexer.index_workspace(ws.id)
        assert progress.total_indexed == 1

        handle = engine.background.start(ws.id)
        try:
            # Drop a new file → watcher should pick it up after debounce.
            (workspace_root / "after.md").write_text("# after\nhello FUTURE")
            time.sleep(DEBOUNCE_SECONDS + 1.5)
            docs = engine.storage.list_documents(workspace_id=ws.id)
            names = {d.name for d in docs}
            assert "after.md" in names, f"watcher missed new file; got {names}"
            assert handle.events_processed >= 1
        finally:
            engine.background.stop(ws.id)
    finally:
        engine.close()


def test_watcher_drops_deleted_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    (workspace_root / "doomed.md").write_text("# doomed")

    engine = _make_engine(state_dir)
    try:
        if not engine.background.is_supported():
            pytest.skip("file watcher not available")
        ws = engine.workspaces.add(
            name="t", root_path=str(workspace_root), policy=WorkspacePolicy.READ_ONLY
        )
        engine.indexer.index_workspace(ws.id)
        assert any(d.name == "doomed.md" for d in engine.storage.list_documents(workspace_id=ws.id))

        engine.background.start(ws.id)
        try:
            (workspace_root / "doomed.md").unlink()
            time.sleep(DEBOUNCE_SECONDS + 1.5)
            names = {d.name for d in engine.storage.list_documents(workspace_id=ws.id)}
            assert "doomed.md" not in names, f"watcher kept stale doc; got {names}"
        finally:
            engine.background.stop(ws.id)
    finally:
        engine.close()
