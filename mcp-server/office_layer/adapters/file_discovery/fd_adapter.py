"""``fd`` (`sharkdp/fd <https://github.com/sharkdp/fd>`_) adapter.

A modern user-friendly ``find`` replacement, MIT/Apache 2.0. Falls back to
``fdfind`` package name on Debian/Ubuntu.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..base import DiscoveredFile


class FdAdapter:
    name = "fd"

    def __init__(self) -> None:
        self._bin = shutil.which("fd") or shutil.which("fdfind")

    def is_available(self) -> bool:
        return self._bin is not None

    def iter_files(
        self,
        root: Path,
        *,
        include_extensions: Iterable[str] | None = None,
        exclude_globs: Iterable[str] = (),
        max_size_bytes: int | None = None,
    ) -> Iterator[DiscoveredFile]:
        if not self._bin:
            return
        args = [self._bin, "--type", "f", "--hidden", "--no-ignore-vcs"]
        for ext in include_extensions or []:
            args.extend(["-e", ext.lstrip(".")])
        for glob in exclude_globs:
            args.extend(["-E", glob])
        args.extend([".", str(root)])
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
        except (subprocess.SubprocessError, FileNotFoundError):
            return
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            p = Path(line)
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
