"""pypdfium2 adapter — BSD/Apache, fast text-only.

Preferred for raw text speed. Does not extract tables (use pdfplumber for
that). Engine combines both when available: pypdfium2 for page text + the
heavier table extractor only on pages that look table-like.
"""

from __future__ import annotations

from pathlib import Path

from ...models import ChunkKind, DocumentChunk, ExtractionMethod
from ..base import ExtractionResult


class Pypdfium2Adapter:
    name = "pypdfium2"
    method = ExtractionMethod.PDF_TEXT

    def __init__(self) -> None:
        try:
            import pypdfium2  # noqa: F401
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
            import pypdfium2 as pdfium
        except ImportError as exc:
            return ExtractionResult(error=f"pypdfium2 missing: {exc}")

        chunks: list[DocumentChunk] = []
        ordinal = 0
        page_count = 0

        try:
            doc = pdfium.PdfDocument(str(path))
            page_count = len(doc)
            for i in range(page_count):
                page = doc[i]
                tp = page.get_textpage()
                try:
                    text = (tp.get_text_range() or "").strip()
                finally:
                    tp.close()
                page.close()
                if not text:
                    continue
                chunks.append(
                    DocumentChunk(
                        id=DocumentChunk.make_id(document_id, ordinal),
                        document_id=document_id,
                        workspace_id=workspace_id,
                        kind=ChunkKind.PDF_PAGE,
                        ordinal=ordinal,
                        page_number=i + 1,
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
                error=f"pypdfium2 failed: {exc}",
            )

        return ExtractionResult(
            chunks=chunks,
            page_count=page_count or None,
            extraction_method=ExtractionMethod.PDF_TEXT,
        )
