"""Adapter Protocols.

Each Protocol describes the smallest surface needed by the engine. Concrete
adapters live in subpackages and guard their imports so that the package is
always importable even when an optional OSS dep is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Protocol, runtime_checkable

from ..models import (
    ChunkKind,
    Document,
    DocumentChunk,
    DocumentKind,
    ExtractedField,
    ExtractedTable,
    ExtractionMethod,
)


@dataclass
class DegradedMode:
    """Returned by adapter selectors when no full-featured adapter is available.

    ``reason`` carries a human-readable explanation that the engine surfaces to
    the user / Claude, and ``install_hint`` gives the exact ``pip install``
    command that would resolve it.
    """

    adapter_kind: str
    reason: str
    install_hint: str | None = None


@dataclass
class ExtractionResult:
    """Output of a Pdf/Office/Text/OCR extractor."""

    chunks: list[DocumentChunk] = field(default_factory=list)
    fields: list[ExtractedField] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    page_count: int | None = None
    sheet_count: int | None = None
    slide_count: int | None = None
    extraction_method: ExtractionMethod | None = None
    error: str | None = None
    language_hints: list[str] = field(default_factory=list)


@dataclass
class DiscoveredFile:
    path: Path
    size_bytes: int
    mtime: datetime


# -- File discovery -----------------------------------------------------------


@runtime_checkable
class FileDiscoveryAdapter(Protocol):
    """Walk a workspace and yield candidate files (filename-level only).

    Implementations: pure-python ``walk``, macOS ``mdfind``, Windows
    ``Everything``, ripgrep ``--files``, fd. Selection happens at runtime —
    fastest available wins, with the pure-Python walker as the always-on
    fallback (§17.4 File discovery / file name search).
    """

    name: str

    def is_available(self) -> bool: ...

    def iter_files(
        self,
        root: Path,
        *,
        include_extensions: Iterable[str] | None = None,
        exclude_globs: Iterable[str] = (),
        max_size_bytes: int | None = None,
    ) -> Iterator[DiscoveredFile]: ...


# -- Extraction ---------------------------------------------------------------


@runtime_checkable
class PdfExtractionAdapter(Protocol):
    name: str
    method: ExtractionMethod

    def is_available(self) -> bool: ...

    def extract(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult: ...


@runtime_checkable
class OfficeExtractionAdapter(Protocol):
    name: str
    supported_kinds: tuple[DocumentKind, ...]

    def is_available(self) -> bool: ...

    def extract(
        self,
        path: Path,
        kind: DocumentKind,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult: ...


@runtime_checkable
class TextExtractionAdapter(Protocol):
    """Lightweight stdlib path for plain-text formats (txt/md/csv/json/html)."""

    name: str
    supported_kinds: tuple[DocumentKind, ...]

    def is_available(self) -> bool: ...

    def extract(
        self,
        path: Path,
        kind: DocumentKind,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult: ...


@runtime_checkable
class OcrAdapter(Protocol):
    """OCR adapter for scanned PDFs / images.

    Off by default per §17.8 — engine only invokes OCR when the workspace
    policy enables it and an OCR adapter is available.
    """

    name: str
    method: ExtractionMethod

    def is_available(self) -> bool: ...

    def extract_image(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult: ...

    def extract_scanned_pdf(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult: ...


# -- Search -------------------------------------------------------------------


@runtime_checkable
class TextSearchAdapter(Protocol):
    name: str

    def is_available(self) -> bool: ...

    # The text-search adapter is intentionally implemented inside Storage for
    # the default SQLite FTS5 backend — see ``office_layer.storage``. This
    # Protocol exists so alternate ripgrep/tantivy backends can register
    # later without touching engine code.


@runtime_checkable
class SemanticSearchAdapter(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def add(self, chunk_id: str, text: str, metadata: dict | None = None) -> None: ...

    def query(
        self,
        text: str,
        *,
        workspace_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[tuple[str, float]]:
        """Return list of (chunk_id, score) ordered by score descending."""
        ...

    def remove(self, chunk_id: str) -> None: ...


# -- File watching ------------------------------------------------------------


@runtime_checkable
class FileWatcherAdapter(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def watch(self, root: Path, on_event: callable) -> object:
        """Begin watching. Returns an opaque handle with .stop()."""
        ...
