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
- `invoices/receipt-coffee.txt` — tiny receipt
- `contracts/nda-old.md` — old NDA text
- `contracts/nda-new.md` — same NDA with one substantive change
- `notes/meeting-2025-04.md` — meeting minutes with action items

The folder uses `.md` instead of real PDF/DOCX so the smoke test works with
nothing more than the core install.
