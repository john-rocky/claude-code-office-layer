"""Adapter selection registry.

Picks the best available implementation of each adapter kind and exposes a
single ``get_registry()`` to the rest of the engine. ``Engine`` consults this
registry to wire up extraction / search / OCR / watching without caring about
which OSS backend ended up loaded.

Selection is conservative: ``pdfplumber`` is the default PDF backend because
it is MIT-licensed and pure-python; ``pypdfium2`` upgrades to a faster BSD
backend if installed; ``pymupdf`` is only chosen when explicitly forced via
``OFFICE_LAYER_PDF=pymupdf`` (AGPL — see spec §17.3).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Iterable

from ..models import DocumentKind, ExtractionMethod
from .base import (
    DegradedMode,
    FileDiscoveryAdapter,
    FileWatcherAdapter,
    OcrAdapter,
    OfficeExtractionAdapter,
    PdfExtractionAdapter,
    SemanticSearchAdapter,
    TextExtractionAdapter,
)

log = logging.getLogger(__name__)


@dataclass
class AdapterRegistry:
    file_discovery: FileDiscoveryAdapter
    pdf: PdfExtractionAdapter | None
    office: list[OfficeExtractionAdapter]
    text: list[TextExtractionAdapter]
    ocr: OcrAdapter | None
    semantic_search: SemanticSearchAdapter | None
    file_watcher: FileWatcherAdapter | None
    degraded: list[DegradedMode] = field(default_factory=list)

    def office_for(self, kind: DocumentKind) -> OfficeExtractionAdapter | None:
        for a in self.office:
            if kind in a.supported_kinds and a.is_available():
                return a
        return None

    def text_for(self, kind: DocumentKind) -> TextExtractionAdapter | None:
        for a in self.text:
            if kind in a.supported_kinds and a.is_available():
                return a
        return None


_registry: AdapterRegistry | None = None


def _select_file_discovery() -> FileDiscoveryAdapter:
    # Indexing wants a *complete* crawl. OS-native indexers (Spotlight /
    # Everything) miss recently-created files and respect user opt-outs, so
    # the walker is the default. Opt into mdfind/Everything via
    # ``OFFICE_LAYER_DISCOVERY=mdfind|everything|fd|ripgrep`` — useful for
    # quick filename search over already-indexed locations.
    from .file_discovery.mdfind import MdfindAdapter
    from .file_discovery.everything import EverythingAdapter
    from .file_discovery.fd_adapter import FdAdapter
    from .file_discovery.ripgrep_adapter import RipgrepFilesAdapter
    from .file_discovery.walk import WalkAdapter

    forced = os.environ.get("OFFICE_LAYER_DISCOVERY", "").lower().strip()
    if forced:
        mapping = {
            "mdfind": MdfindAdapter,
            "everything": EverythingAdapter,
            "fd": FdAdapter,
            "ripgrep": RipgrepFilesAdapter,
            "walk": WalkAdapter,
        }
        cls = mapping.get(forced)
        if cls is not None:
            a = cls()
            if a.is_available():
                return a
    return WalkAdapter()


def _select_pdf() -> tuple[PdfExtractionAdapter | None, DegradedMode | None]:
    forced = os.environ.get("OFFICE_LAYER_PDF", "").lower().strip()

    from .pdf.pdfplumber_adapter import PdfplumberAdapter
    from .pdf.pypdfium2_adapter import Pypdfium2Adapter
    from .pdf.pymupdf_adapter import PyMuPDFAdapter

    candidates: list[type[PdfExtractionAdapter]] = []
    if forced == "pymupdf":
        candidates = [PyMuPDFAdapter, Pypdfium2Adapter, PdfplumberAdapter]
    elif forced == "pypdfium2":
        candidates = [Pypdfium2Adapter, PdfplumberAdapter]
    elif forced == "pdfplumber":
        candidates = [PdfplumberAdapter]
    else:
        # Default order: pypdfium2 (BSD, fast) → pdfplumber (MIT, tables)
        # → pymupdf (AGPL, last resort, only if user opted in via extras).
        candidates = [Pypdfium2Adapter, PdfplumberAdapter, PyMuPDFAdapter]

    for cls in candidates:
        a = cls()
        if a.is_available():
            return a, None

    return None, DegradedMode(
        adapter_kind="pdf",
        reason="No PDF extractor available; PDF content will not be indexed.",
        install_hint="pip install 'claude-code-office-layer[pdf-fast]'  # or pdfplumber alone",
    )


def _select_office() -> tuple[list[OfficeExtractionAdapter], list[DegradedMode]]:
    from .office.docx_adapter import PythonDocxAdapter
    from .office.xlsx_adapter import OpenpyxlAdapter
    from .office.pptx_adapter import PythonPptxAdapter

    adapters: list[OfficeExtractionAdapter] = []
    degraded: list[DegradedMode] = []
    for cls in (PythonDocxAdapter, OpenpyxlAdapter, PythonPptxAdapter):
        a = cls()
        if a.is_available():
            adapters.append(a)
        else:
            degraded.append(
                DegradedMode(
                    adapter_kind=f"office:{cls.__name__}",
                    reason=f"{cls.__name__} optional dep missing",
                    install_hint="pip install claude-code-office-layer  # core install includes Office libs",
                )
            )
    return adapters, degraded


def _select_text() -> list[TextExtractionAdapter]:
    from .text.stdlib import StdlibTextAdapter, CsvTextAdapter, JsonTextAdapter
    from .text.html_adapter import HtmlTextAdapter

    return [StdlibTextAdapter(), CsvTextAdapter(), JsonTextAdapter(), HtmlTextAdapter()]


def _select_ocr() -> tuple[OcrAdapter | None, DegradedMode | None]:
    forced = os.environ.get("OFFICE_LAYER_OCR", "").lower().strip()

    from .ocr.tesseract import TesseractAdapter
    from .ocr.apple_vision import AppleVisionAdapter
    from .ocr.ocrmypdf_adapter import OcrmypdfAdapter

    if forced == "off":
        return None, None

    candidates: list[type[OcrAdapter]] = [
        AppleVisionAdapter,
        TesseractAdapter,
        OcrmypdfAdapter,
    ]
    if forced == "tesseract":
        candidates = [TesseractAdapter]
    elif forced == "apple":
        candidates = [AppleVisionAdapter]
    elif forced == "ocrmypdf":
        candidates = [OcrmypdfAdapter]

    for cls in candidates:
        a = cls()
        if a.is_available():
            return a, None

    return None, DegradedMode(
        adapter_kind="ocr",
        reason="No OCR adapter available — scanned PDFs / images will be skipped.",
        install_hint="brew install tesseract && pip install 'claude-code-office-layer[ocr]'",
    )


def _select_semantic() -> tuple[SemanticSearchAdapter | None, DegradedMode | None]:
    from .semantic_search.disabled import DisabledSemanticAdapter
    from .semantic_search.sqlite_vec_adapter import SqliteVecAdapter

    a = SqliteVecAdapter()
    if a.is_available():
        return a, None
    return (
        DisabledSemanticAdapter(),
        DegradedMode(
            adapter_kind="semantic_search",
            reason="Vector search disabled — install sqlite-vec or lancedb to enable.",
            install_hint="pip install 'claude-code-office-layer[vec-sqlite]'",
        ),
    )


def _select_watcher() -> FileWatcherAdapter | None:
    from .file_watcher.watchdog_adapter import WatchdogAdapter

    a = WatchdogAdapter()
    return a if a.is_available() else None


def build_registry() -> AdapterRegistry:
    pdf, pdf_deg = _select_pdf()
    office, office_deg = _select_office()
    ocr, ocr_deg = _select_ocr()
    sem, sem_deg = _select_semantic()
    degraded = [d for d in (pdf_deg, ocr_deg, sem_deg) if d] + office_deg
    return AdapterRegistry(
        file_discovery=_select_file_discovery(),
        pdf=pdf,
        office=office,
        text=_select_text(),
        ocr=ocr,
        semantic_search=sem,
        file_watcher=_select_watcher(),
        degraded=degraded,
    )


def get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
