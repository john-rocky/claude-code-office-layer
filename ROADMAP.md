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
- ✅ Hybrid ranker (filename + content + recency + kind preference + entity boost)
- ✅ **Entity extraction** (regex baseline) — company / person / money / date / email / phone / url. Indexer stores via `Storage.replace_entities`. Ranker bumps documents whose entities match query terms (`reason: entity ~ '<term>'`). spaCy / GLiNER opt-in extras left as Phase 1.5 stretch.
- ✅ **Query understanding** — `engine/query_understanding.py`. Parses 先月 / 去年 / 今年 / Q3 / 2025年4月 / April 2025 / 2024 into UTC date ranges. Period filter is HARD (user explicitly asked). Kind hints stay SOFT (boost only, never filter — proven by sample workspace where markdown invoices still surface for "請求書" queries even with no PDFs around).
- ✅ **FTS5 prefix matching** — "請求" now hits "請求書"/"請求日" tokens. Each token emitted as both exact and prefix variants OR'd together.
- ✅ **Background incremental indexing** — `engine/watcher.py` BackgroundIndexer + watchdog Observer per workspace, debounced 2s. MCP tools: `start_watch` / `stop_watch` / `list_watches`. CLI: `office-layer watch start/stop/list`. Integration tests (tmp dir + real watchdog) cover create + delete events.
- ✅ **Semantic search wired end-to-end** (sqlite-vec + small embedding model)
  - Storage owns `vec_chunks` (sqlite-vec `vec0`) keyed by chunk_id with workspace_id + document_id aux columns. Lazy `enable_vector_index(dim)` so cheap-PC installs never load the extension. `delete_document` drops vectors before metadata.
  - `engine/embedder.py`: `FastembedEmbedder` (default, onnxruntime, ~200MB) → `SentenceTransformersEmbedder` (heavy, ~2GB PyTorch) → `NullEmbedder` fallback. Env override: `OFFICE_LAYER_EMBEDDER` / `OFFICE_LAYER_EMBED_MODEL`.
  - `engine/semantic.SemanticIndex` glues embedder + storage with lazy init (embedder model loads only on first index, never on `status`).
  - Indexer calls `semantic.index_chunks` after replacing chunks, gated on `ws.enable_vector_search`. Embedding lives outside the metadata `tx()` so slow embeds don't block FTS lookups.
  - HybridSearcher merges vector hits into the candidate set with `reason: semantic` and a baseline score floor for pure-semantic chunks. RRF boost fires only when both keyword + vector lists returned hits (no double-counting top FTS).
  - Per-workspace opt-in is honoured in both indexer and searcher — a query with `workspace_ids=None` skips semantic entirely.
  - CLI: `office-layer workspace add --enable-vector`, `office-layer workspace set-vector <id> on|off`. MCP: `set_workspace_vector_search`. `status` shows `sqlite-vec + fastembed` or the missing-piece hint.
  - Tests: `tests/test_semantic_search.py` (5 cases) — null fallback, disabled workspace no-op, semantic hit recovers a chunk FTS misses, vector cleanup on delete, no cross-workspace leak. Uses a deterministic 4-d FakeEmbedder so CI never downloads weights.
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

