"""SQLite-backed storage with FTS5 full-text index.

Single file persistence keeps Phase 0 install trivial. Vector search (Phase 1+)
plugs into a separate ChromaDB collection — see ``office_layer.engine.vector``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    AuditLogEntry,
    ChunkKind,
    Document,
    DocumentChunk,
    DocumentKind,
    Entity,
    ExtractedField,
    ExtractionMethod,
    OperationRiskLevel,
    Workspace,
    WorkspacePolicy,
    WorkspaceStatus,
)

SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL,
    exclude_globs TEXT NOT NULL DEFAULT '[]',
    include_extensions TEXT,
    max_file_size_mb INTEGER NOT NULL DEFAULT 100,
    enable_ocr INTEGER NOT NULL DEFAULT 0,
    enable_vector_search INTEGER NOT NULL DEFAULT 0,
    pii_warning INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_indexed_at TEXT,
    document_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_workspaces_root ON workspaces(root_path);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    mtime TEXT NOT NULL,
    indexed_at TEXT,
    page_count INTEGER,
    sheet_count INTEGER,
    slide_count INTEGER,
    has_extracted_text INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT,
    extraction_error TEXT,
    language_hints TEXT NOT NULL DEFAULT '[]',
    pii_flagged INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_path ON documents(workspace_id, path);
CREATE INDEX IF NOT EXISTS ix_documents_mtime ON documents(mtime);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    page_number INTEGER,
    sheet_name TEXT,
    cell_range TEXT,
    slide_number INTEGER,
    section_heading TEXT,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    extraction_method TEXT
);

CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);

-- FTS5 virtual table over chunk text. Tokenizer "unicode61" handles JP/EN.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    section_heading,
    sheet_name,
    content='chunks',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section_heading, sheet_name)
    VALUES (new.rowid, new.text, COALESCE(new.section_heading, ''), COALESCE(new.sheet_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_heading, sheet_name)
    VALUES ('delete', old.rowid, old.text, COALESCE(old.section_heading, ''), COALESCE(old.sheet_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_heading, sheet_name)
    VALUES ('delete', old.rowid, old.text, COALESCE(old.section_heading, ''), COALESCE(old.sheet_name, ''));
    INSERT INTO chunks_fts(rowid, text, section_heading, sheet_name)
    VALUES (new.rowid, new.text, COALESCE(new.section_heading, ''), COALESCE(new.sheet_name, ''));
END;

CREATE TABLE IF NOT EXISTS extracted_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'text',
    confidence REAL NOT NULL DEFAULT 1.0,
    page_number INTEGER,
    cell_range TEXT,
    extractor TEXT NOT NULL DEFAULT 'heuristic'
);

CREATE INDEX IF NOT EXISTS ix_fields_document ON extracted_fields(document_id);
CREATE INDEX IF NOT EXISTS ix_fields_key ON extracted_fields(key);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8
);

CREATE INDEX IF NOT EXISTS ix_entities_document ON entities(document_id);
CREATE INDEX IF NOT EXISTS ix_entities_text ON entities(text);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    user_request TEXT,
    tool TEXT,
    referenced_files TEXT NOT NULL DEFAULT '[]',
    output_files TEXT NOT NULL DEFAULT '[]',
    risk TEXT NOT NULL DEFAULT 'low',
    user_approved INTEGER,
    error TEXT,
    extra TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_audit_timestamp ON audit_log(timestamp);
"""


