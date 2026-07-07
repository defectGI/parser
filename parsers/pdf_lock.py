"""Process-wide lock serializing all pdfplumber/pypdfium2 access.

PDFium (the native library behind pdfplumber) is not safe to call from
multiple threads at once -- concurrent `pdfplumber.open()` / page-render
calls corrupt its internal state, surfacing as spurious "PDFium: Data format
error" or access-violation crashes. Every call site that opens a PDF via
pdfplumber (parsers/pdf_parser.py's own parse(), images/image_handler.py's
PDF image fetcher) must hold this lock for the duration of that PDFium work,
so DOC_CONCURRENCY/IMAGE_CONCURRENCY can run multiple documents/images at
once without two of them touching PDFium at the same instant.
"""

from __future__ import annotations

import threading

PDFIUM_LOCK = threading.Lock()
