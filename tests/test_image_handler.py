"""images/image_handler.py tests: locator -> bytes fetchers, blob store
dedup, nested table-cell collection, and the meaningful/meaningless OCR
paths. No network: VLM/LLM are fakes injected as arguments; a remote
http(s) src is asserted to stay unresolved rather than fetched.
"""

from __future__ import annotations

import base64
import zipfile

import pytest

from images.image_handler import (
    _collect_images, _fetch_pdf_region, _fetch_src, _fetch_zip_part,
    _store_blob, handle_images,
)
from parsers.base import (
    Cell, ImageBlock, ParagraphBlock, ParsedDocument, TableBlock, TableData,
)

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def complete_vision(self, *, system, user, images, max_tokens=2048):
        self.calls.append({"system": system, "n_images": len(images)})
        return self.text


class FakeLLM:
    def __init__(self, verdict_json: str) -> None:
        self.verdict_json = verdict_json
        self.calls = 0

    def complete(self, *, system, user, max_tokens=1024):
        self.calls += 1
        return self.verdict_json


# ---------------------------------------------------------------------------
# Minimal one-page PDF builder (same low-level shape as test_pdf_parser.py)
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


def _blank_pdf(width: int = 200, height: int = 100) -> bytes:
    return _build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
        f"/Contents 4 0 R >>".encode(),
        (b"<< /Length 0 >>\nstream\n\nendstream"),
    ])


# ---------------------------------------------------------------------------
# Blob store
# ---------------------------------------------------------------------------


def test_store_blob_dedups_by_sha256(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_IMAGES_DIR", str(tmp_path))
    sha1 = _store_blob(_PNG_1PX, "image/png")
    sha2 = _store_blob(_PNG_1PX, "image/png")
    assert sha1 == sha2
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == f"{sha1}.png"


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def test_fetch_zip_part_reads_media_from_zip(tmp_path):
    zpath = tmp_path / "doc.docx"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("word/media/image1.png", _PNG_1PX)

    result = _fetch_zip_part(zpath, {"part": "word/media/image1.png"})
    assert result is not None
    data, mime = result
    assert data == _PNG_1PX
    assert mime == "image/png"


def test_fetch_zip_part_missing_member_returns_none(tmp_path):
    zpath = tmp_path / "doc.docx"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("word/media/other.png", _PNG_1PX)
    assert _fetch_zip_part(zpath, {"part": "word/media/image1.png"}) is None


def test_fetch_src_data_uri(tmp_path):
    encoded = base64.b64encode(_PNG_1PX).decode()
    locator = {"src": f"data:image/png;base64,{encoded}"}
    data, mime = _fetch_src(tmp_path / "doc.md", locator)
    assert data == _PNG_1PX
    assert mime == "image/png"


def test_fetch_src_local_file_relative_to_source(tmp_path):
    (tmp_path / "img.png").write_bytes(_PNG_1PX)
    data, mime = _fetch_src(tmp_path / "doc.md", {"src": "img.png"})
    assert data == _PNG_1PX
    assert mime == "image/png"


def test_fetch_src_remote_url_left_unresolved(tmp_path):
    assert _fetch_src(tmp_path / "doc.md",
                       {"src": "https://example.com/x.png"}) is None


def test_fetch_pdf_region_crops_bbox_scaled_by_dpi(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_RENDER_DPI", "72")  # 1 point == 1 pixel
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_blank_pdf(width=200, height=100))

    data, mime = _fetch_pdf_region(
        pdf_path, {"page": 1, "bbox": [10, 20, 60, 70]}, {})
    assert mime == "image/png"
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data))
    assert img.size == (50, 50)


# ---------------------------------------------------------------------------
# Recursive collection (including table-cell images)
# ---------------------------------------------------------------------------


def test_collect_images_includes_nested_table_cell_images():
    body_img = ImageBlock(id="b0", image_index=1, locator={"src": "a.png"})
    cell_img = ImageBlock(id="b1", image_index=2, locator={"src": "b.png"})
    table = TableBlock(
        id="t0",
        table=TableData(n_rows=1, n_cols=1,
                         cells=[[Cell(blocks=[cell_img])]]))
    doc = ParsedDocument(doc_id="d", source_path="x.md", fmt="markdown",
                         blocks=[body_img, table])
    found = _collect_images(doc)
    assert found == [body_img, cell_img]


# ---------------------------------------------------------------------------
# handle_images: end-to-end with fakes
# ---------------------------------------------------------------------------


def _doc_with_data_uri_image() -> ParsedDocument:
    encoded = base64.b64encode(_PNG_1PX).decode()
    img = ImageBlock(id="b0", image_index=1,
                      locator={"src": f"data:image/png;base64,{encoded}"})
    return ParsedDocument(doc_id="d", source_path="unused.md", fmt="markdown",
                         blocks=[img])


def test_handle_images_meaningful_ocr_fills_ocr_text(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_IMAGES_DIR", str(tmp_path))
    doc = _doc_with_data_uri_image()
    vlm = FakeVLM("Reveneu Q3: 42")
    llm = FakeLLM('{"meaningful": true, "cleaned_text": "Revenue Q3: 42"}')

    handle_images(doc, vlm=vlm, llm=llm)

    block = doc.blocks[0]
    assert block.image_id == _store_blob(_PNG_1PX, "image/png")
    assert block.mime == "image/png"
    assert block.width == 1 and block.height == 1
    assert block.ocr_meaningful is True
    assert block.ocr_text == "Revenue Q3: 42"
    assert len(vlm.calls) == 1


def test_handle_images_no_text_skips_llm_check(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_IMAGES_DIR", str(tmp_path))
    doc = _doc_with_data_uri_image()
    vlm = FakeVLM("NO_TEXT")
    llm = FakeLLM('{"meaningful": true, "cleaned_text": "should not be used"}')

    handle_images(doc, vlm=vlm, llm=llm)

    block = doc.blocks[0]
    assert block.image_id is not None  # blob stored regardless of OCR outcome
    assert block.ocr_meaningful is False
    assert block.ocr_text is None
    assert llm.calls == 0


def test_handle_images_meaningless_ocr_keeps_blob_drops_text(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_IMAGES_DIR", str(tmp_path))
    doc = _doc_with_data_uri_image()
    vlm = FakeVLM("l0go inc.")
    llm = FakeLLM('{"meaningful": false, "cleaned_text": ""}')

    handle_images(doc, vlm=vlm, llm=llm)

    block = doc.blocks[0]
    assert block.image_id is not None
    assert block.ocr_meaningful is False
    assert block.ocr_text is None


def test_handle_images_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_IMAGES_DIR", str(tmp_path))
    doc = _doc_with_data_uri_image()
    vlm = FakeVLM("some text")
    llm = FakeLLM('{"meaningful": true, "cleaned_text": "some text"}')

    handle_images(doc, vlm=vlm, llm=llm)
    handle_images(doc, vlm=vlm, llm=llm)  # already resolved -> no-op

    assert len(vlm.calls) == 1


def test_handle_images_noop_without_images():
    doc = ParsedDocument(doc_id="d", source_path="x.md", fmt="markdown",
                         blocks=[ParagraphBlock(id="b0", text="hello")])
    # would raise if it tried to build a real VLM/LLM client
    handle_images(doc)


def test_handle_images_roundtrip_json(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_IMAGES_DIR", str(tmp_path))
    doc = _doc_with_data_uri_image()
    handle_images(doc, vlm=FakeVLM("NO_TEXT"))
    restored = ParsedDocument.from_json(doc.to_json())
    assert restored.to_dict() == doc.to_dict()
