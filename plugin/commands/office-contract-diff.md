---
description: Compare two contract versions and report clause-by-clause differences ranked by risk. (Phase 2)
allowed-tools: ["mcp__office-layer__extract_document_text", "mcp__office-layer__build_evidence_packet"]
---

**Phase 2 workflow — wired on top of Phase 0 primitives.**

Inputs from `$ARGUMENTS`: two file paths or document IDs (old + new).

Steps:
1. `extract_document_text` for both files.
2. Identify clause boundaries (見出し / 条番号 / Heading) by scanning chunk `section_heading` / leading numerals.
3. Pair clauses across versions by heading similarity.
4. For each clause: classify the diff (no-change / wording / substantive / removed / added).
5. Rank substantive diffs first, by topic priority: payment terms > term & termination > liability > IP > confidentiality > governing law > misc.
6. Report each diff with both quotes side-by-side and clear citations (file, page/section).
7. Add a disclaimer: this is decision support, not legal advice — the user must confirm.
