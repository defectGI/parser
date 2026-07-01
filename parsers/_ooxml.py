"""Shared helpers for OOXML (docx/pptx/xlsx) parsers.

Best-effort byte offsets (Karar B): OOXML is a zip of XML parts. We keep byte
offsets *within the relevant XML part* (Span.part names the stream) when we can
obtain them reliably, and leave them None otherwise. `element_byte_range` uses
stdlib expat's `CurrentByteIndex` to locate an element's byte span inside a part.
"""

from __future__ import annotations

from xml.parsers import expat


def element_byte_range(xml: bytes, tag: str) -> tuple[int, int] | None:
    """Byte range [start, end) of the first `<tag>` element in `xml`.

    `tag` is the local element name (expat runs without namespace processing, so
    e.g. "sheetData" matches regardless of the default namespace). Returns None if
    the element is absent or the XML cannot be parsed — the caller then leaves the
    Span's byte offsets unset.
    """
    found: dict[str, int] = {}
    parser = expat.ParserCreate()

    def on_start(name: str, _attrs) -> None:
        if name == tag and "start" not in found:
            found["start"] = parser.CurrentByteIndex

    def on_end(name: str) -> None:
        if name == tag and "start" in found and "end" not in found:
            found["end"] = parser.CurrentByteIndex

    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    try:
        parser.Parse(xml, True)
    except expat.ExpatError:
        return None
    if "start" in found and "end" in found:
        return found["start"], found["end"]
    return None
