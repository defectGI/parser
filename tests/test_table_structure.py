"""tables/structure/ tests: the neutral protocol, the HTTP adapter's JSON
contract, PyMuPDF text-by-bbox extraction, and pdf_parser.py's wiring of a
configured structure model into the code path.

No network and no real TableFormer: a FakeTableStructureClient is injected
through PdfParser's constructor (same pattern test_pdf_parser.py uses for
VLM/detector fakes), and the HTTP adapter is tested by monkeypatching
urllib.request.urlopen.
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest

import pdfplumber

from parsers.base import TableBlock
from parsers.pdf_parser import (
    PROV_TABLE_STRUCTURE, PROV_TEXT_LAYER,
    PdfParser, _as_table_data, _digital_text_in_bbox, _PreparedTable,
)
from tables.structure import DetectedCell, DetectedTable, TextCellHint
from tables.structure.http_client import HttpTableStructureClient


# ---------------------------------------------------------------------------
# Minimal one-page born-digital PDF builder (same low-level shape as
# test_pdf_parser.py/test_image_handler.py -- no third-party writer needed)
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


def offline_parser(**kw) -> PdfParser:
    kw.setdefault("vlm", None)
    kw.setdefault("vlm2", None)
    kw.setdefault("detector", None)
    kw.setdefault("table_struct", None)
    return PdfParser(**kw)


# ---------------------------------------------------------------------------
# _digital_text_in_bbox
# ---------------------------------------------------------------------------


def test_digital_text_in_bbox_extracts_exact_text(tmp_path):
    pdf = text_pdf([(72, 100, 12, "Power Supply Current")])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    # top=100, size=12 -> text baseline sits in y in [792-112, 792-100] = [680, 692]
    # PyMuPDF's coordinate origin is top-left, same convention as `top`.
    text = _digital_text_in_bbox(path, 1, (60.0, 95.0, 300.0, 118.0))
    assert text == "Power Supply Current"


def test_digital_text_in_bbox_empty_region_yields_empty_string(tmp_path):
    pdf = text_pdf([(72, 100, 12, "Some text")])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    text = _digital_text_in_bbox(path, 1, (400.0, 400.0, 450.0, 420.0))
    assert text == ""


def test_digital_text_in_bbox_missing_file_never_raises(tmp_path):
    text = _digital_text_in_bbox(tmp_path / "nope.pdf", 1, (0, 0, 10, 10))
    assert text == ""


# ---------------------------------------------------------------------------
# _as_table_data / _PreparedTable
# ---------------------------------------------------------------------------


def test_as_table_data_passes_prepared_table_through():
    from parsers.base import Cell, ParagraphBlock, TableData

    data = TableData(n_rows=1, n_cols=1,
                     cells=[[Cell(blocks=[ParagraphBlock(id="p0", text="x")])]])
    prepared = _PreparedTable(bbox=(0, 0, 10, 10), data=data)
    assert _as_table_data(prepared) is data


# ---------------------------------------------------------------------------
# Structure-model refinement: _structure_model_tables refines pdfplumber's
# found REGIONS (geom_tables) rather than discovering tables itself -- so
# these call it directly with a stub geom_tables list (a bare object with
# just `.bbox`, all _structure_model_tables ever reads off it) instead of
# needing a synthetic PDF with real ruling lines for find_tables() to find.
# ---------------------------------------------------------------------------


class _StubGeomTable:
    """Minimal stand-in for a pdfplumber Table -- only `.bbox` is read."""

    def __init__(self, bbox: tuple[float, float, float, float]) -> None:
        self.bbox = bbox


class FakeTableStructureClient:
    """Returns pre-baked geometry; deliberately claims implausible cell text
    is irrelevant -- the point of the design is that pdf_parser.py ignores
    whatever "text" the model might imply and pulls the real digital text
    itself, so this fake never even offers a text field."""

    def __init__(self, tables: list[DetectedTable]) -> None:
        self._tables = tables
        self.calls: list[tuple[list, list[TextCellHint]]] = []

    def detect(self, image: bytes, *, table_bboxes=(), text_cells=()) -> list[DetectedTable]:
        self.calls.append((list(table_bboxes), list(text_cells)))
        return self._tables


def test_structure_model_table_uses_digital_text(tmp_path):
    # Two cells stacked vertically, matching two text_pdf lines exactly.
    pdf = text_pdf([
        (72, 100, 12, "Spec"),
        (72, 120, 12, "42"),
    ])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    dpi = 150
    to_px = dpi / 72.0

    def px(bbox_pt):
        return tuple(v * to_px for v in bbox_pt)

    region_bbox_pt = (60, 95, 300, 140)
    detected = DetectedTable(
        bbox=px(region_bbox_pt),
        cells=[
            DetectedCell(row=0, col=0, bbox=px((60, 95, 300, 118))),
            DetectedCell(row=1, col=0, bbox=px((60, 115, 300, 140))),
        ],
    )
    fake = FakeTableStructureClient([detected])
    parser = offline_parser(table_struct=fake)

    with pdfplumber.open(path) as pdf_doc:
        page = pdf_doc.pages[0]
        png = parser._render(page)
        # _StubGeomTable.bbox mirrors a real pdfplumber Table.bbox: PDF POINT
        # space. _structure_model_tables converts it to pixels itself before
        # calling detect(), same as it does for a real Table's .bbox.
        geom_tables = [_StubGeomTable(region_bbox_pt)]
        result = parser._structure_model_tables(page, 1, path, png, geom_tables)

    assert result is not None
    assert len(result) == 1
    table = result[0]
    assert table.data.n_rows == 2 and table.data.n_cols == 1
    assert table.data.cells[0][0].plain_text() == "Spec"
    assert table.data.cells[1][0].plain_text() == "42"

    # the fake was handed the region bbox converted to pixel space, and real
    # digital text cells to match against -- not just a bare image
    assert fake.calls, "detect() was never called"
    called_bboxes, called_hints = fake.calls[0]
    assert called_bboxes == [px(region_bbox_pt)]
    hint_texts = {h.text for h in called_hints}
    assert "Spec" in hint_texts and "42" in hint_texts


def test_structure_model_rowspan_becomes_merge(tmp_path):
    pdf = text_pdf([(72, 100, 12, "Merged")])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    dpi = 150
    to_px = dpi / 72.0

    def px(bbox_pt):
        return tuple(v * to_px for v in bbox_pt)

    region_bbox_pt = (60, 95, 300, 140)
    detected = DetectedTable(
        bbox=px(region_bbox_pt),
        cells=[
            DetectedCell(row=0, col=0, rowspan=2, colspan=1, bbox=px(region_bbox_pt)),
        ],
    )
    fake = FakeTableStructureClient([detected])
    parser = offline_parser(table_struct=fake)

    with pdfplumber.open(path) as pdf_doc:
        page = pdf_doc.pages[0]
        png = parser._render(page)
        geom_tables = [_StubGeomTable(region_bbox_pt)]  # PDF point space
        result = parser._structure_model_tables(page, 1, path, png, geom_tables)

    table = result[0]
    assert table.data.n_rows == 2
    assert len(table.data.merges) == 1
    m = table.data.merges[0]
    assert (m.row, m.col, m.rowspan, m.colspan) == (0, 0, 2, 1)
    assert table.data.cells[1][0] is None  # covered by the merge


def test_structure_model_unconfigured_returns_none(tmp_path):
    pdf = text_pdf([(72, 100, 12, "No structure model here")])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    parser = offline_parser()  # table_struct=None by default
    assert parser._table_structure() is None

    with pdfplumber.open(path) as pdf_doc:
        page = pdf_doc.pages[0]
        png = parser._render(page)
        result = parser._structure_model_tables(
            page, 1, path, png, [_StubGeomTable((0, 0, 10, 10))])
    assert result is None


def test_structure_model_error_falls_back_gracefully(tmp_path):
    from tables.structure import TableStructureError

    class FailingClient:
        def detect(self, image, *, table_bboxes=(), text_cells=()):
            raise TableStructureError("model unavailable")

    pdf = text_pdf([(72, 100, 12, "Plain text, no table")])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    parser = offline_parser(table_struct=FailingClient())
    with pdfplumber.open(path) as pdf_doc:
        page = pdf_doc.pages[0]
        png = parser._render(page)
        result = parser._structure_model_tables(
            page, 1, path, png, [_StubGeomTable((0, 0, 10, 10))])
    assert result is None  # caller falls back to geom_tables unrefined

    # And the full parse() must still succeed end to end despite the failure.
    doc = parser.parse(path, "doc1")
    assert doc.page_count == 1


def test_parse_code_tags_prepared_table_with_structure_provenance(tmp_path):
    from parsers.base import Cell, ParagraphBlock, TableData
    from parsers.pdf_parser import _Counters

    pdf = text_pdf([(72, 100, 12, "unrelated paragraph")])
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)

    data = TableData(n_rows=1, n_cols=1,
                     cells=[[Cell(blocks=[ParagraphBlock(id="p0", text="x")])]])
    prepared = _PreparedTable(bbox=(0.0, 0.0, 10.0, 10.0), data=data)

    parser = offline_parser()
    with pdfplumber.open(path) as pdf_doc:
        page = pdf_doc.pages[0]
        blocks = parser._parse_code(
            page, pageno=1,
            prep={"tables": [prepared], "gutter": None, "words": []},
            ct=_Counters(),
        )

    tables = [b for b in blocks if isinstance(b, TableBlock)]
    assert len(tables) == 1
    assert tables[0].provenance == PROV_TABLE_STRUCTURE
    assert tables[0].table is data


# ---------------------------------------------------------------------------
# HttpTableStructureClient
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_client_parses_tables_and_sends_text_cells(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse({
            "tables": [{
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "cells": [
                    {"row": 0, "col": 0, "bbox": [1.0, 2.0, 2.0, 3.0]},
                    {"row": 0, "col": 1, "rowspan": 1, "colspan": 2,
                     "bbox": [2.0, 2.0, 3.0, 3.0]},
                ],
            }],
        })

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = HttpTableStructureClient(base_url="http://example.test/detect")
    result = client.detect(b"fake-png-bytes",
                           table_bboxes=[(5.0, 6.0, 7.0, 8.0)],
                           text_cells=[TextCellHint(text="hi", bbox=(0, 0, 1, 1))])

    assert len(result) == 1
    assert result[0].bbox == (1.0, 2.0, 3.0, 4.0)
    assert len(result[0].cells) == 2
    assert result[0].cells[1].colspan == 2

    assert captured["body"]["text_cells"] == [{"text": "hi", "bbox": [0, 0, 1, 1]}]
    assert captured["body"]["table_bboxes"] == [[5.0, 6.0, 7.0, 8.0]]


def test_http_client_bad_shape_raises_table_structure_error(monkeypatch):
    from tables.structure import TableStructureError

    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse({"unexpected": "shape"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = HttpTableStructureClient(base_url="http://example.test/detect")
    with pytest.raises(TableStructureError):
        client.detect(b"fake-png-bytes")


# ---------------------------------------------------------------------------
# VLMStructureAdapter -- reuses an existing VLMClient, no extra dependency
# ---------------------------------------------------------------------------


class _FakeVLM:
    """Stands in for llm.VLMClient -- records what it was asked and returns
    a canned JSON response per call (one per table_bbox, in order)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def complete_vision(self, *, system, user, images, max_tokens=4096):
        self.calls.append({"system": system, "user": user,
                           "n_images": len(images), "max_tokens": max_tokens})
        return self._responses[len(self.calls) - 1]