def _isofmt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parsedt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        # executescript handles BEGIN/END trigger blocks correctly — naive
        # split-on-";" breaks triggers because they contain inner statements.
        self._conn.executescript(SCHEMA_SQL)
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        # Future migrations would dispatch on row["version"].

    # -- Workspaces -----------------------------------------------------------

    def upsert_workspace(self, ws: Workspace) -> Workspace:
        self._conn.execute(
            """
            INSERT INTO workspaces(id, name, root_path, policy, status, exclude_globs,
                include_extensions, max_file_size_mb, enable_ocr, enable_vector_search,
                pii_warning, created_at, last_indexed_at, document_count, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                root_path=excluded.root_path,
                policy=excluded.policy,
                status=excluded.status,
                exclude_globs=excluded.exclude_globs,
                include_extensions=excluded.include_extensions,
                max_file_size_mb=excluded.max_file_size_mb,
                enable_ocr=excluded.enable_ocr,
                enable_vector_search=excluded.enable_vector_search,
                pii_warning=excluded.pii_warning,
                last_indexed_at=excluded.last_indexed_at,
                document_count=excluded.document_count,
                error=excluded.error
            """,
            (
                ws.id,
                ws.name,
                ws.root_path,
                ws.policy.value if hasattr(ws.policy, "value") else ws.policy,
                ws.status.value if hasattr(ws.status, "value") else ws.status,
                json.dumps(ws.exclude_globs),
                json.dumps(ws.include_extensions) if ws.include_extensions else None,
                ws.max_file_size_mb,
                int(ws.enable_ocr),
                int(ws.enable_vector_search),
                int(ws.pii_warning),
                _isofmt(ws.created_at),
                _isofmt(ws.last_indexed_at),
                ws.document_count,
                ws.error,
            ),
        )
        return ws

    def list_workspaces(self) -> list[Workspace]:
        rows = self._conn.execute("SELECT * FROM workspaces ORDER BY created_at").fetchall()
        return [self._row_to_workspace(r) for r in rows]

    def get_workspace(self, ws_id: str) -> Workspace | None:
        row = self._conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        return self._row_to_workspace(row) if row else None

    def get_workspace_by_path(self, root_path: str) -> Workspace | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?", (root_path,)
        ).fetchone()
        return self._row_to_workspace(row) if row else None

    def delete_workspace(self, ws_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        return cur.rowcount > 0

    def set_workspace_status(
        self,
        ws_id: str,
        status: WorkspaceStatus,
        *,
        last_indexed_at: datetime | None = None,
        document_count: int | None = None,
        error: str | None = None,
    ) -> None:
        fields = ["status = ?"]
        params: list[Any] = [status.value]
        if last_indexed_at is not None:
            fields.append("last_indexed_at = ?")
            params.append(_isofmt(last_indexed_at))
        if document_count is not None:
            fields.append("document_count = ?")
            params.append(document_count)
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        params.append(ws_id)
        self._conn.execute(f"UPDATE workspaces SET {', '.join(fields)} WHERE id = ?", params)

    @staticmethod
    def _row_to_workspace(row: sqlite3.Row) -> Workspace:
        return Workspace(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            policy=WorkspacePolicy(row["policy"]),
            status=WorkspaceStatus(row["status"]),
            exclude_globs=json.loads(row["exclude_globs"]),
            include_extensions=(
                json.loads(row["include_extensions"]) if row["include_extensions"] else None
            ),
            max_file_size_mb=row["max_file_size_mb"],
            enable_ocr=bool(row["enable_ocr"]),
            enable_vector_search=bool(row["enable_vector_search"]),
            pii_warning=bool(row["pii_warning"]),
            created_at=_parsedt(row["created_at"]) or datetime.now(timezone.utc),
            last_indexed_at=_parsedt(row["last_indexed_at"]),
            document_count=row["document_count"],
            error=row["error"],
        )

    # -- Documents ------------------------------------------------------------

    def upsert_document(self, doc: Document) -> None:
        self._conn.execute(
            """
            INSERT INTO documents(id, workspace_id, path, name, kind, size_bytes, sha256,
                mtime, indexed_at, page_count, sheet_count, slide_count, has_extracted_text,
                extraction_method, extraction_error, language_hints, pii_flagged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                size_bytes=excluded.size_bytes,
                sha256=excluded.sha256,
                mtime=excluded.mtime,
                indexed_at=excluded.indexed_at,
                page_count=excluded.page_count,
                sheet_count=excluded.sheet_count,
                slide_count=excluded.slide_count,
                has_extracted_text=excluded.has_extracted_text,
                extraction_method=excluded.extraction_method,
                extraction_error=excluded.extraction_error,
                language_hints=excluded.language_hints,
                pii_flagged=excluded.pii_flagged
            """,
            (
                doc.id,
                doc.workspace_id,
                doc.path,
                doc.name,
                doc.kind.value if hasattr(doc.kind, "value") else doc.kind,
                doc.size_bytes,
                doc.sha256,
                _isofmt(doc.mtime),
                _isofmt(doc.indexed_at),
                doc.page_count,
                doc.sheet_count,
                doc.slide_count,
                int(doc.has_extracted_text),
                (doc.extraction_method.value if doc.extraction_method else None),
                doc.extraction_error,
                json.dumps(doc.language_hints),
                int(doc.pii_flagged),
            ),
        )

    def get_document(self, doc_id: str) -> Document | None:
        row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._row_to_document(row) if row else None

    def get_document_by_path(self, workspace_id: str, path: str) -> Document | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE workspace_id = ? AND path = ?", (workspace_id, path)
        ).fetchone()
        return self._row_to_document(row) if row else None

    def list_documents(self, workspace_id: str | None = None, limit: int = 1000) -> list[Document]:
        if workspace_id:
            rows = self._conn.execute(
                "SELECT * FROM documents WHERE workspace_id = ? ORDER BY mtime DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM documents ORDER BY mtime DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_document(r) for r in rows]

    def delete_document(self, doc_id: str) -> None:
        self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    def count_documents(self, workspace_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM documents WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return int(row["c"]) if row else 0

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> Document:
        return Document(
            id=row["id"],
            workspace_id=row["workspace_id"],
            path=row["path"],
            name=row["name"],
            kind=DocumentKind(row["kind"]),
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            mtime=_parsedt(row["mtime"]) or datetime.now(timezone.utc),
            indexed_at=_parsedt(row["indexed_at"]),
            page_count=row["page_count"],
            sheet_count=row["sheet_count"],
            slide_count=row["slide_count"],
            has_extracted_text=bool(row["has_extracted_text"]),
            extraction_method=(
                ExtractionMethod(row["extraction_method"]) if row["extraction_method"] else None
            ),
            extraction_error=row["extraction_error"],
            language_hints=json.loads(row["language_hints"]),
            pii_flagged=bool(row["pii_flagged"]),
        )

    # -- Chunks ---------------------------------------------------------------

    def replace_chunks(self, document_id: str, chunks: list[DocumentChunk]) -> None:
        self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        for ch in chunks:
            self._conn.execute(
                """
                INSERT INTO chunks(id, document_id, workspace_id, kind, ordinal, page_number,
                    sheet_name, cell_range, slide_number, section_heading, text, char_count,
                    confidence, extraction_method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ch.id,
                    ch.document_id,
                    ch.workspace_id,
                    ch.kind.value if hasattr(ch.kind, "value") else ch.kind,
                    ch.ordinal,
                    ch.page_number,
                    ch.sheet_name,
                    ch.cell_range,
                    ch.slide_number,
                    ch.section_heading,
                    ch.text,
                    ch.char_count,
                    ch.confidence,
                    (ch.extraction_method.value if ch.extraction_method else None),
                ),
            )

    def get_chunks(self, document_id: str) -> list[DocumentChunk]:
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal", (document_id,)
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_chunk(self, chunk_id: str) -> DocumentChunk | None:
        row = self._conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        return self._row_to_chunk(row) if row else None

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> DocumentChunk:
        return DocumentChunk(
            id=row["id"],
            document_id=row["document_id"],
            workspace_id=row["workspace_id"],
            kind=ChunkKind(row["kind"]),
            ordinal=row["ordinal"],
            page_number=row["page_number"],
            sheet_name=row["sheet_name"],
            cell_range=row["cell_range"],
            slide_number=row["slide_number"],
            section_heading=row["section_heading"],
            text=row["text"],
            char_count=row["char_count"],
            confidence=row["confidence"],
            extraction_method=(
                ExtractionMethod(row["extraction_method"]) if row["extraction_method"] else None
            ),
        )

    # -- FTS5 search ----------------------------------------------------------

    def fts_search(
        self,
        query: str,
        *,
        workspace_ids: list[str] | None = None,
        kinds: list[DocumentKind] | None = None,
        limit: int = 20,
    ) -> list[tuple[DocumentChunk, Document, float]]:
        """Return (chunk, document, bm25_score) tuples ordered by relevance.

        ``query`` is passed to FTS5 as a MATCH expression; callers should sanitize.
        """

        # Two-step query keeps column names unambiguous (chunks + documents
        # share several names — id, workspace_id, kind, etc.).
        clauses = ["chunks_fts MATCH ?"]
        params: list[Any] = [query]
        if workspace_ids:
            placeholders = ",".join("?" * len(workspace_ids))
            clauses.append(f"chunks.workspace_id IN ({placeholders})")
            params.extend(workspace_ids)
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            clauses.append(f"documents.kind IN ({placeholders})")
            params.extend([k.value if hasattr(k, "value") else k for k in kinds])

        sql = f"""
            SELECT chunks.id AS chunk_id, bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks ON chunks.rowid = chunks_fts.rowid
            JOIN documents ON documents.id = chunks.document_id
            WHERE {' AND '.join(clauses)}
            ORDER BY score
            LIMIT ?
        """
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out: list[tuple[DocumentChunk, Document, float]] = []
        for r in rows:
            chunk = self.get_chunk(r["chunk_id"])
            if chunk is None:
                continue
            doc = self.get_document(chunk.document_id)
            if doc is None:
                continue
            out.append((chunk, doc, float(r["score"])))
        return out

    # -- Fields / entities ----------------------------------------------------

    def replace_extracted_fields(self, document_id: str, fields: list[ExtractedField]) -> None:
        self._conn.execute("DELETE FROM extracted_fields WHERE document_id = ?", (document_id,))
        for f in fields:
            self._conn.execute(
                """
                INSERT INTO extracted_fields(document_id, chunk_id, key, value, value_type,
                    confidence, page_number, cell_range, extractor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f.document_id,
                    f.chunk_id,
                    f.key,
                    f.value,
                    f.value_type,
                    f.confidence,
                    f.page_number,
                    f.cell_range,
                    f.extractor,
                ),
            )

    def get_fields(self, document_id: str) -> list[ExtractedField]:
        rows = self._conn.execute(
            "SELECT * FROM extracted_fields WHERE document_id = ?", (document_id,)
        ).fetchall()
        return [
            ExtractedField(
                document_id=r["document_id"],
                chunk_id=r["chunk_id"],
                key=r["key"],
                value=r["value"],
                value_type=r["value_type"],
                confidence=r["confidence"],
                page_number=r["page_number"],
                cell_range=r["cell_range"],
                extractor=r["extractor"],
            )
            for r in rows
        ]

    def replace_entities(self, document_id: str, entities: list[Entity]) -> None:
        self._conn.execute("DELETE FROM entities WHERE document_id = ?", (document_id,))
        for e in entities:
            self._conn.execute(
                "INSERT INTO entities(document_id, chunk_id, text, kind, confidence) VALUES (?, ?, ?, ?, ?)",
                (e.document_id, e.chunk_id, e.text, e.kind, e.confidence),
            )

    def get_entities(self, document_id: str) -> list[Entity]:
        rows = self._conn.execute(
            "SELECT * FROM entities WHERE document_id = ?", (document_id,)
        ).fetchall()
        return [
            Entity(
                document_id=r["document_id"],
                chunk_id=r["chunk_id"],
                text=r["text"],
                kind=r["kind"],
                confidence=r["confidence"],
            )
            for r in rows
        ]

    def find_documents_with_entity(
        self,
        text: str,
        *,
        kind: str | None = None,
        workspace_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[str]:
        """Return document_ids that contain an entity matching ``text``.

        Case-insensitive substring match. Cheap — used by the ranker to add
        a boost when the user query mentions a company / email / domain.
        """
        clauses = ["LOWER(entities.text) LIKE ?"]
        params: list[Any] = [f"%{text.lower()}%"]
        if kind:
            clauses.append("entities.kind = ?")
            params.append(kind)
        if workspace_ids:
            placeholders = ",".join("?" * len(workspace_ids))
            clauses.append(f"documents.workspace_id IN ({placeholders})")
            params.extend(workspace_ids)
        sql = f"""
            SELECT DISTINCT entities.document_id
            FROM entities
            JOIN documents ON documents.id = entities.document_id
            WHERE {' AND '.join(clauses)}
            LIMIT ?
        """
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [r["document_id"] for r in rows]

    # -- Audit log ------------------------------------------------------------

    def append_audit(self, entry: AuditLogEntry) -> None:
        self._conn.execute(
            """
            INSERT INTO audit_log(id, timestamp, actor, operation, user_request, tool,
                referenced_files, output_files, risk, user_approved, error, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                _isofmt(entry.timestamp),
                entry.actor,
                entry.operation,
                entry.user_request,
                entry.tool,
                json.dumps(entry.referenced_files),
                json.dumps(entry.output_files),
                entry.risk.value if hasattr(entry.risk, "value") else entry.risk,
                None if entry.user_approved is None else int(entry.user_approved),
                entry.error,
                json.dumps(entry.extra),
            ),
        )

    def recent_audit(self, limit: int = 50) -> list[AuditLogEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            AuditLogEntry(
                id=r["id"],
                timestamp=_parsedt(r["timestamp"]) or datetime.now(timezone.utc),
                actor=r["actor"],
                operation=r["operation"],
                user_request=r["user_request"],
                tool=r["tool"],
                referenced_files=json.loads(r["referenced_files"]),
                output_files=json.loads(r["output_files"]),
                risk=OperationRiskLevel(r["risk"]),
                user_approved=None if r["user_approved"] is None else bool(r["user_approved"]),
                error=r["error"],
                extra=json.loads(r["extra"]),
            )
            for r in rows
        ]
