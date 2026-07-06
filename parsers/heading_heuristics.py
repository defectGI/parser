"""Pure-text heading clues shared by format parsers that fake headings via
formatting (bold/caps/short line) rather than a dedicated style/placeholder.

These functions only look at plain text; nothing here touches a specific
format's object model. Each parser (docx, pptx, ...) pairs them with its own
scoring weights/threshold (docx: `docx_parser.HeadingConfig`, pptx:
`pptx_parser.PptxHeadingConfig`) since the available structural signals differ
per format — but "is this all-caps", "is this a date line", etc. should answer
the same way everywhere.
"""

from __future__ import annotations

import re

_NUM_PREFIX = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+\S")
_ROMAN_PREFIX = re.compile(r"^\s*([IVXLCDM]+)[.)]\s+\S")
_NAMED_SECTION = re.compile(
    r"^\s*(chapter|section|part|appendix|b[öo]l[üu]m|k[ıi]s[ıi]m|ek)\b", re.I)

# Figure/table captions are not headings even though they look like short bold lines.
CAPTION = re.compile(r"^\s*(figure|fig|table|gambar|tabel|foto|resim|[şs]ekil|tablo)\s*\d",
                     re.I)

# Date/time lines (meeting date, venue schedule) look like short bold lines but are
# never headings. Matched only on short lines to avoid catching real headings.
_MONTH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|ocak|[şs]ubat|mart|nisan|"
    r"may[ıi]s|haziran|temmuz|a[ğg]ustos|eyl[üu]l|ekim|kas[ıi]m|aral[ıi]k)", re.I)
_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(a\.?m\.?|p\.?m\.?|pm|am)?\b", re.I)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_ORDINAL = re.compile(r"\b\d{1,2}(st|nd|rd|th)\b", re.I)


def is_all_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def is_title_case(text: str) -> bool:
    """Mixed-case phrase where each significant (>3 char) word is capitalized —
    e.g. 'Wind Tunnel Experiment'. Excludes ALL-CAPS (scored separately)."""
    if is_all_caps(text):
        return False
    sig = [w for w in text.split() if len(w) > 3 and w[0].isalpha()]
    return len(sig) >= 2 and all(w[0].isupper() for w in sig)


def numbering_level(text: str) -> int | None:
    """Heading level implied by a leading number/section word, or None."""
    m = _NUM_PREFIX.match(text)
    if m:
        return min(m.group(1).count(".") + 1, 6)
    if _ROMAN_PREFIX.match(text) or _NAMED_SECTION.match(text):
        return 1
    return None


def looks_like_date(text: str) -> bool:
    t = text.strip()
    if not t or len(t.split()) > 8:
        return False
    has_month, has_time = bool(_MONTH.search(t)), bool(_TIME.search(t))
    has_year, has_ord = bool(_YEAR.search(t)), bool(_ORDINAL.search(t))
    return ((has_month and (has_year or has_ord))
            or (has_time and (has_year or has_month or len(t.split()) <= 3)))
