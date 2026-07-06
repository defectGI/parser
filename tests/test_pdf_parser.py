"""pdf parser tests: triage routing, code path, hybrid/scanned verification.

No network and no real models: PDFs are tiny hand-built byte streams, the VLM
and the text detector are fakes injected through PdfParser's constructor.
"""

from __future__ import annotations

import json

import pytest

from parsers.base import HeadingBlock, ImageBlock, ParagraphBlock, TableBlock
from parsers.pdf_parser import (
    PROV_CONSENSUS, PROV_TEXT_LAYER, PROV_UNVERIFIED,
    DetectedLine, PdfParser,
    _containment, _criticals, _find_gutter, _parse_vlm_blocks, _tok_list,
)


# ---------------------------------------------------------------------------
# Minimal PDF builders (no third-party writer; enough for pdfplumber/pdfium)
# ---------------------------------------------------------------------------


def _build_pdf(objs: list[bytes]) -> bytes:
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF").encode()
    return out


def _stream(head: bytes, data: bytes) -> bytes:
    return head + f" /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def text_pdf(lines: list[tuple[float, float, float, str]]) -> bytes:
    """One-page born-digital PDF. lines: (x, top_from_page_top, size, text)."""
    content = b""
    for x, top, size, text in lines:
        y = 792 - top - size  # PDF origin is bottom-left
        esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"BT /F1 {size} Tf {x} {y} Td ({esc}) Tj ET\n".encode("latin-1")
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(b"<<", content),
    ])


def scanned_pdf() -> bytes:
    """One-page PDF whose only content is a page-filling raster image."""
    w = h = 50
    pixels = bytes([180, 180, 180]) * (w * h)
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>",
        _stream(b"<< /Type /XObject /Subtype /Image /Width 50 /Height 50 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8", pixels),
        _stream(b"<<", b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
    ])


def scanned_pdf_with_subfigure() -> bytes:
    """Same page-filling scan, plus one distinct, non-page-spanning raster
    (e.g. a photo printed on the scanned page) drawn on top of it."""
    bg = bytes([180, 180, 180]) * (50 * 50)
    fg = bytes([90, 60, 30]) * (10 * 10)
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im1 4 0 R /Im2 5 0 R >> >> "
        b"/Contents 6 0 R >>",
        _stream(b"<< /Type /XObject /Subtype /Image /Width 50 /Height 50 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8", bg),
        _stream(b"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8", fg),
        _stream(b"<<", b"q 612 0 0 792 0 0 cm /Im1 Do Q\n"
                        b"q 100 0 0 80 250 350 cm /Im2 Do Q"),
    ])


def mixed_font_pdf(segments: list[tuple[str, float, float, str]]) -> bytes:
    """One-page PDF where each segment is (font, x, top, text), each drawn in
    its own absolute BT/ET block so multiple fonts can land on one visual
    line -- used to exercise font-based bold/italic run detection."""
    size = 11.0
    content = b""
    for font, x, top, text in segments:
        y = 792 - top - size
        esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"BT /{font} {size} Tf {x} {y} Td ({esc}) Tj ET\n".encode("latin-1")
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R /F2 6 0 R /F3 7 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(b"<<", content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>",
    ])


TWO_COL_LEFT = [f"left column line {i} alpha beta gamma" for i in range(1, 9)]
TWO_COL_RIGHT = [f"right column line {i} delta epsilon zeta" for i in range(1, 9)]


def two_column_pdf() -> bytes:
    lines = []
    for i, text in enumerate(TWO_COL_LEFT):
        lines.append((50.0, 100.0 + 20 * i, 10.0, text))
    for i, text in enumerate(TWO_COL_RIGHT):
        lines.append((340.0, 100.0 + 20 * i, 10.0, text))
    return lines and text_pdf(lines)


