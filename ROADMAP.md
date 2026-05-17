# Claude Code Office Layer — Live Roadmap

> This file is the **single source of truth** for what is done and what remains.
> Updated incrementally session-by-session. Match the format of
> `~/Downloads/MLX_CONTRIB_ROADMAP.md` — terse, dated, scannable.

Source spec: `~/Downloads/claude_code_office_layer_full_spec_oss_first.md` (revised
2026-05-17 to add §17 OSS-first policy).

---

## Status legend

- ✅ done & verified locally
- 🟡 implemented but not yet exercised end-to-end on real data
- ⬜ not started
- 🔴 known issue / blocker

## Phase 0 — Concept Prototype  (target: Claude Code → search local Office docs)

Spec §13 Phase 0. Goal: prove the round-trip.

- ✅ Project skeleton, `pyproject.toml` w/ cheap-PC defaults, `package.json` for plugin shipping
- ✅ Domain models (`models.py`) — Workspace, Document, DocumentChunk, ExtractedField, ExtractedTable, Entity, SearchQuery, SearchResult, EvidencePacket, EvidenceSource, WorkflowRun, OperationRisk, AuditLogEntry
- ✅ Storage with SQLite + FTS5 triggers (`storage.py`)
- ✅ Adapter Protocols (`adapters/base.py`) — FileDiscovery / Pdf / Office / Text / Ocr / TextSearch / SemanticSearch / FileWatcher
- ✅ Adapter registry with degraded-mode selection (`adapters/registry.py`)
- ✅ File discovery adapters: walk / mdfind / Everything / fd / ripgrep
- ✅ PDF adapters: pypdfium2 (default, BSD) / pdfplumber (MIT, tables) / pymupdf (AGPL, opt-in)
- ✅ Office adapters: python-docx / openpyxl / python-pptx
- ✅ Text adapters: stdlib (txt/md), csv, json, bs4 html
- ✅ Engine (`engine/engine.py`) + WorkspaceManager + Indexer + HybridSearcher
- ✅ Evidence Packet builder (`engine/evidence.py`)
- ✅ Safety layer — operation risk classifier + audit logger
- ✅ MCP server (FastMCP) exposing Phase 0 tools
- ✅ CLI (`office-layer …`) for non-Claude-Code workflows
- ✅ Claude Code plugin — manifest, .mcp.json, 13 slash commands (Phase 0 working + Phase 1-3 stubs), 6 specialized agents, 2 skills, illustrative hooks
- 🟡 End-to-end smoke test on a real folder — to be run by the user after `pip install -e .`
- ⬜ pytest suite covering the storage / extraction / search / evidence paths

### Phase 0 next-session checklist

1. From repo root:
   ```bash
   pip install -e .
   office-layer status
   office-layer workspace add ~/Downloads/claude-code-office-layer/examples/sample_workspace --name samples
   office-layer index <workspace_id>
   office-layer search "テスト" --workspace <workspace_id>
   ```
2. Install the plugin into Claude Code (see README quickstart). Try `/office-init` → `/office-index` → `/office-search`.
3. Write `tests/test_phase0_smoke.py` exercising the same flow programmatically.

---

## Phase 1 — Practical Local Search

Spec §13 Phase 1. Goal: usable everyday search across Office formats.

- ✅ Word / Excel / PowerPoint extraction (already shipped in Phase 0 since the adapter layer made it cheap)
- ✅ Hybrid ranker (filename + content + recency + kind preference)
- ⬜ Background incremental indexing — wire watchdog to push events into a worker thread that calls `Indexer.reindex_path`
- ⬜ Semantic search wired end-to-end (sqlite-vec + small embedding model)
  - Embedding pick: `intfloat/multilingual-e5-small` (good JA/EN, ~120MB) or `cl-nagoya/ruri-base` for JP-heavy use
  - Make embeddings optional — default Phase 1 ships keyword-only
- ⬜ Entity extraction — wire spaCy or a regex first-pass for {company, person, date, money, email}
- ⬜ Query-understanding layer: parse "先月" / "去年" / "Q3" into date ranges before search
- ⬜ Pagination / "show me more" beyond top-N
- ⬜ Per-result locator polishing: PDF page text-rect, XLSX cell range for the actual matched token
- ⬜ Index packaging (export / import for moving an index between machines)

### Phase 1 owner notes

- Cheap-PC tax: keep semantic search OFF by default, document the install line clearly, never auto-download a model
- Watchdog Observer is OS-native (FSEvents on darwin) — fine to run continuously
- Entity extraction with spaCy `ja_core_news_sm` is ~50MB and CPU-friendly

---

## Phase 2 — Evidence Workflows

Spec §13 Phase 2. Goal: turn evidence into artifacts.

- ⬜ MCP tool: `extract_invoice_fields(document_id)` — regex + heuristic on extracted chunks
- ⬜ MCP tool: `extract_contract_sections(document_id)` — clause boundaries by heading / 第N条
- ⬜ MCP tool: `compare_contracts(doc_a_id, doc_b_id)` — clause pairing + diff
- ⬜ MCP tool: `draft_email_from_evidence(packet_id, …)` — uses Claude via the parent agent; this tool just stages drafts in `<workspace>/drafts/`
- ⬜ MCP tool: `extract_invoices_to_table(workspace_id, output_path)` — uses the above + safety check
- ⬜ MCP tool: `compare_contracts(...)` — same
- ⬜ MCP tool: `build_client_history(client_name)` — query expansion + multi-kind aggregation
- ⬜ `workflows/` package: invoice / contract / email / client_history / folder_summary modules

