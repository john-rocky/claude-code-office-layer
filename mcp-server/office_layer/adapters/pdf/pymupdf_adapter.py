"""PyMuPDF adapter — AGPL/commercial. Opt-in only.

Not selected by default because PyMuPDF is dual-licensed under AGPL and a
commercial license. Some users will prefer it (fastest text + layout); they
opt in by installing the ``pdf-mupdf`` extra and setting
``OFFICE_LAYER_PDF=pymupdf``.
"""

from __future__ import annotations

from pathlib import Path

from ...models import ChunkKind, DocumentChunk, ExtractionMethod
from ..base import ExtractionResult


class PyMuPDFAdapter:
    name = "pymupdf"
    method = ExtractionMethod.PDF_TEXT

    def __init__(self) -> None:
        try:
            import pymupdf  # noqa: F401
            self._available = True
        except ImportError:
            try:
                import fitz  # noqa: F401  # legacy name pre-1.24
                self._available = True
            except ImportError:
                self._available = False

    def is_available(self) -> bool:
        return self._available

    def extract(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        try:
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
        except ImportError as exc:
            return ExtractionResult(error=f"pymupdf missing: {exc}")

        chunks: list[DocumentChunk] = []
        ordinal = 0
        page_count = 0

        try:
            doc = fitz.open(str(path))
            page_count = len(doc)
            for i, page in enumerate(doc, start=1):
                text = (page.get_text("text") or "").strip()
                if not text:
                    continue
                chunks.append(
                    DocumentChunk(
                        id=DocumentChunk.make_id(document_id, ordinal),
                        document_id=document_id,
                        workspace_id=workspace_id,
                        kind=ChunkKind.PDF_PAGE,
                        ordinal=ordinal,
                        page_number=i,
                        text=text,
                        char_count=len(text),
                        confidence=1.0,
                        extraction_method=ExtractionMethod.PDF_TEXT,
                    )
                )
                ordinal += 1
            doc.close()
        except Exception as exc:
            return ExtractionResult(
                chunks=chunks,
                page_count=page_count or None,
                extraction_method=ExtractionMethod.PDF_TEXT,
                error=f"pymupdf failed: {exc}",
            )

        return ExtractionResult(
            chunks=chunks,
            page_count=page_count or None,
            extraction_method=ExtractionMethod.PDF_TEXT,
        )