def two_column_with_image_pdf() -> bytes:
    """Same two-column layout, plus one real embedded raster near the top of
    the page (well above the text so it never overlaps a column)."""
    content = b""
    for x, top, size, text in (
        [(50.0, 100.0 + 20 * i, 10.0, t) for i, t in enumerate(TWO_COL_LEFT)]
        + [(340.0, 100.0 + 20 * i, 10.0, t) for i, t in enumerate(TWO_COL_RIGHT)]
    ):
        y = 792 - top - size
        esc = text_escape(text)
        content += f"BT /F1 {size} Tf {x} {y} Td ({esc}) Tj ET\n".encode("latin-1")
    content += b"q 100 0 0 30 250 750 cm /Im1 Do Q\n"
    pixels = bytes([200, 150, 100]) * (10 * 10)
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> /XObject << /Im1 5 0 R >> >> "
        b"/Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(b"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8", pixels),
        _stream(b"<<", content),
    ])


def two_page_pdf_mixed_heading_sizes() -> bytes:
    """Page 1 has a size-18 heading (the document's biggest) plus size-11
    body lines; page 2 has only a size-14 heading plus size-11 body lines --
    big enough to be the biggest thing on its own page, but smaller than page
    1's, so document-wide heading-level ranking must place it at level 2,
    not level 1 (which page-local ranking would give it)."""
    def page_content(heading_size: float, heading_text: str) -> bytes:
        content = b""
        y = 792 - 80 - heading_size
        content += (f"BT /F1 {heading_size} Tf 72 {y} Td "
                    f"({text_escape(heading_text)}) Tj ET\n").encode("latin-1")
        for i, body in enumerate(["body line one here",
                                  "body line two here",
                                  "body line three here"]):
            top = 140.0 + 20 * i
            y = 792 - top - 11
            content += (f"BT /F1 11 Tf 72 {y} Td "
                        f"({text_escape(body)}) Tj ET\n").encode("latin-1")
        return content

    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 6 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 6 0 R >> >> /Contents 7 0 R >>",
        _stream(b"<<", page_content(18.0, "Big Heading")),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(b"<<", page_content(14.0, "Small Heading")),
    ])


def two_column_pdf_with_bold_word() -> bytes:
    """Same two-column layout as two_column_pdf(), but "delta" in the first
    right-column line is rendered in Helvetica-Bold -- exercises hybrid-path
    run backfill against the page's own text layer. Kept clear of the left
    column's max extent so gutter detection is unaffected."""
    content = b""
    size = 10.0
    for i, text in enumerate(TWO_COL_LEFT):
        top = 100.0 + 20 * i
        y = 792 - top - size
        esc = text_escape(text)
        content += f"BT /F1 {size} Tf 50 {y} Td ({esc}) Tj ET\n".encode("latin-1")
    y0 = 792 - 100.0 - size
    for font, x, text in (
        ("F1", 340.0, "right column line 1"),
        ("F2", 460.0, "delta"),
        ("F1", 500.0, "epsilon zeta"),
    ):
        esc = text_escape(text)
        content += f"BT /{font} {size} Tf {x} {y0} Td ({esc}) Tj ET\n".encode("latin-1")
    for i, text in enumerate(TWO_COL_RIGHT[1:], start=1):
        top = 100.0 + 20 * i
        y = 792 - top - size
        esc = text_escape(text)
        content += f"BT /F1 {size} Tf 340 {y} Td ({esc}) Tj ET\n".encode("latin-1")
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R /F2 6 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(b"<<", content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ])