- ✅ **MCP tool `extract_invoice_fields(document)`** — regex + label-anchored heuristic over indexed chunks. Three strategies per spec entry (same-line `label: value`, section-header `## label \n value`, fallback inline ID match). Detects: `invoice_number`, `issue_date`, `due_date`, `subtotal`, `tax`, `total`, `recipient`, `issuer`, `payment_account`. Confidence 0.95 / 0.75 / 0.65 by strategy. Verified against JP section-header MD, JP inline-colon MD, English invoice, XLSX layout, and the receipt-only edge case (5 fixtures under `examples/sample_workspace/invoices/`). CLI: `office-layer invoice extract <path-or-id>`. Persists into `extracted_fields` when `persist=true` (default). 12 dedicated tests in `tests/test_invoice_extraction.py`.
- ✅ **MCP tool `extract_contract_sections(document)`** — clause boundary regex over the chunk-concatenated text. Detects `第N条` (with/without markdown `##` prefix), `Section N` / `Article N` / `Clause N` (separator-tolerant), and generic `## N.` / `## N)` headings. Bare-line JP/EN clause markers are accepted only when the line is short (≤60 chars) AND has no prose sentence terminator, so inline references like `前条 と 第3条 に従い…` do not split a clause body. Output dict: `{document, preamble, sections: [{ordinal, title, heading, body, char_offset, body_char_count, body_truncated}], section_count, persisted}`. Body cap default 4 KB → `body_truncated=true` flag persisted only when actually truncated. Persistence uses a merge-write under the `section.{n}.{title,heading,body,body_truncated}` key namespace so it does NOT clobber non-`section.*` extracted fields (e.g. invoice fields written earlier on the same doc). CLI: `office-layer contract sections <doc-id-or-path> [--no-persist] [--body-cap N] [--json-output]`. 18 dedicated tests in `tests/test_contract_sections.py` covering JP NDA shape, EN Section/Article, numbered headings, bare-line JP, inline-reference safety, body truncation, multi-chunk stitching, merge-write preserves invoice fields, re-run drops stale section keys.
- ✅ **MCP tool `compare_contracts(doc_a, doc_b)`** — clause pairing + diff that builds on `extract_contract_sections`. Pairing prefers ordinal match (rejected if titles disagree below the 0.40 SequenceMatcher floor — handles renumbered drafts), with a greedy title-edit-distance fallback for the leftovers. Body classification: `identical` (byte-equal after per-line whitespace strip) / `wording` (char-edit ratio < 0.10) / `substantive` (anything else). Override: if the digit set differs between bodies OR the count of negation tokens (`なし`/`無し`/`無`/`ない`) flips, the pair is always substantive regardless of edit ratio — so the canonical `3年間 → 5年間` term-length flip is classified correctly even though it is only a one-char diff. Each non-identical pair carries unified-diff hunks for the clause body (3-line context). Nothing is persisted. CLI: `office-layer contract diff <doc-a> <doc-b> [--json-output]`. 14 dedicated tests in `tests/test_contract_diff.py`: 9 pure-function cases + 5 integration cases including the NDA fixture pair (`nda-old.md` vs `nda-new.md`) verifying §2 (3年→5年) + §3 (上限なし→契約金額上限) surface as substantive while §1/§4/§5 stay identical.
- ⬜ MCP tool: `draft_email_from_evidence(packet_id, …)` — uses Claude via the parent agent; this tool just stages drafts in `<workspace>/drafts/`
- ✅ **MCP tool `extract_invoices_to_table(workspace_id, output_path)`** — workspace-scoped batch counterpart to `extract_invoice_fields`. Loops the field extractor over every PDF / MD / XLSX / DOCX in the workspace (kind filter — txt/csv/json never produce a real invoice_number and would only pollute the CSV), keeps the docs that produced `invoice_number` after extraction, projects them into a 12-column CSV (`invoice_number, issue_date, due_date, issuer, recipient, subtotal, tax, total, currency, payment_account, source_path, confidence_avg`). `currency` is derived from the parsed `total` via the same `parse_amount` already used by `build_client_history`. `confidence_avg` averages only over fields that actually fired so a sparse extraction is not double-penalised against the 7 missing ones. Safety: refused when the workspace is `read-only`, or when the output target resolves outside the workspace root — both surface as HIGH-risk through the existing `classify_operation("export_csv", …)` classifier (no new safety code). Output path: relative resolves against the workspace root and a `-YYYYMMDD-HHMMSS` suffix is appended before the extension so re-running never silently overwrites a prior export. `run_extractor=false` lets callers project pre-persisted fields without re-running the regex. CLI: `office-layer invoice export <workspace> <path> [--no-extract] [--json-output]`. 16 dedicated tests in `tests/test_invoices_table.py`: 6 pure-function (drop-without-invoice-number, currency derivation, column lockstep, sparse-confidence, source_path) + 10 integration (end-to-end CSV write, skip-non-invoice, kind filter rejects txt, read-only refusal, outside-workspace refusal, no-extract path, empty workspace, unknown workspace, low-confidence flagging, timestamp-distinct consecutive writes).
- ✅ **MCP tool `build_client_history(client_name)`** — regex alias expansion (JP `株式会社`/EN `Inc.|LLC|…` suffix stripping + email local/domain/first-label + caller-supplied aliases). Recall by `find_documents_with_entity` + filename fallback so docs the entity regex missed still surface. Bucketing by `extracted_fields` presence (invoice → has persisted `invoice_number`) + `/invoices/`-`/contracts/` path heuristic + `DocumentKind.EMAIL`. Multi-currency totals grouped (no FX), timeline merged across kinds sorted mtime desc. Cross-workspace by default; `workspace_ids=[...]` filters. CLI: `office-layer client history "ACME 株式会社" [--alias X] [--workspace-id W]`. 21 dedicated tests in `tests/test_client_history.py`.
- ⬜ `workflows/` package: invoice ✅ / invoices_table ✅ / contract_sections ✅ / contract_diff ✅ / email / client_history ✅ / folder_summary ✅

