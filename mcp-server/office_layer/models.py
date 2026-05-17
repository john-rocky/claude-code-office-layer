"""Domain models for Claude Code Office Layer.

Models map to spec §17.2 internal domain objects. All exchange across MCP is
typed via these pydantic models so Claude Code receives stable JSON shapes.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# -- Enums --------------------------------------------------------------------


class WorkspacePolicy(str, Enum):
    READ_ONLY = "read-only"
    DRAFT_WRITE = "draft-write"
    FULL_WRITE = "full-write"


class WorkspaceStatus(str, Enum):
    UNINDEXED = "unindexed"
    INDEXING = "indexing"
    INDEXED = "indexed"
    UPDATING = "updating"
    ERROR = "error"
    PERMISSION_DENIED = "permission-denied"
    RESCAN_NEEDED = "rescan-needed"


class DocumentKind(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    CSV = "csv"
    TSV = "tsv"
    TXT = "txt"
    MARKDOWN = "markdown"
    HTML = "html"
    RTF = "rtf"
    JSON = "json"
    XML = "xml"
    IMAGE = "image"
    EMAIL = "email"
    ZIP = "zip"
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    NATIVE_TEXT = "native-text"
    PDF_TEXT = "pdf-text"
    PDF_OCR = "pdf-ocr"
    DOCX_PARSE = "docx-parse"
    XLSX_PARSE = "xlsx-parse"
    PPTX_PARSE = "pptx-parse"
    CSV_PARSE = "csv-parse"
    EMAIL_PARSE = "email-parse"
    IMAGE_OCR = "image-ocr"
    HEURISTIC = "heuristic"


class OperationRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ChunkKind(str, Enum):
    PARAGRAPH = "paragraph"
    PDF_PAGE = "pdf-page"
    XLSX_SHEET = "xlsx-sheet"
    XLSX_TABLE = "xlsx-table"
    PPTX_SLIDE = "pptx-slide"
    DOCX_SECTION = "docx-section"
    EMAIL_BODY = "email-body"
    OCR_BLOCK = "ocr-block"


# -- Core domain --------------------------------------------------------------


class Workspace(BaseModel):
    id: str = Field(default_factory=lambda: f"ws_{uuid.uuid4().hex[:12]}")
    name: str
    root_path: str
    policy: WorkspacePolicy = WorkspacePolicy.READ_ONLY
    status: WorkspaceStatus = WorkspaceStatus.UNINDEXED
    exclude_globs: list[str] = Field(default_factory=list)
    include_extensions: list[str] | None = None
    max_file_size_mb: int = 100
    enable_ocr: bool = False
    enable_vector_search: bool = False
    pii_warning: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_indexed_at: datetime | None = None
    document_count: int = 0
    error: str | None = None

    @field_validator("root_path")
    @classmethod
    def expand_root(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class Document(BaseModel):
    id: str
    workspace_id: str
    path: str
    name: str
    kind: DocumentKind
    size_bytes: int
    sha256: str | None = None
    mtime: datetime
    indexed_at: datetime | None = None
    page_count: int | None = None
    sheet_count: int | None = None
    slide_count: int | None = None
    has_extracted_text: bool = False
    extraction_method: ExtractionMethod | None = None
    extraction_error: str | None = None
    language_hints: list[str] = Field(default_factory=list)
    pii_flagged: bool = False

    @classmethod
    def make_id(cls, workspace_id: str, path: str) -> str:
        digest = hashlib.sha1(f"{workspace_id}:{path}".encode()).hexdigest()[:16]
        return f"doc_{digest}"


class DocumentChunk(BaseModel):
    id: str
    document_id: str
    workspace_id: str
    kind: ChunkKind
    ordinal: int
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    slide_number: int | None = None
    section_heading: str | None = None
    text: str
    char_count: int = 0
    confidence: float = 1.0
    extraction_method: ExtractionMethod | None = None

    @classmethod
    def make_id(cls, document_id: str, ordinal: int) -> str:
        return f"chk_{document_id[4:]}_{ordinal:05d}"


class ExtractedTable(BaseModel):
    """Tabular data extracted from PDF/XLSX/DOCX."""

    document_id: str
    chunk_id: str | None = None
    sheet_name: str | None = None
    page_number: int | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cell_range: str | None = None
    confidence: float = 1.0


class ExtractedField(BaseModel):
    """A structured value extracted from a document (e.g. invoice amount)."""

    document_id: str
    chunk_id: str | None = None
    key: str
    value: str
    value_type: Literal["text", "amount", "date", "email", "phone", "id", "url"] = "text"
    confidence: float = 1.0
    page_number: int | None = None
    cell_range: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    extractor: str = "heuristic"


class Entity(BaseModel):
    """Named entity surfaced from text — company, person, project, etc."""

    document_id: str
    chunk_id: str | None = None
    text: str
    kind: Literal["org", "person", "money", "date", "email", "phone", "url", "project", "other"]
    confidence: float = 0.8


# -- Search -------------------------------------------------------------------


class SearchMode(str, Enum):
    FILENAME = "filename"
    FULLTEXT = "fulltext"
    SEMANTIC = "semantic"
    ENTITY = "entity"
    DATE_RANGE = "date-range"
    AMOUNT = "amount"
    KIND = "kind"
    HYBRID = "hybrid"


class SearchQuery(BaseModel):
    text: str = ""
    workspace_ids: list[str] | None = None
    mode: SearchMode = SearchMode.HYBRID
    kinds: list[DocumentKind] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    limit: int = 20
    include_chunks: bool = True


class SearchResult(BaseModel):
    document: Document
    chunk: DocumentChunk | None = None
    score: float
    reason: str = ""
    matched_terms: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: SearchQuery
    results: list[SearchResult]
    total: int
    elapsed_ms: float = 0.0


# -- Evidence Packet ----------------------------------------------------------


class EvidenceSource(BaseModel):
    source_id: str
    file_path: str
    file_name: str
    file_type: DocumentKind
    workspace_name: str
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    slide_number: int | None = None
    extracted_text: str
    extracted_tables: list[ExtractedTable] = Field(default_factory=list)
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    relevance_score: float = 0.0
    confidence_score: float = 1.0
    extraction_method: ExtractionMethod | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reason_for_inclusion: str = ""
    suggested_next_action: str | None = None


class EvidencePacket(BaseModel):
    """The Evidence Packet — minimal grounded context handed to Claude Code.

    Per spec §9.5, this is NOT a search result dump. It is what Claude Code
    needs to safely produce an artifact (CSV, email draft, report) with
    traceable provenance.
    """

    packet_id: str = Field(default_factory=lambda: f"ep_{uuid.uuid4().hex[:12]}")
    intent: str
    query: SearchQuery | None = None
    sources: list[EvidenceSource] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    workspace_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    low_confidence_items: list[str] = Field(default_factory=list)
    truncated: bool = False

    def summary_markdown(self) -> str:
        """Human-readable summary for Claude or logs."""
        lines = [
            f"# Evidence Packet {self.packet_id}",
            f"**Intent**: {self.intent}",
            f"**Created**: {self.created_at.isoformat()}",
            f"**Sources**: {len(self.sources)}",
            "",
        ]
        for i, src in enumerate(self.sources, 1):
            loc = []
            if src.page_number:
                loc.append(f"p.{src.page_number}")
            if src.sheet_name:
                loc.append(f"sheet={src.sheet_name}")
            if src.cell_range:
                loc.append(f"cells={src.cell_range}")
            if src.slide_number:
                loc.append(f"slide={src.slide_number}")
            loc_str = " ".join(loc) or "—"
            kind_str = src.file_type.value if hasattr(src.file_type, "value") else str(src.file_type)
            lines.append(
                f"{i}. **{src.file_name}** ({kind_str}) {loc_str} "
                f"[rel={src.relevance_score:.2f} conf={src.confidence_score:.2f}]"
            )
            if src.reason_for_inclusion:
                lines.append(f"   - why: {src.reason_for_inclusion}")
            preview = src.extracted_text.strip().splitlines()[0:3]
            if preview:
                lines.append("   - preview: " + " / ".join(preview))
        if self.low_confidence_items:
            lines.append("")
            lines.append("## Low-confidence items to review")
            for item in self.low_confidence_items:
                lines.append(f"- {item}")
        return "\n".join(lines)


# -- Workflow & Safety --------------------------------------------------------


class WorkflowRun(BaseModel):
    id: str = Field(default_factory=lambda: f"wf_{uuid.uuid4().hex[:12]}")
    name: str
    intent: str
    workspace_ids: list[str]
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    status: Literal["pending", "running", "completed", "failed", "needs_review"] = "pending"
    evidence_packet_ids: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    error: str | None = None
    notes: list[str] = Field(default_factory=list)


class OperationRisk(BaseModel):
    level: OperationRiskLevel
    operation: str
    targets: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    suggested_safer_alternative: str | None = None


class AuditLogEntry(BaseModel):
    id: str = Field(default_factory=lambda: f"log_{uuid.uuid4().hex[:12]}")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str = "claude-code"
    operation: str
    user_request: str | None = None
    tool: str | None = None
    referenced_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    risk: OperationRiskLevel = OperationRiskLevel.LOW
    user_approved: bool | None = None
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
