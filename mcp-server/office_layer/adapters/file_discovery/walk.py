"""Pure-Python walker. Always available — degraded-mode safety net."""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..base import DiscoveredFile


class WalkAdapter:
    name = "walk"

    def is_available(self) -> bool:
        return True

    def iter_files(
        self,
        root: Path,
        *,
        include_extensions: Iterable[str] | None = None,
        exclude_globs: Iterable[str] = (),
        max_size_bytes: int | None = None,
    ) -> Iterator[DiscoveredFile]:
        root = root.expanduser().resolve()
        if not root.exists():
            return
        ext_filter = (
            {e.lower().lstrip(".") for e in include_extensions} if include_extensions else None
        )
        excludes = list(exclude_globs)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # prune excluded directories early
            dirnames[:] = [
                d for d in dirnames
                if not _matches_any(os.path.join(dirpath, d), excludes)
                and not d.startswith(".") or d in {".claude"}
            ]
            for fname in filenames:
                if fname.startswith(".DS_Store"):
                    continue
                full = Path(dirpath) / fname
                if ext_filter is not None and full.suffix.lower().lstrip(".") not in ext_filter:
                    continue
                if _matches_any(str(full), excludes):
                    continue
                try:
                    st = full.stat()
                except (OSError, PermissionError):
                    continue
                if max_size_bytes is not None and st.st_size > max_size_bytes:
                    continue
                yield DiscoveredFile(
                    path=full,
                    size_bytes=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                )


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)
