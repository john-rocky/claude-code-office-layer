"""Adapter layer — wraps every external OSS/OS facility behind a stable interface.

Per spec §17.5, the project does not depend on a specific PDF parser, OCR
engine, search engine, or vector store. Each is hidden behind a Protocol so
implementations can be swapped (and so optional dependencies stay optional).

The selection helpers in this package check what is installed and return the
best available adapter, with a clear ``degraded mode`` story (§17.8) when
optional deps are missing.
"""

from .base import (
    DegradedMode,
    FileDiscoveryAdapter,
    FileWatcherAdapter,
    OcrAdapter,
    OfficeExtractionAdapter,
    PdfExtractionAdapter,
    SemanticSearchAdapter,
    TextExtractionAdapter,
    TextSearchAdapter,
)
from .registry import AdapterRegistry, get_registry

__all__ = [
    "AdapterRegistry",
    "DegradedMode",
    "FileDiscoveryAdapter",
    "FileWatcherAdapter",
    "OcrAdapter",
    "OfficeExtractionAdapter",
    "PdfExtractionAdapter",
    "SemanticSearchAdapter",
    "TextExtractionAdapter",
    "TextSearchAdapter",
    "get_registry",
]
