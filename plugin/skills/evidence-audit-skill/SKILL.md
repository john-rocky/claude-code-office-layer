---
name: evidence-audit-skill
description: Use after a draft / CSV / report has been produced. Walks every concrete claim, checks it cites a source from the Evidence Packet, and flags anything ungrounded or low-confidence.
---

# Evidence Audit Skill

Activate when reviewing an artifact (email draft, CSV, report) before the user sends or shares it.

Checks:

1. **Every factual claim has a citation.** Amount / date / name / clause → must point to a packet source (file + locator).
2. **Low-confidence items are flagged in-line.** OCR-derived numbers or chunks below 0.7 confidence get a `⚠️` or footnote.
3. **No unsupported summary.** A summary line like "総額X円" must be checkable against the rows that composed it.
4. **No PII leak.** Personal info (phone, address, ID number) appears only if the intent justified it.
5. **Consistent units / currencies.**

Output: a punch list of issues to fix, ordered most-load-bearing first.