def _make_page_png(width=200, height=100) -> bytes:
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_vlm_adapter_converts_normalized_bbox_to_page_pixels():
    from tables.structure.vlm_adapter import VLMStructureAdapter

    response = json.dumps([
        {"row": 0, "col": 0, "bbox": [0.0, 0.0, 0.5, 1.0]},
        {"row": 0, "col": 1, "bbox": [0.5, 0.0, 1.0, 1.0]},
    ])
    fake = _FakeVLM([response])
    adapter = VLMStructureAdapter(vlm_client=fake)

    png = _make_page_png(200, 100)
    # region [50, 20, 150, 80] -> a 100x60 crop
    result = adapter.detect(png, table_bboxes=[(50.0, 20.0, 150.0, 80.0)])

    assert len(result) == 1
    table = result[0]
    assert table.bbox == (50.0, 20.0, 150.0, 80.0)
    assert len(table.cells) == 2

    left, right = table.cells
    assert (left.row, left.col) == (0, 0)
    assert left.bbox == pytest.approx((50.0, 20.0, 100.0, 80.0))
    assert (right.row, right.col) == (0, 1)
    assert right.bbox == pytest.approx((100.0, 20.0, 150.0, 80.0))

    assert len(fake.calls) == 1
    assert fake.calls[0]["n_images"] == 1


