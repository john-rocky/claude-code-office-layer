"""HTML via BeautifulSoup."""

from __future__ import annotations

from pathlib import Path

from ...models import ChunkKind, DocumentChunk, DocumentKind, ExtractionMethod
from ..base import ExtractionResult


class HtmlTextAdapter:
    name = "beautifulsoup"
    supported_kinds = (DocumentKind.HTML,)

    def __init__(self) -> None:
        try:
            import bs4  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def extract(
        self,
        path: Path,
        kind: DocumentKind,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            return ExtractionResult(error=f"beautifulsoup missing: {exc}")
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ExtractionResult(error=f"read failed: {exc}")
        soup = BeautifulSoup(raw, "html.parser")
        # strip script/style noise
        for s in soup(["script", "style", "noscript"]):
            s.decompose()
        text = soup.get_text("\n", strip=True)
        chunks = [
            DocumentChunk(
                id=DocumentChunk.make_id(document_id, 0),
                document_id=document_id,
                workspace_id=workspace_id,
                kind=ChunkKind.PARAGRAPH,
                ordinal=0,
                text=text,
                char_count=len(text),
                confidence=1.0,
                extraction_method=ExtractionMethod.NATIVE_TEXT,
            )
        ]
        return ExtractionResult(chunks=chunks, extraction_method=ExtractionMethod.NATIVE_TEXT)
