"""Selects the right parser by file extension / mimetype.

Single decision point for "which parser opens this file?". Adding a new format
means adding its class to `_PARSERS` here; nothing else in the pipeline changes.
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseParser
from .docx_parser import DocxParser
from .html_parser import HtmlParser
from .markdown_parser import MarkdownParser
from .pdf_parser import PdfParser
from .pptx_parser import PptxParser
from .xlsx_parser import XlsxParser

# Registered parsers, in priority order. Later formats get appended as they land.
_PARSERS: list[type[BaseParser]] = [
    MarkdownParser,
    HtmlParser,
    XlsxParser,
    DocxParser,
    PptxParser,
    PdfParser,
]


class UnsupportedFormatError(Exception):
    """Raised when no registered parser handles the given file."""


def register(parser_cls: type[BaseParser]) -> None:
    """Register an additional parser class (used to extend the registry)."""
    if parser_cls not in _PARSERS:
        _PARSERS.append(parser_cls)


def parser_for(path: str | Path, mimetype: str | None = None) -> BaseParser:
    """Return a parser instance for `path`, matched by extension then mimetype.

    Raises UnsupportedFormatError if nothing matches.
    """
    ext = Path(path).suffix.lower()
    for cls in _PARSERS:
        if cls.supports(extension=ext, mimetype=mimetype):
            return cls()
    raise UnsupportedFormatError(
        f"No parser for extension {ext!r}"
        + (f" / mimetype {mimetype!r}" if mimetype else "")
    )