def test_vlm_adapter_rowspan_colspan_and_fenced_json():
    from tables.structure.vlm_adapter import VLMStructureAdapter

    response = "```json\n" + json.dumps([
        {"row": 0, "col": 0, "rowspan": 2, "colspan": 1, "bbox": [0.0, 0.0, 1.0, 1.0]},
    ]) + "\n```"
    fake = _FakeVLM([response])
    adapter = VLMStructureAdapter(vlm_client=fake)

    png = _make_page_png(200, 100)
    result = adapter.detect(png, table_bboxes=[(0.0, 0.0, 100.0, 100.0)])

    cell = result[0].cells[0]
    assert (cell.rowspan, cell.colspan) == (2, 1)


def test_vlm_adapter_no_table_bboxes_returns_empty_without_calling_vlm():
    from tables.structure.vlm_adapter import VLMStructureAdapter

    fake = _FakeVLM([])
    adapter = VLMStructureAdapter(vlm_client=fake)
    result = adapter.detect(_make_page_png(), table_bboxes=[])

    assert result == []
    assert fake.calls == []


def test_vlm_adapter_bad_json_raises_so_caller_can_fall_back():
    from tables.structure import TableStructureError
    from tables.structure.vlm_adapter import VLMStructureAdapter

    fake = _FakeVLM(["not json at all"])
    adapter = VLMStructureAdapter(vlm_client=fake)

    # Unparseable response must NOT be silently treated as "no table here" --
    # that would make pdf_parser.py drop a table pdfplumber already found.
    # Raising lets _structure_model_tables fall back to geom_tables instead.
    with pytest.raises(TableStructureError):
        adapter.detect(_make_page_png(), table_bboxes=[(0.0, 0.0, 50.0, 50.0)])


def test_vlm_adapter_valid_empty_array_skips_table_without_raising():
    from tables.structure.vlm_adapter import VLMStructureAdapter

    fake = _FakeVLM(["[]"])
    adapter = VLMStructureAdapter(vlm_client=fake)
    result = adapter.detect(_make_page_png(), table_bboxes=[(0.0, 0.0, 50.0, 50.0)])

    assert result == []  # a genuinely empty (but valid) response is not an error