def text_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVLM:
    """Returns a canned payload; records how it was called."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete_vision(self, *, system, user, images, max_tokens=2048):
        self.calls.append({"system": system, "user": user,
                           "n_images": len(images)})
        return self.payload


class FakeDetector:
    def __init__(self, lines: list[DetectedLine]) -> None:
        self.lines = lines

    def detect(self, png: bytes) -> list[DetectedLine]:
        return self.lines


def offline_parser(**kw) -> PdfParser:
    """PdfParser with every model/detector explicitly disabled unless given."""
    kw.setdefault("vlm", None)
    kw.setdefault("vlm2", None)
    kw.setdefault("detector", None)
    return PdfParser(**kw)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_containment_and_criticals():
    hay = _tok_list("Part 99-1234 ships on 2026-07-04 for 500 USD.")
    from collections import Counter
    assert _containment(_tok_list("ships on 2026-07-04"), Counter(hay)) == 1.0
    assert _containment(_tok_list("totally absent words"), Counter(hay)) == 0.0
    crits = _criticals("Part 99-1234 for 500 USD")
    assert "99-1234" in crits and "500" in crits and "part" not in crits


def test_tok_list_drops_pure_symbol_tokens():
    # Two independent transcriptions of the same formula, differing only in
    # operator glyphs (→ vs ->, − vs -): symbol-only tokens must not count as
    # content, or short formula lines spuriously fail containment even though
    # every letter/digit agrees (see corpus_pdf/LecNotes.pdf page 13).
    from collections import Counter
    a = _tok_list("w = (24 − 11) / 5 = 2.6, always rounding up → w = 3")
    b = _tok_list("w = (24 - 11) / 5 = 2.6, always rounding up -> w = 3")
    assert "−" not in a and "→" not in a and "=" not in a and "/" not in a
    assert _containment(a, Counter(b)) == 1.0


def test_find_gutter_two_columns():
    words = []
    for row in range(20):
        for x in (40, 80, 120, 160):
            words.append({"x0": x, "x1": x + 30, "top": row * 12,
                          "bottom": row * 12 + 10})
        for x in (340, 380, 420, 460):
            words.append({"x0": x, "x1": x + 30, "top": row * 12,
                          "bottom": row * 12 + 10})
    gx = _find_gutter(words, 612.0)
    assert gx is not None and 200 < gx < 340


def test_find_gutter_single_column():
    words = [{"x0": 50 + (i % 8) * 60, "x1": 50 + (i % 8) * 60 + 50,
              "top": (i // 8) * 12, "bottom": (i // 8) * 12 + 10}
             for i in range(80)]
    assert _find_gutter(words, 612.0) is None


def test_parse_vlm_blocks_fenced_and_invalid():
    payload = [{"type": "heading", "level": 2, "text": "Title"},
               {"type": "paragraph", "text": "Body"},
               {"type": "table", "rows": [["a", "b"], ["c", None]]},
               {"type": "nonsense", "text": "x"}]
    raw = "```json\n" + json.dumps(payload) + "\n```"
    specs = _parse_vlm_blocks(raw)
    assert [s["type"] for s in specs] == ["heading", "paragraph", "table"]
    assert specs[2]["rows"][1] == ["c", ""]
    assert _parse_vlm_blocks("no json here") is None
    assert _parse_vlm_blocks("") is None


# ---------------------------------------------------------------------------
# Code path
# ---------------------------------------------------------------------------


def test_code_path_heading_paragraph_list(tmp_path):
    pdf = text_pdf([
        (72, 80, 18, "Document Title"),
        (72, 130, 11, "This is the first body paragraph of the document."),
        (72, 145, 11, "It continues on a second wrapped line here."),
        (72, 180, 11, "- first bullet item"),
        (72, 195, 11, "- second bullet item"),
    ])
    path = tmp_path / "simple.pdf"
    path.write_bytes(pdf)

    doc = offline_parser().parse(path, "doc1")

    assert doc.fmt == "pdf" and doc.page_count == 1
    assert doc.metadata["pdf_pages"][0]["route"] == "code"

    headings = [b for b in doc.blocks if isinstance(b, HeadingBlock)]
    assert len(headings) == 1
    assert headings[0].text == "Document Title" and headings[0].level == 1
    assert headings[0].provenance == PROV_TEXT_LAYER
    assert headings[0].span.page == 1

    items = [b for b in doc.blocks if b.list_id is not None]
    assert len(items) == 2
    assert items[0].text == "first bullet item"
    assert items[0].list_id == items[1].list_id
    assert items[0].list_ordered is False

    paras = [b for b in doc.blocks
             if isinstance(b, ParagraphBlock) and b.list_id is None]
    assert len(paras) == 1  # the two wrapped lines merged into one paragraph
    assert "second wrapped line" in paras[0].text

    # ids are sequential in reading order
    assert [b.id for b in doc.blocks] == [f"b{i}" for i in range(len(doc.blocks))]


def test_code_path_bold_italic_runs_from_font_name(tmp_path):
    # F1=Helvetica (plain), F2=Helvetica-Bold, F3=Helvetica-Oblique, all on
    # one visual line -- deterministic from the PDF's own font names, same
    # idea as reading a docx run's rPr.
    pdf = mixed_font_pdf([
        ("F1", 72, 100, "plain"),
        ("F2", 140, 100, "boldword"),
        ("F3", 230, 100, "italicword"),
    ])
    path = tmp_path / "mixed.pdf"
    path.write_bytes(pdf)

    doc = offline_parser().parse(path, "doc_runs")
    assert doc.metadata["pdf_pages"][0]["route"] == "code"

    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert len(paras) == 1
    para = paras[0]
    assert para.text == "plain boldword italicword"
    assert "".join(r.text for r in para.runs) == para.text

    from parsers.base import Mark
    marks_by_word = {r.text.strip(): set(r.marks) for r in para.runs}
    assert marks_by_word["plain"] == set()
    assert marks_by_word["boldword"] == {Mark.BOLD}
    assert marks_by_word["italicword"] == {Mark.ITALIC}


def test_code_path_heading_level_ranked_document_wide(tmp_path):
    # Page 2's only heading (size 14) is locally the biggest thing on its own
    # page -- page-local ranking would call it level 1 -- but page 1 has a
    # bigger heading (size 18), so document-wide ranking must give page 2's
    # heading level 2.
    path = tmp_path / "twopage.pdf"
    path.write_bytes(two_page_pdf_mixed_heading_sizes())

    doc = offline_parser().parse(path, "doc_headings")
    assert doc.page_count == 2
    assert doc.metadata["pdf_pages"][0]["route"] == "code"
    assert doc.metadata["pdf_pages"][1]["route"] == "code"

    headings = [b for b in doc.blocks if isinstance(b, HeadingBlock)]
    assert len(headings) == 2
    big = next(h for h in headings if h.span.page == 1)
    small = next(h for h in headings if h.span.page == 2)
    assert big.text == "Big Heading" and big.level == 1
    assert small.text == "Small Heading" and small.level == 2


def test_multicolumn_without_vlm_falls_back_to_code(tmp_path):
    path = tmp_path / "twocol.pdf"
    path.write_bytes(two_column_pdf())

    doc = offline_parser().parse(path, "doc2")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["route"] == "hybrid" and meta["used"] == "code"
    texts = [b.text for b in doc.blocks if isinstance(b, ParagraphBlock)]
    joined = " ".join(texts)
    # left column content must come before right column content
    assert joined.index("left column line 1") < joined.index("right column line 1")
    assert all(b.provenance == PROV_TEXT_LAYER for b in doc.blocks)


# ---------------------------------------------------------------------------
# Hybrid path
# ---------------------------------------------------------------------------


def _hybrid_payload(extra: list[dict] | None = None) -> str:
    blocks = [{"type": "paragraph", "text": t} for t in TWO_COL_LEFT]
    blocks += [{"type": "paragraph", "text": t} for t in TWO_COL_RIGHT]
    return json.dumps(blocks + (extra or []))


def test_hybrid_verified_against_text_layer(tmp_path):
    path = tmp_path / "twocol.pdf"
    path.write_bytes(two_column_pdf())

    vlm = FakeVLM(_hybrid_payload())
    doc = offline_parser(vlm=vlm).parse(path, "doc3")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["route"] == "hybrid" and meta["used"] == "hybrid"
    assert len(vlm.calls) == 1 and vlm.calls[0]["n_images"] == 1
    assert "TEXT LAYER:" in vlm.calls[0]["user"]  # grounding was passed

    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert len(paras) == 16
    assert all(b.provenance == PROV_TEXT_LAYER for b in paras)
    assert all(b.source_crop is None for b in paras)


def test_hybrid_hallucination_is_unverified_with_crop(tmp_path, monkeypatch):
    crop_dir = tmp_path / "crops"
    monkeypatch.setenv("PDF_CROP_DIR", str(crop_dir))
    path = tmp_path / "twocol.pdf"
    path.write_bytes(two_column_pdf())

    fake = {"type": "paragraph", "text": "Part 77-9999 costs 500 USD"}
    vlm = FakeVLM(_hybrid_payload([fake]))
    doc = offline_parser(vlm=vlm).parse(path, "doc4")

    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    bad = [b for b in paras if "77-9999" in b.text]
    assert len(bad) == 1
    assert bad[0].provenance == PROV_UNVERIFIED
    assert bad[0].source_crop is not None
    assert (crop_dir / f"{bad[0].source_crop}.png").exists()
    good = [b for b in paras if "77-9999" not in b.text]
    assert all(b.provenance == PROV_TEXT_LAYER for b in good)


def test_hybrid_bold_run_backfilled_from_text_layer(tmp_path):
    # "delta" is Helvetica-Bold in the PDF's own text layer; the VLM's
    # transcription carries no font info at all, but its wording matches the
    # text layer verbatim (PROV_TEXT_LAYER), so runs are backfilled from the
    # same deterministic font-name signal the code path reads directly.
    path = tmp_path / "twocol_bold.pdf"
    path.write_bytes(two_column_pdf_with_bold_word())

    vlm = FakeVLM(_hybrid_payload())
    doc = offline_parser(vlm=vlm).parse(path, "doc_hybrid_runs")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["route"] == "hybrid" and meta["used"] == "hybrid"

    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    target = next(b for b in paras if b.text == TWO_COL_RIGHT[0])
    assert target.provenance == PROV_TEXT_LAYER
    assert "".join(r.text for r in target.runs) == target.text

    from parsers.base import Mark
    bold_runs = [r for r in target.runs if Mark.BOLD in r.marks]
    assert len(bold_runs) == 1 and bold_runs[0].text.strip() == "delta"

    # Untouched paragraphs (including ones after "delta" in reading order)
    # still align cleanly -- the shared pointer never rewound past them.
    other = next(b for b in paras if b.text == TWO_COL_RIGHT[1])
    assert other.runs == []


def test_hybrid_figure_unpaired_is_unverified_no_crop(tmp_path, monkeypatch):
    # No embedded raster in this PDF at all: a VLM-claimed figure has nothing
    # to pair with geometrically, so it must still surface as an ImageBlock,
    # flagged unverified (never silently dropped). With no bbox guess either,
    # a full-page dump wouldn't be a useful crop, so none is stored.
    crop_dir = tmp_path / "crops"
    monkeypatch.setenv("PDF_CROP_DIR", str(crop_dir))
    path = tmp_path / "twocol.pdf"
    path.write_bytes(two_column_pdf())

    payload = json.dumps([{"type": "figure"}] +
                          [{"type": "paragraph", "text": t} for t in TWO_COL_LEFT] +
                          [{"type": "paragraph", "text": t} for t in TWO_COL_RIGHT])
    vlm = FakeVLM(payload)
    doc = offline_parser(vlm=vlm).parse(path, "doc10")

    images = [b for b in doc.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    assert "bbox" not in images[0].locator
    assert images[0].locator["region"] == "vlm_figure"
    assert images[0].provenance == PROV_UNVERIFIED
    assert images[0].source_crop is None
    # the figure spec was first in the VLM's array -> first block in the doc
    assert doc.blocks[0] is images[0]


def test_hybrid_figure_paired_with_geometry_keeps_position(tmp_path):
    # A real embedded raster this time: the VLM's single figure spec should
    # pair positionally with it, giving a real bbox instead of a bare guess.
    path = tmp_path / "twocol_img.pdf"
    path.write_bytes(two_column_with_image_pdf())

    payload = json.dumps([{"type": "figure"}] +
                          [{"type": "paragraph", "text": t} for t in TWO_COL_LEFT] +
                          [{"type": "paragraph", "text": t} for t in TWO_COL_RIGHT])
    vlm = FakeVLM(payload)
    doc = offline_parser(vlm=vlm).parse(path, "doc11")

    images = [b for b in doc.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    assert "bbox" in images[0].locator
    assert images[0].provenance is None  # deterministic, same as the code path
    assert images[0].source_crop is None
    assert doc.blocks[0] is images[0]


def test_hybrid_image_not_mentioned_by_vlm_still_appended(tmp_path):
    # A real embedded raster the VLM's payload never calls out as a figure
    # (it under-counted): the image must still be emitted, just without a
    # reading-order position -- never silently lost.
    path = tmp_path / "twocol_img.pdf"
    path.write_bytes(two_column_with_image_pdf())

    vlm = FakeVLM(_hybrid_payload())  # no "figure" spec at all
    doc = offline_parser(vlm=vlm).parse(path, "doc12")

    images = [b for b in doc.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    assert "bbox" in images[0].locator
    assert images[0].provenance is None
    # appended after the VLM-driven text flow, so it lands last
    assert doc.blocks[-1] is images[0]


def test_hybrid_bad_vlm_json_falls_back_to_code(tmp_path):
    path = tmp_path / "twocol.pdf"
    path.write_bytes(two_column_pdf())

    vlm = FakeVLM("I refuse to answer in JSON.")
    doc = offline_parser(vlm=vlm).parse(path, "doc5")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["used"] == "code" and "fallback" in meta["note"]
    assert len(vlm.calls) == 2  # one retry with the stricter reminder
    assert any(isinstance(b, ParagraphBlock) for b in doc.blocks)
    assert all(b.provenance == PROV_TEXT_LAYER for b in doc.blocks)


# ---------------------------------------------------------------------------
# Scanned path
# ---------------------------------------------------------------------------


def test_scanned_routes_and_consensus(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf())

    vlm = FakeVLM(json.dumps([
        {"type": "paragraph", "text": "Hello scanned world"}]))
    det = FakeDetector([DetectedLine("Hello scanned world", (10, 10, 500, 40))])
    doc = offline_parser(vlm=vlm, detector=det).parse(path, "doc6")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["route"] == "scanned" and meta["used"] == "scanned"
    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert len(paras) == 1
    assert paras[0].text == "Hello scanned world"
    assert paras[0].provenance == PROV_CONSENSUS


def test_scanned_detector_disagreement_unverified(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_CROP_DIR", str(tmp_path / "crops"))
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf())

    vlm = FakeVLM(json.dumps([
        {"type": "paragraph", "text": "Invoice total 500 USD"}]))
    det = FakeDetector([DetectedLine("Invoice total 600 USD", (10, 10, 500, 40))])
    doc = offline_parser(vlm=vlm, detector=det).parse(path, "doc7")

    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert len(paras) == 1
    # 500 vs 600: critical tokens disagree, no second model -> stays unverified
    assert paras[0].provenance == PROV_UNVERIFIED
    assert paras[0].source_crop is not None


def test_scanned_second_vlm_rescues_disputed_line(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf())

    vlm = FakeVLM(json.dumps([
        {"type": "paragraph", "text": "Invoice total 500 USD"}]))
    det = FakeDetector([DetectedLine("Invoice total 600 USD", (10, 10, 500, 40))])
    vlm2 = FakeVLM("Invoice total 500 USD")  # independent reread agrees w/ VLM1
    doc = offline_parser(vlm=vlm, detector=det, vlm2=vlm2).parse(path, "doc8")

    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert paras[0].provenance == PROV_CONSENSUS
    assert len(vlm2.calls) == 1  # the crop-and-reread call


def test_scanned_figure_over_background_scan_is_unverified_no_crop(tmp_path, monkeypatch):
    # scanned_pdf()'s only raster IS the page-spanning scan itself -- it must
    # be excluded from geometric pairing, so a VLM-claimed figure here has
    # nothing to pair with and surfaces as unverified. With no bbox guess
    # either, a full-page dump wouldn't be a useful crop, so none is stored.
    crop_dir = tmp_path / "crops"
    monkeypatch.setenv("PDF_CROP_DIR", str(crop_dir))
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf())

    vlm = FakeVLM(json.dumps([{"type": "figure"}]))
    doc = offline_parser(vlm=vlm).parse(path, "doc9b")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["used"] == "scanned"
    assert len(doc.blocks) == 1
    img = doc.blocks[0]
    assert isinstance(img, ImageBlock)
    assert "bbox" not in img.locator
    assert img.locator["region"] == "vlm_figure"
    assert img.provenance == PROV_UNVERIFIED
    assert img.source_crop is None


def test_scanned_figure_with_vlm_bbox_gets_tight_crop(tmp_path, monkeypatch):
    # Same page-spanning-scan situation as the test above, but this time the
    # VLM volunteers its own (ungrounded) bbox for the sub-figure. It still
    # can't be geometrically confirmed, so it stays unverified -- but since
    # there's a bbox guess to work with, a tight audit crop is stored (unlike
    # the no-bbox case, which stores none rather than a full-page dump).
    crop_dir = tmp_path / "crops"
    monkeypatch.setenv("PDF_CROP_DIR", str(crop_dir))
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf())

    vlm = FakeVLM(json.dumps(
        [{"type": "figure", "bbox": [0.1, 0.1, 0.4, 0.3]}]))
    doc = offline_parser(vlm=vlm).parse(path, "doc9d")

    img = doc.blocks[0]
    assert isinstance(img, ImageBlock)
    assert img.provenance == PROV_UNVERIFIED
    assert "vlm_bbox" not in img.locator  # consumed, not left dangling
    assert img.source_crop is not None
    assert (crop_dir / f"{img.source_crop}.png").exists()


def test_scanned_figure_paired_with_real_subfigure_keeps_bbox(tmp_path):
    # A distinct, non-page-spanning embedded raster alongside the scan
    # background: the VLM's figure spec should pair with it (not the
    # background) and get a real geometric bbox, deterministic provenance.
    path = tmp_path / "scan_sub.pdf"
    path.write_bytes(scanned_pdf_with_subfigure())

    vlm = FakeVLM(json.dumps([{"type": "figure"},
                              {"type": "paragraph", "text": "Caption text"}]))
    det = FakeDetector([DetectedLine("Caption text", (10, 400, 300, 430))])
    doc = offline_parser(vlm=vlm, detector=det).parse(path, "doc9c")

    images = [b for b in doc.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    assert "bbox" in images[0].locator
    assert images[0].provenance is None
    assert images[0].source_crop is None


def test_scanned_without_vlm_keeps_page_as_image(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf())

    doc = offline_parser().parse(path, "doc9")

    meta = doc.metadata["pdf_pages"][0]
    assert meta["route"] == "scanned" and meta["used"] == "image-fallback"
    assert len(doc.blocks) == 1
    img = doc.blocks[0]
    assert isinstance(img, ImageBlock)
    assert img.locator["region"] == "full_page"
    assert img.locator["page"] == 1


# ---------------------------------------------------------------------------
# IR round-trip with provenance
# ---------------------------------------------------------------------------


def test_provenance_serializes_and_roundtrips(tmp_path):
    from parsers.base import ParsedDocument

    pdf = text_pdf([(72, 80, 12, "hello world roundtrip")])
    path = tmp_path / "rt.pdf"
    path.write_bytes(pdf)

    doc = offline_parser().parse(path, "rt")
    again = ParsedDocument.from_json(doc.to_json())
    assert again.blocks[0].provenance == PROV_TEXT_LAYER
    assert again.blocks[0].source_crop is None
    d = doc.blocks[0].to_dict()
    assert d["provenance"] == PROV_TEXT_LAYER and "source_crop" not in d


def test_registry_selects_pdf(tmp_path):
    from parsers.registry import parser_for

    assert isinstance(parser_for("x.pdf"), PdfParser)
