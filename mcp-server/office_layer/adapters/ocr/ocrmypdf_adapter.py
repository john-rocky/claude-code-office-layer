"""OCRmyPDF adapter — full PDF-OCR pipeline (subprocess).

Adds a text layer to a scanned PDF and then re-extracts via the normal PDF
adapter. Heavy (Ghostscript + Tesseract) but produces searchable PDFs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from ...models import ExtractionMethod
from ..base import ExtractionResult


class OcrmypdfAdapter:
    name = "ocrmypdf"
    method = ExtractionMethod.PDF_OCR

    def __init__(self) -> None:
        self._bin = shutil.which("ocrmypdf")

    def is_available(self) -> bool:
        return self._bin is not None

    def extract_image(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        return ExtractionResult(error="ocrmypdf is PDF-only; use tesseract/apple-vision for images")

    def extract_scanned_pdf(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        if not self._bin:
            return ExtractionResult(error="ocrmypdf binary not on PATH")
        # Skip if the PDF already has text — ocrmypdf can do this check with
        # --skip-text, but we want to be transparent about behaviour.
        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            cmd = [
                self._bin,
                "--skip-text",
                "--language",
                "jpn+eng",
                "--rotate-pages",
                str(path),
                str(tmp_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                return ExtractionResult(
                    error=f"ocrmypdf failed (rc={proc.returncode}): {proc.stderr[:500]}"
                )
            # Re-extract the OCR'd PDF using the regular PDF adapter so we get
            # native chunks (engine handles this).
            from ..pdf.pdfplumber_adapter import PdfplumberAdapter
            from ..pdf.pypdfium2_adapter import Pypdfium2Adapter

            for cls in (Pypdfium2Adapter, PdfplumberAdapter):
                a = cls()
                if a.is_available():
                    result = a.extract(
                        tmp_path, document_id=document_id, workspace_id=workspace_id
                    )
                    result.extraction_method = ExtractionMethod.PDF_OCR
                    for c in result.chunks:
                        c.extraction_method = ExtractionMethod.PDF_OCR
                        c.confidence = min(c.confidence, 0.7)
                    return result
            return ExtractionResult(error="ocrmypdf ran but no PDF text adapter available")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
