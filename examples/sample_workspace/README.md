# Sample workspace

A tiny corpus you can register as a workspace to smoke-test the Office Layer
without exposing real customer documents.

```bash
office-layer workspace add ~/Downloads/claude-code-office-layer/examples/sample_workspace --name samples
office-layer index <workspace_id>
office-layer search "請求"
office-layer search "contract" --workspace <workspace_id>
office-layer evidence "draft followup email" "ACME 請求"
```

Contents:
- `invoices/INV-2025-03.md` — fake invoice text (for keyword search)
- `invoices/invoice-suzuki-2026-02.md` — JP invoice w/ inline-colon labels (exercises the `label: value` strategy)
- `invoices/invoice-acme-en-2026-01.md` — English invoice (Bill To / Subtotal / Total)
- `invoices/invoice-tanaka-2026-03.xlsx` — JP invoice laid out as XLSX cells
- `invoices/receipt-coffee.txt` — tiny receipt
- `contracts/nda-old.md` — old NDA text
- `contracts/nda-new.md` — same NDA with one substantive change
- `notes/meeting-2025-04.md` — meeting minutes with action items

The markdown / txt files keep the smoke test runnable with nothing more than
the core install. The single XLSX is there so the Phase 2 invoice extractor
can be exercised against a real Office binary format too.

All names, amounts, and bank accounts are fabricated — no real PII.
