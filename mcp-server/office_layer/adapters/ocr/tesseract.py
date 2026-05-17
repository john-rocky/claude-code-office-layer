"""Tesseract OCR adapter via pytesseract.

Requires the ``tesseract`` binary on PATH plus the ``pytesseract`` Python
wrapper. Installs on macOS via ``brew install tesseract``, on Ubuntu via
``apt install tesseract-ocr``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ...models import ChunkKind, DocumentChunk, ExtractionMethod
from ..base import ExtractionResult


class TesseractAdapter:
    name = "tesseract"
    method = ExtractionMethod.IMAGE_OCR

    def __init__(self) -> None:
        self._py_ok = False
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
            self._py_ok = True
        except ImportError:
            self._py_ok = False
        self._bin_ok = shutil.which("tesseract") is not None

    def is_available(self) -> bool:
        return self._py_ok and self._bin_ok

    def extract_image(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        if not self.is_available():
            return ExtractionResult(error="tesseract not installed")
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            return ExtractionResult(error=f"tesseract deps missing: {exc}")
        try:
            with Image.open(path) as img:
                text = pytesseract.image_to_string(img, lang="jpn+eng")
        except Exception as exc:
            return ExtractionResult(error=f"tesseract failed: {exc}")
        chunk = DocumentChunk(
            id=DocumentChunk.make_id(document_id, 0),
            document_id=document_id,
            workspace_id=workspace_id,
            kind=ChunkKind.OCR_BLOCK,
            ordinal=0,
            text=text,
            char_count=len(text),
            confidence=0.7,
            extraction_method=ExtractionMethod.IMAGE_OCR,
        )
        return ExtractionResult(chunks=[chunk], extraction_method=ExtractionMethod.IMAGE_OCR)

    def extract_scanned_pdf(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        try:
            from pdf2image import convert_from_path
            import pytesseract
        except ImportError as exc:
            return ExtractionResult(error=f"pdf2image/pytesseract missing: {exc}")
        try:
            pages = convert_from_path(str(path), dpi=200)
        except Exception as exc:
            return ExtractionResult(error=f"pdf2image failed: {exc}")
        chunks: list[DocumentChunk] = []
        for i, img in enumerate(pages, start=1):
            text = pytesseract.image_to_string(img, lang="jpn+eng").strip()
            if not text:
                continue
            chunks.append(
                DocumentChunk(
                    id=DocumentChunk.make_id(document_id, i - 1),
                    document_id=document_id,
                    workspace_id=workspace_id,
                    kind=ChunkKind.OCR_BLOCK,
                    ordinal=i - 1,
                    page_number=i,
                    text=text,
                    char_count=len(text),
                    confidence=0.65,
                    extraction_method=ExtractionMethod.PDF_OCR,
                )
            )
        return ExtractionResult(
            chunks=chunks,
            page_count=len(pages),
            extraction_method=ExtractionMethod.PDF_OCR,
        )
