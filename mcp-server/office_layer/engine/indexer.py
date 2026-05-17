"""Indexer — turns workspace files into searchable chunks.

Pipeline per file:
1. classify by extension (DocumentKind)
2. pick the cheapest adapter that supports the kind
3. extract → chunks + tables + fields
4. persist via Storage (which triggers FTS5 inserts)
5. mark Document.indexed_at

For Phase 0 this runs sequentially in the caller's thread. Phase 1 will add a
background worker; the public surface stays the same.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..adapters import AdapterRegistry
from ..models import (
    Document,
    DocumentKind,
    ExtractionMethod,
    Workspace,
    WorkspaceStatus,
)
from ..storage import Storage
from .entities import extract_entities

log = logging.getLogger(__name__)


# Map file extension → DocumentKind. Kept here (rather than in registry) so
# the indexer is the single source of truth for what we will even try to read.
EXTENSION_KIND: dict[str, DocumentKind] = {
    ".pdf": DocumentKind.PDF,
    ".docx": DocumentKind.DOCX,
    ".xlsx": DocumentKind.XLSX,
    ".xlsm": DocumentKind.XLSX,
    ".pptx": DocumentKind.PPTX,
    ".csv": DocumentKind.CSV,
    ".tsv": DocumentKind.TSV,
    ".txt": DocumentKind.TXT,
    ".md": DocumentKind.MARKDOWN,
    ".markdown": DocumentKind.MARKDOWN,
    ".html": DocumentKind.HTML,
    ".htm": DocumentKind.HTML,
    ".json": DocumentKind.JSON,
    ".xml": DocumentKind.XML,
    ".eml": DocumentKind.EMAIL,
    ".png": DocumentKind.IMAGE,
    ".jpg": DocumentKind.IMAGE,
    ".jpeg": DocumentKind.IMAGE,
    ".webp": DocumentKind.IMAGE,
    ".tiff": DocumentKind.IMAGE,
    ".tif": DocumentKind.IMAGE,
    ".heic": DocumentKind.IMAGE,
}


@dataclass
class IndexProgress:
    total_seen: int = 0
    total_indexed: int = 0
    total_skipped: int = 0
    total_errors: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class Indexer:
    def __init__(self, storage: Storage, registry: AdapterRegistry):
        self.storage = storage
        self.registry = registry

    # -- public ---------------------------------------------------------------

    def index_workspace(
        self,
        workspace_id: str,
        *,
        force: bool = False,
        max_files: int | None = None,
    ) -> IndexProgress:
        ws = self.storage.get_workspace(workspace_id)
        if ws is None:
            raise ValueError(f"workspace {workspace_id} not found")
        self.storage.set_workspace_status(workspace_id, WorkspaceStatus.INDEXING)

        progress = IndexProgress()
        try:
            for n, df in enumerate(
                self.registry.file_discovery.iter_files(
                    Path(ws.root_path),
                    include_extensions=(
                        ws.include_extensions
                        if ws.include_extensions
                        else list(EXTENSION_KIND.keys())
                    ),
                    exclude_globs=ws.exclude_globs,
                    max_size_bytes=ws.max_file_size_mb * 1024 * 1024,
                )
            ):
                if max_files is not None and progress.total_seen >= max_files:
                    break
                progress.total_seen += 1
                try:
                    indexed = self._index_one(ws, df.path, df.mtime, df.size_bytes, force=force)
                except Exception as exc:  # never let one bad file break the run
                    progress.total_errors += 1
                    progress.errors.append((str(df.path), repr(exc)))
                    log.exception("indexing %s failed", df.path)
                    continue
                if indexed:
                    progress.total_indexed += 1
                else:
                    progress.total_skipped += 1
        finally:
            progress.finished_at = datetime.now(timezone.utc)
            doc_count = self.storage.count_documents(workspace_id)
            status = WorkspaceStatus.INDEXED
            if progress.total_errors:
                status = WorkspaceStatus.INDEXED  # keep usable; errors surface via API
            self.storage.set_workspace_status(
                workspace_id,
                status,
                last_indexed_at=progress.finished_at,
                document_count=doc_count,
            )
        return progress

    def reindex_path(self, workspace_id: str, path: Path) -> bool:
        ws = self.storage.get_workspace(workspace_id)
        if ws is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        return self._index_one(ws, path, mtime, st.st_size, force=True)

    # -- internal -------------------------------------------------------------

    def _index_one(
        self,
        ws: Workspace,
        path: Path,
        mtime: datetime,
        size_bytes: int,
        *,
        force: bool,
    ) -> bool:
        kind = EXTENSION_KIND.get(path.suffix.lower())
        if kind is None:
            return False

        doc_id = Document.make_id(ws.id, str(path))
        existing = self.storage.get_document(doc_id)
        if (
            existing
            and not force
            and existing.indexed_at is not None
            and existing.mtime >= mtime
            and existing.size_bytes == size_bytes
        ):
            return False

        result = self._dispatch_extract(kind, path, doc_id, ws.id, ws)
        if result.error and not result.chunks:
            doc = Document(
                id=doc_id,
                workspace_id=ws.id,
                path=str(path),
                name=path.name,
                kind=kind,
                size_bytes=size_bytes,
                mtime=mtime,
                indexed_at=datetime.now(timezone.utc),
                has_extracted_text=False,
                extraction_error=result.error[:1000],
            )
            self.storage.upsert_document(doc)
            return False

        sha = _sha256(path)
        doc = Document(
            id=doc_id,
            workspace_id=ws.id,
            path=str(path),
            name=path.name,
            kind=kind,
            size_bytes=size_bytes,
            sha256=sha,
            mtime=mtime,
            indexed_at=datetime.now(timezone.utc),
            page_count=result.page_count,
            sheet_count=result.sheet_count,
            slide_count=result.slide_count,
            has_extracted_text=bool(result.chunks),
            extraction_method=result.extraction_method,
            extraction_error=result.error,
            language_hints=result.language_hints,
        )
        # Regex-baseline entities — cheap, no ML, gives the ranker free signal.
        entities = []
        for chunk in result.chunks:
            entities.extend(
                extract_entities(chunk.text, document_id=doc_id, chunk_id=chunk.id)
            )

        with self.storage.tx():
            self.storage.upsert_document(doc)
            self.storage.replace_chunks(doc_id, result.chunks)
            if result.fields:
                self.storage.replace_extracted_fields(doc_id, result.fields)
            if entities:
                self.storage.replace_entities(doc_id, entities)
        return True

    def _dispatch_extract(
        self,
        kind: DocumentKind,
        path: Path,
        document_id: str,
        workspace_id: str,
        ws: Workspace,
    ):
        # Plain-text fast path — avoid loading pdfplumber/openpyxl for txt/md/csv.
        text_adapter = self.registry.text_for(kind)
        if text_adapter is not None:
            return text_adapter.extract(
                path, kind, document_id=document_id, workspace_id=workspace_id
            )

        if kind == DocumentKind.PDF:
            if self.registry.pdf is None:
                from ..adapters.base import ExtractionResult

                return ExtractionResult(error="no PDF adapter available")
            result = self.registry.pdf.extract(
                path, document_id=document_id, workspace_id=workspace_id
            )
            # If text PDF gave nothing but OCR is enabled, fall back to OCR.
            if (not result.chunks) and ws.enable_ocr and self.registry.ocr is not None:
                ocr_result = self.registry.ocr.extract_scanned_pdf(
                    path, document_id=document_id, workspace_id=workspace_id
                )
                if ocr_result.chunks:
                    return ocr_result
            return result

        office_adapter = self.registry.office_for(kind)
        if office_adapter is not None:
            return office_adapter.extract(
                path, kind, document_id=document_id, workspace_id=workspace_id
            )

        if kind == DocumentKind.IMAGE and ws.enable_ocr and self.registry.ocr is not None:
            return self.registry.ocr.extract_image(
                path, document_id=document_id, workspace_id=workspace_id
            )

        from ..adapters.base import ExtractionResult

        return ExtractionResult(error=f"no adapter for kind={kind}")


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None