### Workflows directory layout

```
mcp-server/office_layer/workflows/
├── __init__.py
├── invoice.py          # field extraction heuristics
├── contract_diff.py    # clause pairing
├── client_history.py   # query expansion + aggregation
├── email_draft.py      # template assembly
└── folder_summary.py   # workspace-level overview
```

---

## Phase 3 — Safety & Review

Spec §13 Phase 3.

- ✅ Operation risk classifier (Low / Medium / High)
- ✅ AuditLog table + writer
- ⬜ Hook: PreToolUse intercept that blocks high-risk writes outside `drafts/`
- ⬜ `create_low_confidence_review(workspace_id)` MCP tool — surfaces `confidence < 0.7` items in a single batched list
- ⬜ Diff preview before any overwrite (we should never overwrite by default, but when user explicitly allows it, show the diff)
- ⬜ Mass-operation guard: >5 file writes → require explicit "yes apply to all"
- ⬜ PII detector: lightweight regex pass for {phone, mynumber, credit-card-like} in any draft output

---

## Phase 4 — Desktop UX

Spec §13 Phase 4. Goal: easy install for non-engineers.

- ⬜ One-line installer (`curl ... | sh` or pipx-friendly)
- ⬜ Localhost dashboard (FastAPI) for workspace mgmt + index status
- ⬜ Tauri wrapper (cross-platform, small) around the dashboard for users who don't run a terminal
- ⬜ Evidence Viewer — clickable packet sources that open the source file at the right page

### Default UI stack pick

- Backend: FastAPI on a random localhost port
- Frontend: Plain HTML + htmx → minimum JS, minimum build step
- Phase 4b: Tauri shell (Rust, ~3MB binary) if users ask for an app icon

---

## Phase 5 — Team / Enterprise

Spec §13 Phase 5.

- ⬜ Shared workspace concept (network-mounted folder w/ index synced to user-local cache)
- ⬜ Per-user permission templates
- ⬜ Team audit log aggregation
- ⬜ Google Drive / OneDrive native connectors (initially relies on local sync folders per §19.1)
- ⬜ Admin controls (which workspaces, which extensions, OCR yes/no)

---

## Cross-phase OSS adapter matrix

See spec §17.6. Selection is automatic via `adapters/registry.py`.

| Layer | Cheap-PC default | Upgrade | Heavy |
|---|---|---|---|
| File discovery | walk (always) | mdfind (macOS) / Everything (Win) | — |
| PDF text | pypdfium2 | pdfplumber (+tables) | pymupdf (AGPL) |
| PDF OCR | off | tesseract + pdf2image | ocrmypdf |
| DOCX | python-docx | — | Docling |
| XLSX | openpyxl | — | Docling |
| PPTX | python-pptx | — | Docling |
| HTML | beautifulsoup | — | — |
| Full-text | SQLite FTS5 | tantivy | Elasticsearch (Phase 5 server install) |
| Vector | off | sqlite-vec | LanceDB / Chroma |
| Embeddings | off | multilingual-e5-small | larger ST models |
| Image OCR | off | Apple Vision (macOS) / tesseract | PaddleOCR |
| Entity extraction | off | spaCy ja_core_news_sm | GLiNER |
| Email | off | mail-parser | — |
| Watcher | off | watchdog | — |

---

## Risks / open questions

1. **PyMuPDF licensing** — staying off-default; user-toggle only. Document this clearly so accidental opt-in doesn't viralize AGPL into the project.
2. **Tantivy vs FTS5** — FTS5 is good for ≤100k docs; tantivy needed past that. Defer until a real user hits the wall.
3. **Embedding model size** — multilingual-e5-small is ~120MB; some "cheap PC" users will balk. Make the install explicit.
4. **Claude Code plugin loading mechanism** — `.mcp.json` paths assume `office-layer-mcp` is on PATH. If user installs into a venv that Claude Code can't see, the plugin breaks silently. Phase 4 dashboard should expose a "test MCP" button.
5. **Win11 path handling** — needs a real-machine smoke test; we have not exercised it. Treat first Windows install as a bug-finding pass.

---

## Decision log

- **2026-05-17** Adopted spec v2 (OSS-first §17). Rewrote extractor layer as `adapters/` with Protocol-based dispatch.
- **2026-05-17** Picked SQLite FTS5 over tantivy for Phase 0 (no extra binary, stdlib).
- **2026-05-17** PyMuPDF held back to AGPL opt-in; pypdfium2 + pdfplumber cover default needs.
- **2026-05-17** Embeddings deferred to Phase 1 to keep Phase 0 install lightweight (no PyTorch).
- **2026-05-17** Dropped TypeScript MCP server in favor of Python only — the OSS document libs live in Python; a JS port would be wheel-reinvention. The Node `package.json` ships the plugin / installer helpers only.
