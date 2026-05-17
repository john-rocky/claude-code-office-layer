"""Windows Everything (``es.exe``) adapter.

`Everything <https://www.voidtools.com/>`_ maintains an instant NTFS index.
The companion CLI ``es.exe`` is what we shell out to. Only enabled when the
binary is on PATH — Everything itself does not need to be running because
``es.exe`` requires the service to be installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..base import DiscoveredFile


class EverythingAdapter:
    name = "everything"

    def is_available(self) -> bool:
        return sys.platform.startswith("win") and shutil.which("es") is not None

    def iter_files(
        self,
        root: Path,
        *,
        include_extensions: Iterable[str] | None = None,
        exclude_globs: Iterable[str] = (),
        max_size_bytes: int | None = None,
    ) -> Iterator[DiscoveredFile]:
        exts = [e.lstrip(".").lower() for e in (include_extensions or [])]
        # ``es`` syntax: path:<root> ext:<csv>
        args = ["es", "-path", str(root)]
        if exts:
            args.extend(["ext:" + ";".join(exts)])
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
        except (subprocess.SubprocessError, FileNotFoundError):
            return
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            p = Path(line.strip())
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
