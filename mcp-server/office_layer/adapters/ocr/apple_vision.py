"""Apple Vision OCR adapter (macOS-only).

Uses ``Vision.VNRecognizeTextRequest`` via pyobjc. Zero model download,
fast, and reasonable quality for English; Japanese requires macOS 13+ with
ja-JP language pack enabled.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ...models import ChunkKind, DocumentChunk, ExtractionMethod
from ..base import ExtractionResult


class AppleVisionAdapter:
    name = "apple-vision"
    method = ExtractionMethod.IMAGE_OCR

    def __init__(self) -> None:
        self._available = False
        if sys.platform != "darwin":
            return
        try:
            import Vision  # type: ignore  # noqa: F401
            import Quartz  # type: ignore  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def extract_image(
        self,
        path: Path,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        if not self._available:
            return ExtractionResult(error="Apple Vision not available")
        try:
            import Quartz  # type: ignore
            import Vision  # type: ignore
            from Foundation import NSURL  # type: ignore
        except ImportError as exc:
            return ExtractionResult(error=f"pyobjc missing: {exc}")

        url = NSURL.fileURLWithPath_(str(path.resolve()))
        try:
            src = Quartz.CGImageSourceCreateWithURL(url, None)
            if src is None:
                return ExtractionResult(error="Failed to read image")
            cg_image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
            if cg_image is None:
                return ExtractionResult(error="Failed to decode image")
        except Exception as exc:
            return ExtractionResult(error=f"Quartz failed: {exc}")

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        try:
            request.setRecognitionLanguages_(["ja-JP", "en-US"])
        except Exception:
            pass
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
        try:
            ok, err = handler.performRequests_error_([request], None)
        except Exception as exc:
            return ExtractionResult(error=f"Vision perform failed: {exc}")
        if not ok:
            return ExtractionResult(error="Vision perform returned false")
        observations = request.results() or []
        lines: list[str] = []
        confidences: list[float] = []
        for o in observations:
            candidate = o.topCandidates_(1)
            if candidate and len(candidate) > 0:
                lines.append(str(candidate[0].string()))
                confidences.append(float(candidate[0].confidence()))
        text = "\n".join(lines).strip()
        conf = sum(confidences) / len(confidences) if confidences else 0.5
        chunk = DocumentChunk(
            id=DocumentChunk.make_id(document_id, 0),
            document_id=document_id,
            workspace_id=workspace_id,
            kind=ChunkKind.OCR_BLOCK,
            ordinal=0,
            text=text,
            char_count=len(text),
            confidence=conf,
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
        # Apple Vision doesn't natively render PDFs to images. Fall through to
        # a Tesseract-style pipeline only if pdf2image is available. Otherwise
        # signal that the caller should use a different adapter.
        try:
            from pdf2image import convert_from_path  # type: ignore
        except ImportError:
            return ExtractionResult(
                error="Apple Vision PDF requires pdf2image (poppler) — install or use OCRmyPDF"
            )
        chunks: list[DocumentChunk] = []
        try:
            images = convert_from_path(str(path), dpi=200)
        except Exception as exc:
            return ExtractionResult(error=f"pdf2image failed: {exc}")
        for i, img in enumerate(images, start=1):
            tmp = path.parent / f".__vision_tmp_{i}.png"
            img.save(tmp)
            try:
                page_result = self.extract_image(
                    tmp, document_id=document_id, workspace_id=workspace_id
                )
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            for c in page_result.chunks:
                c.page_number = i
                c.ordinal = i - 1
                c.extraction_method = ExtractionMethod.PDF_OCR
                chunks.append(c)
        return ExtractionResult(
            chunks=chunks,
            page_count=len(images),
            extraction_method=ExtractionMethod.PDF_OCR,
        )
