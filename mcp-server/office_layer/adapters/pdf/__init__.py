"""PDF extraction adapters.

Per spec §17.3 priority:
1. ``pypdfium2`` (BSD/Apache, fast text only)
2. ``pdfplumber`` (MIT, slower but extracts tables)
3. ``pymupdf`` (AGPL/commercial — opt-in via ``OFFICE_LAYER_PDF=pymupdf``)
"""
