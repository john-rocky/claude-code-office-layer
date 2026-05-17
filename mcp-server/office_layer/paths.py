"""Per-user state paths.

The state directory holds SQLite database, audit log, and per-workspace cache.
Default location follows platformdirs (XDG on Linux, ``~/Library/Application
Support/...`` on macOS, ``%LOCALAPPDATA%`` on Windows). Override with
``OFFICE_LAYER_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_path


APP_NAME = "claude-code-office-layer"


def state_dir() -> Path:
    forced = os.environ.get("OFFICE_LAYER_HOME")
    if forced:
        p = Path(forced).expanduser().resolve()
    else:
        p = user_data_path(APP_NAME, appauthor=False)
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return state_dir() / "office_layer.sqlite"


def log_dir() -> Path:
    p = state_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = state_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def evidence_dir() -> Path:
    p = state_dir() / "evidence"
    p.mkdir(parents=True, exist_ok=True)
    return p
