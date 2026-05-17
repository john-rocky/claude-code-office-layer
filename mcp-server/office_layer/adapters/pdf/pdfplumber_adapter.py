"""pdfplumber adapter — MIT, table-aware, pure-python.

Default choice when ``pypdfium2`` is not installed. Slower for pure text but
extracts tables which the engine surfaces as ExtractedTable objects.
"""

from __future__ import annotations

from pathlib import Path

from ...models import ChunkKind, DocumentChunk, ExtractedTable, ExtractionMethod
from ..base import ExtractionResult


class PdfplumberAdapter:
    name = "pdfplumber"
    method = ExtractionMethod.PDF_TEXT

    def __init__(self) -> None:
        try:
            import pdfplumber  # noqa: F401
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
            import pdfplumber
        except ImportError as exc:
            return ExtractionResult(error=f"pdfplumber missing: {exc}")

        chunks: list[DocumentChunk] = []
        tables: list[ExtractedTable] = []
        page_count = 0
        ordinal = 0

        try:
            with pdfplumber.open(str(path)) as pdf:
                page_count = len(pdf.pages)
                for i, page in enumerate(pdf.pages, start=1):
                    text = (page.extract_text() or "").strip()
                    if text:
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
                    for tbl in page.extract_tables() or []:
                        if not tbl:
                            continue
                        headers = [str(c or "") for c in tbl[0]]
                        rows = [[str(c or "") for c in r] for r in tbl[1:]]
                        tables.append(
                            ExtractedTable(
                                document_id=document_id,
                                page_number=i,
                                headers=headers,
                                rows=rows,
                                confidence=0.8,
                            )
                        )
        except Exception as exc:  # pdfplumber raises various wrapped errors
            return ExtractionResult(
                chunks=chunks,
                tables=tables,
                page_count=page_count or None,
                extraction_method=ExtractionMethod.PDF_TEXT,
                error=f"pdfplumber failed: {exc}",
            )

        return ExtractionResult(
            chunks=chunks,
            tables=tables,
            page_count=page_count or None,
            extraction_method=ExtractionMethod.PDF_TEXT,
        )
