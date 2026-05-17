"""OCR adapters — all optional, off by default (§17.8).

Order of preference (when available):
1. Apple Vision on macOS (no install, no model download, fast)
2. Tesseract (cross-platform, free, decent quality)
3. OCRmyPDF (wraps Tesseract + Ghostscript, end-to-end PDF pipeline)
"""
