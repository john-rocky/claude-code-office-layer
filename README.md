# Claude Code Office Layer

> Local Office Evidence Layer for Claude Code — turn the messy PDFs, Excel
> sheets, Word docs, and scans on your PC into grounded, actionable context
> Claude Code can use to do real office work safely.

This is **not** another RAG chatbot, document parser, OCR engine, or vector
database. The Office Layer is OSS-first by design (spec §17): mature local
infrastructure is wrapped behind replaceable adapters, and the unique value
sits in the **Evidence Packet**, **hybrid ranker**, **workflow templates**,
and **safety policy** that connect those components to Claude Code's
agentic workflow.

## Quickstart

### 1. Install the MCP server

```bash
pip install -e .
# or, with optional extras:
pip install -e '.[recommended]'   # adds pypdfium2, OCR, sqlite-vec, email, macOS Vision
pip install -e '.[all]'           # everything except AGPL PyMuPDF
```

Phase 0 ships with sensible defaults that work on a cheap PC: SQLite FTS5
for search, `pdfplumber` for PDFs, `python-docx` / `openpyxl` /
`python-pptx` for Office. No PyTorch, no ML download, no JVM, no daemon.

### 2. Register the Claude Code plugin

Add to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "plugins": ["/Users/you/Downloads/claude-code-office-layer/plugin"]
}
```

Restart Claude Code. You should see new slash commands beginning with `/office-`.

### 3. Try it

In Claude Code:

```text
/office-init
```

Claude will walk you through picking a folder. Then:

```text
/office-index <workspace_id>
/office-search "去年のA社の請求書"
```

### CLI alternative

The same engine is available without Claude Code:

```bash
office-layer status
office-layer workspace add ~/Documents --name docs
office-layer index <workspace_id>
office-layer search "invoice 2025"
office-layer evidence "draft invoice follow-up" "A社 請求"
```

## What is an Evidence Packet?

The product's central abstraction (spec §9.5). When Claude Code is about
to draft an artifact (CSV, email, report) the Office Layer hands it a
small, grounded packet:

```jsonc
{
  "packet_id": "ep_8c1...",
  "intent": "draft invoice follow-up email",
  "sources": [
    {
      "source_id": "chk_8a91...",
      "file_path": "/Users/me/Documents/Clients/ACME/INV-2025-03.pdf",
      "file_name": "INV-2025-03.pdf",
      "file_type": "pdf",
      "page_number": 1,
      "extracted_text": "...請求書 2025/03/15 ¥120,000 支払期限 2025/04/30...",
      "relevance_score": 8.4,
      "confidence_score": 1.0,
      "extraction_method": "pdf-text",
      "reason_for_inclusion": "filename ~ 'INV-2025-03'; kind == pdf; recent",
      "suggested_next_action": "cite by page number when quoting"
    }
  ],
  "low_confidence_items": []
}
```

Claude then drafts off the packet. No silent quoting from beyond it.

## Architecture

```
                       ┌─────────────────────────┐
   Claude Code ───────▶│  Plugin                 │
                       │  /office-search etc.    │
                       │  document-finder agent  │
                       │  evidence-builder agent │
                       │  safety-reviewer agent  │
                       └────────────┬────────────┘
                                    │ MCP (FastMCP, stdio)
                                    ▼
                       ┌─────────────────────────┐
                       │  MCP Server             │
                       │  office-layer-mcp       │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  Engine                 │
                       │  ├ WorkspaceManager     │
                       │  ├ Indexer              │
                       │  ├ HybridSearcher       │
                       │  ├ EvidencePacketBuilder│
                       │  └ Safety (risk+audit)  │
                       └────────────┬────────────┘
                                    │
              ┌────────────┬────────┼────────────┬────────────┐
              ▼            ▼        ▼            ▼            ▼
        ┌──────────┐ ┌─────────┐ ┌──────┐  ┌──────────┐ ┌──────────┐
        │ Adapters │ │ Storage │ │ FTS5 │  │ Adapters │ │ Adapters │
        │ pdf      │ │ SQLite  │ │ idx  │  │ ocr      │ │ watcher  │
        │ office   │ │         │ │      │  │ vector   │ │          │
        │ text     │ │         │ │      │  │          │ │          │
        │ discovery│ │         │ │      │  │          │ │          │
        └──────────┘ └─────────┘ └──────┘  └──────────┘ └──────────┘
```

Every external OSS / OS facility is hidden behind a Protocol in
`office_layer.adapters.base` and selected at startup via
`office_layer.adapters.registry`. Missing optional deps degrade to a clear
error instead of crashing — see `office-layer status`.

## Project layout

```
claude-code-office-layer/
├── plugin/                        # Claude Code plugin
│   ├── .claude-plugin/plugin.json
│   ├── .mcp.json
│   ├── commands/                  # slash commands (Phase 0 working + Phase 1-3 stubs)
│   ├── agents/                    # specialized subagents
│   ├── skills/                    # skills
│   └── hooks/hooks.json
├── mcp-server/office_layer/       # Python package: engine + adapters + MCP server
│   ├── models.py
│   ├── storage.py
│   ├── paths.py
│   ├── server.py                  # MCP entrypoint (FastMCP)
│   ├── cli.py                     # office-layer command
│   ├── adapters/                  # OSS adapters (spec §17.5)
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── file_discovery/
│   │   ├── pdf/
│   │   ├── office/
│   │   ├── text/
│   │   ├── ocr/
│   │   ├── semantic_search/
│   │   └── file_watcher/
│   ├── engine/                    # workspace, indexer, searcher, evidence
│   ├── safety/                    # risk classifier, audit logger
│   └── workflows/                 # Phase 2+ workflows (stubs)
├── examples/sample_workspace/     # tiny corpus for smoke tests
├── tests/
├── docs/
├── ROADMAP.md                     # live progress tracker — read this every session
├── pyproject.toml
└── package.json
```

## OSS used (selected, all permissive)

| Layer | Library | License |
|---|---|---|
| MCP | mcp (Anthropic) | MIT |
| PDF text | pypdfium2 | BSD/Apache |
| PDF tables | pdfplumber | MIT |
| DOCX | python-docx | MIT |
| XLSX | openpyxl | MIT |
| PPTX | python-pptx | MIT |
| HTML | beautifulsoup4 | MIT |
| FTS | SQLite FTS5 | Public domain |
| File watch | watchdog | Apache 2.0 |
| OCR (optional) | Tesseract / pytesseract | Apache / MIT |
| Vector (optional) | sqlite-vec | Apache 2.0 |
| Embedding (optional) | sentence-transformers | Apache 2.0 |

PyMuPDF (AGPL/commercial) is **opt-in only** via `OFFICE_LAYER_PDF=pymupdf`
so the rest of the project stays cleanly MIT.

## Status

Phase 0 implemented. See `ROADMAP.md` for live progress on Phases 1-5.

## License

MIT. See `LICENSE`.
