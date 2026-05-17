"""Lightweight stdlib text extractors — TXT / Markdown / CSV / JSON.

Reading these formats with stdlib avoids pulling pdfplumber/openpyxl for files
that obviously don't need them. Each adapter handles its own chunking strategy
so downstream search keeps stable units (paragraphs, csv rows, json paths).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ...models import ChunkKind, DocumentChunk, DocumentKind, ExtractedTable, ExtractionMethod
from ..base import ExtractionResult


PARAGRAPH_TARGET_CHARS = 1500
CSV_ROWS_PER_CHUNK = 200


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"read failed: {exc}") from exc


class StdlibTextAdapter:
    """TXT and Markdown via stdlib."""

    name = "stdlib-text"
    supported_kinds = (DocumentKind.TXT, DocumentKind.MARKDOWN)

    def is_available(self) -> bool:
        return True

    def extract(
        self,
        path: Path,
        kind: DocumentKind,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        try:
            text = _read_text(path)
        except RuntimeError as exc:
            return ExtractionResult(error=str(exc))

        markdown = kind == DocumentKind.MARKDOWN
        chunks: list[DocumentChunk] = []
        ordinal = 0
        current_heading: str | None = None
        buf: list[str] = []
        buf_len = 0

        def flush() -> None:
            nonlocal ordinal, buf, buf_len
            if not buf:
                return
            text_block = "\n".join(buf).strip()
            if text_block:
                chunks.append(
                    DocumentChunk(
                        id=DocumentChunk.make_id(document_id, ordinal),
                        document_id=document_id,
                        workspace_id=workspace_id,
                        kind=ChunkKind.PARAGRAPH,
                        ordinal=ordinal,
                        section_heading=current_heading,
                        text=text_block,
                        char_count=len(text_block),
                        confidence=1.0,
                        extraction_method=ExtractionMethod.NATIVE_TEXT,
                    )
                )
                ordinal += 1
            buf = []
            buf_len = 0

        for line in text.splitlines():
            if markdown and line.startswith("#"):
                flush()
                current_heading = line.lstrip("# ").strip() or current_heading
                buf.append(line)
                buf_len += len(line)
                continue
            buf.append(line)
            buf_len += len(line)
            if buf_len >= PARAGRAPH_TARGET_CHARS and not line.strip():
                flush()
        flush()

        return ExtractionResult(
            chunks=chunks,
            extraction_method=ExtractionMethod.NATIVE_TEXT,
        )


class CsvTextAdapter:
    """CSV / TSV via stdlib csv."""

    name = "stdlib-csv"
    supported_kinds = (DocumentKind.CSV, DocumentKind.TSV)

    def is_available(self) -> bool:
        return True

    def extract(
        self,
        path: Path,
        kind: DocumentKind,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        delim = "\t" if kind == DocumentKind.TSV else ","
        chunks: list[DocumentChunk] = []
        tables: list[ExtractedTable] = []
        ordinal = 0
        rows: list[list[str]] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                reader = csv.reader(f, delimiter=delim)
                for r in reader:
                    rows.append([cell for cell in r])
        except OSError as exc:
            return ExtractionResult(error=f"read failed: {exc}")

        if not rows:
            return ExtractionResult(extraction_method=ExtractionMethod.CSV_PARSE)

        headers = rows[0]
        body = rows[1:]
        tables.append(
            ExtractedTable(
                document_id=document_id,
                headers=headers,
                rows=body,
                confidence=1.0,
            )
        )

        # Chunk by row blocks so FTS can locate matches to a row range.
        if not body:
            chunks.append(
                DocumentChunk(
                    id=DocumentChunk.make_id(document_id, ordinal),
                    document_id=document_id,
                    workspace_id=workspace_id,
                    kind=ChunkKind.XLSX_TABLE,
                    ordinal=ordinal,
                    text=delim.join(headers),
                    char_count=sum(len(h) for h in headers),
                    confidence=1.0,
                    extraction_method=ExtractionMethod.CSV_PARSE,
                )
            )
        else:
            for start in range(0, len(body), CSV_ROWS_PER_CHUNK):
                block = body[start : start + CSV_ROWS_PER_CHUNK]
                # cell_range uses 1-indexed row numbers from the file, header is row 1.
                first_row = start + 2
                last_row = start + 1 + len(block)
                text_lines = [delim.join(headers)] + [delim.join(r) for r in block]
                text = "\n".join(text_lines)
                chunks.append(
                    DocumentChunk(
                        id=DocumentChunk.make_id(document_id, ordinal),
                        document_id=document_id,
                        workspace_id=workspace_id,
                        kind=ChunkKind.XLSX_TABLE,
                        ordinal=ordinal,
                        cell_range=f"rows {first_row}-{last_row}",
                        text=text,
                        char_count=len(text),
                        confidence=1.0,
                        extraction_method=ExtractionMethod.CSV_PARSE,
                    )
                )
                ordinal += 1

        return ExtractionResult(
            chunks=chunks,
            tables=tables,
            extraction_method=ExtractionMethod.CSV_PARSE,
        )


class JsonTextAdapter:
    """JSON via stdlib — flat dump as text, simplest possible."""

    name = "stdlib-json"
    supported_kinds = (DocumentKind.JSON,)

    def is_available(self) -> bool:
        return True

    def extract(
        self,
        path: Path,
        kind: DocumentKind,
        *,
        document_id: str,
        workspace_id: str,
    ) -> ExtractionResult:
        try:
            raw = _read_text(path)
        except RuntimeError as exc:
            return ExtractionResult(error=str(exc))
        try:
            data = json.loads(raw)
            text = json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            text = raw
        chunks = [
            DocumentChunk(
                id=DocumentChunk.make_id(document_id, 0),
                document_id=document_id,
                workspace_id=workspace_id,
                kind=ChunkKind.PARAGRAPH,
                ordinal=0,
                text=text[:200_000],
                char_count=min(len(text), 200_000),
                confidence=1.0,
                extraction_method=ExtractionMethod.NATIVE_TEXT,
            )
        ]
        return ExtractionResult(chunks=chunks, extraction_method=ExtractionMethod.NATIVE_TEXT)