### Workflows directory layout

```
mcp-server/office_layer/workflows/
├── __init__.py
├── invoice.py            # per-document field extraction heuristics
├── invoices_table.py     # workspace-scoped batch CSV export
├── contract_sections.py  # clause boundary detection
├── contract_diff.py      # clause pairing
├── client_history.py     # query expansion + aggregation
├── email_draft.py        # template assembly (stub — next)
└── folder_summary.py     # workspace-level overview
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
- **2026-05-17** Phase 1.4 semantic search shipped. Embedding default is fastembed (onnxruntime) bundled into the `[vec-sqlite]` extra so users get a working pipeline from one install line — sentence-transformers stays available as a heavier opt-in. RRF only fires when both keyword + vector returned hits, to avoid rewarding top FTS twice when the embedder is missing.
- **2026-05-17** Phase 2 invoice extractor shipped as `extract_invoice_fields`. Picked a 3-strategy label-anchored regex over an LLM call — keeps the tool offline, deterministic, and easy to test (12 cases, 0.25s). The LLM path stays open for later as a fallback when the heuristic returns `low_confidence_keys`. Word-boundary check on ASCII labels was added after a "Total" / "Subtotal" collision surfaced on the English fixture.
- **2026-05-17** Phase 2 client-history shipped as `build_client_history`. Picked the regex-baseline alias expansion (strip JP/EN corporate suffixes, expand email → local/domain/first-label, accept caller `aliases`) over spaCy/GLiNER — `find_documents_with_entity` is already LOWER LIKE substring so the regex stem is enough recall and we avoid the 50MB+ model download. Bucketing by `extracted_fields` presence (invoice = has `invoice_number`) + path/name heuristics keeps DB schema unchanged; the alternative `business_kind` column would have cost a SCHEMA_VERSION bump for no extra signal. Multi-currency totals grouped per ISO code rather than FX-normalised — the wrong default beats no number. NDA fixtures (`examples/sample_workspace/contracts/nda-{old,new}.md`) gained a "本契約は ACME 株式会社 と ジョン・ロッキー との間で締結する。" line so manual `office-layer client history "ACME"` against the sample workspace surfaces both invoices and contracts as the demo intends.
- **2026-05-17** Phase 2 contract diff shipped as `compare_contracts`. Built on `extract_contract_sections` so the heading detection stays single-source. Pairing: ordinal first, fallback by greedy title edit distance, with a 0.40 SequenceMatcher floor applied to **both** passes — the floor on the ordinal pass is what makes renumbered drafts (B inserts a new clause upstream, pushing the same clause down by 1) pair correctly via the title fallback instead of gluing two unrelated clauses together. Status thresholds: identical (byte-equal post per-line whitespace strip) / wording (char edit ratio < 0.10) / substantive (≥0.10). Added a substantive-marker override: if the *set* of digit runs differs between bodies, or the count of negation tokens (`なし` / `無し` / `無` / `ない`) flips, classify substantive regardless of edit ratio. Without that override the canonical `3年間 → 5年間` term-length change in the NDA fixture would land at ratio ≈ 0.025 (one char in a ~40-char body) and get filed under "wording", which is contractually wrong — that change is exactly the kind of thing a diff tool exists to surface. No persistence: the diff is a one-shot comparison artifact, the inputs already live in storage. CLI: `office-layer contract diff` prints a status-coloured table + inline unified-diff hunks for non-identical clauses. 14 dedicated tests (9 pure + 5 integration on the shipped NDA fixtures).
- **2026-05-17** Phase 2 contract sectioner shipped as `extract_contract_sections`. Persisted via `ExtractedField` under the `section.{n}.{title,heading,body,body_truncated}` namespace rather than a new `sections` SQL table — keeps DB schema stable and reuses the existing field reader. Used a merge-write that only replaces rows whose key starts with `section.` (and leaves invoice/other keys alone) so the sectioner is safe to run on a doc that was previously invoice-extracted. Bare-line JP/EN clause markers ARE accepted (not markdown-only) because contracts piped through PDF extractors lose the `##` prefix — but the bare-line case is gated on `len(line) ≤ 60` AND no sentence terminator `。`/`.` to keep inline references like `前条 と 第3条 に従い…` from splitting a clause body. Body cap is 4 KB by default with a `section.{n}.body_truncated=true` flag emitted only when actually truncated; `body_char_count` always carries the original body length so the caller can tell what was dropped. Chunk stitching uses ordinal-sorted `"\n\n".join(...)` so a clause split across two PDF chunks reassembles into a single section.
- **2026-05-17** Phase 2 invoice-table batch export shipped as `extract_invoices_to_table`. New module `workflows/invoices_table.py` rather than extending `workflows/invoice.py` because the per-doc extractor and the workspace-scoped projector are two different abstractions; mirrors the `contract_sections.py` / `contract_diff.py` split. Deleted the unused `workflows/csv_export.py` stub — the export of invoices is invoice-specific, not a generic "rows → csv" helper, and no caller depended on the stub. Safety gate reuses the existing `classify_operation("export_csv", …)` classifier: read-only workspaces escalate writes to HIGH (refused), targets outside the workspace root escalate to HIGH (refused). No new safety code was added because the classifier already encodes the two rules we care about. Kind filter is `{PDF, MARKDOWN, XLSX, DOCX}` — txt receipts / csv ledgers / json dumps never produce a real `invoice_number` under the current regex set and would only pollute the CSV; the `receipt-coffee.txt` fixture is the canonical example. The "what is an invoice" signal is **post-extraction** `invoice_number` presence (not a kind heuristic, not the filename): the extractor runs over every kind-matching doc, and only the rows that produced `invoice_number` survive into the CSV. This means non-invoice markdowns under the workspace (meeting notes, READMEs) silently land in `skipped[]` with `reason: "no invoice_number after extraction"`. `currency` column is derived from the parsed `total` via the same `parse_amount` `client_history` already uses — kept as a string column (`"JPY"` / `"USD"` / `""`) so the CSV stays human-readable; numeric normalisation can be a downstream step. `confidence_avg` averages only over fields that actually fired (not over the 10 invoice keys) so a sparse extraction with 2 high-confidence fields gets a high avg, not a falsely low one. Output path: relative resolves against the workspace root and we ALWAYS append a `-YYYYMMDD-HHMMSS` suffix before the extension — no `--overwrite` flag, no "skip if file exists" mode, because the contract for a batch CSV export is "always produce a fresh artifact" and silently overwriting last week's CSV is exactly what the safety story is meant to prevent. `run_extractor=false` lets a Claude-driven flow that already populated the fields skip the per-doc regex pass.
