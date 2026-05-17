"""macOS Spotlight (``mdfind``) adapter.

Spotlight has an OS-maintained index that is much faster than a fresh walk on
large homedirs. Only used for the initial candidate scan — the engine still
checks mtime/size after to confirm.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..base import DiscoveredFile


class MdfindAdapter:
    name = "mdfind"

    def is_available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("mdfind") is not None

    def iter_files(
        self,
        root: Path,
        *,
        include_extensions: Iterable[str] | None = None,
        exclude_globs: Iterable[str] = (),
        max_size_bytes: int | None = None,
    ) -> Iterator[DiscoveredFile]:
        # Build Spotlight expression: kMDItemContentTypeTree filter, scoped to root.
        exts = [e.lstrip(".").lower() for e in (include_extensions or [])]
        ext_expr = (
            " || ".join(f"kMDItemFSName == '*.{e}'c" for e in exts) if exts else "kMDItemFSName == '*'c"
        )
        query = f"({ext_expr})"
        cmd = ["mdfind", "-onlyin", str(root), query]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except (subprocess.SubprocessError, FileNotFoundError):
            return
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            p = Path(line)
            if not p.is_file():
                continue
            if any(p.match(g) for g in exclude_globs):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if max_size_bytes is not None and st.st_size > max_size_bytes:
                continue
            yield DiscoveredFile(
                path=p,
                size_bytes=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            )
