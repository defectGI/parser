"""Fetches the raw bytes an ImageBlock's locator points at, OCRs them via a
VLM, validates/cleans the OCR text via ocr_output_control, and stores every
image -- regardless of OCR outcome -- in the sha256-keyed blob store.

Pipeline per image:
    1. fetch raw bytes from the source document (format-specific: a zip
       media part for docx/pptx/xlsx, a data: URI or local file for
       html/markdown -- a remote http(s) src is left unresolved, see below
       -- or a rendered-and-cropped page region for pdf),
    2. store the bytes in storage/images/ by sha256 (dedup, immutable) and
       fill image_id/mime/width/height,
    3. ask a VLM to transcribe any text the image contains,
    4. hand the raw transcription to ocr_output_control, which judges
       whether it's meaningful and returns a cleaned version; ocr_text and
       ocr_meaningful are filled from that verdict.

Design decisions
-----------------
* The corrected text becomes the value of `ImageBlock.ocr_text` -- the
  image's own slot in the IR -- not a splice into a sibling paragraph's
  text. Every block's `Span` stays byte-exact to the source (see
  parsers/base.py); resolving a `<imageN>` marker to its image's text for
  display/search is a read-time concern for consumers (webapp/chunker), not
  something this stage does in place.
* A remote (http/https) `src` in html/markdown is left unresolved: no
  network fetch happens from this pipeline. image_id/ocr fields simply stay
  None for that ImageBlock.
* OCR uses a VLM only (no tesseract dependency): one `complete_vision` call
  per image, consistent with how parsers/pdf_parser.py already reads
  scanned pages, and keeps this stage model/provider-agnostic like the rest
  of llm/.
* Storing the blob never depends on OCR/VLM availability -- an unconfigured
  or failing VLM only leaves ocr_text/ocr_meaningful unset.

Environment (all optional):
    IMAGE_OCR_MAX_TOKENS  VLM transcription token budget (default 1024)
    PDF_RENDER_DPI        page render resolution for pdf crops (default 150;
                           shared with parsers/pdf_parser.py for consistency)
"""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from PIL import Image

from llm import LLMClient, LLMError, VLMClient, get_client, get_vlm_client
from parsers.base import ImageBlock, ParsedDocument, TableBlock
from storage_paths import images_dir

from .ocr_output_control import check_ocr_text

_OCR_SYSTEM = (
    "Transcribe any text visible in this image exactly as it appears, "
    "preserving line breaks. If the image contains no legible text (e.g. a "
    "photo, icon, logo or decorative graphic), reply with exactly: NO_TEXT"
)

Fetcher = Callable[[Path, dict[str, Any]], "tuple[bytes, str | None] | None"]


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _collect_images(doc: ParsedDocument) -> list[ImageBlock]:
    """Every ImageBlock in reading order, including ones nested inside table
    cells at any depth -- `ParsedDocument.images()` only looks at top-level
    blocks, which misses cell images (a real, tested feature) entirely."""
    found: list[ImageBlock] = []

    def walk_block(b) -> None:
        if isinstance(b, ImageBlock):
            found.append(b)
        elif isinstance(b, TableBlock):
            for row in b.table.cells:
                for cell in row:
                    if cell is None:
                        continue
                    for cb in cell.blocks:
                        walk_block(cb)

    for b in doc.blocks:
        walk_block(b)
    return found


def _ext_for(mime: str | None) -> str:
    return mimetypes.guess_extension(mime or "") or ".bin"


def _store_blob(data: bytes, mime: str | None) -> str:
    """Write into the shared immutable blob store, deduped by sha256; return
    the sha256 (the image_id)."""
    sha = hashlib.sha256(data).hexdigest()
    root = images_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sha}{_ext_for(mime)}"
    if not path.exists():
        path.write_bytes(data)
    return sha


# -- fetchers, one per locator shape -----------------------------------------


def _fetch_zip_part(raw_path: Path, locator: dict[str, Any]):
    """docx/pptx/xlsx: locator = {"part": "word/media/image1.png"}."""
    part = locator.get("part")
    if not part:
        return None
    try:
        with zipfile.ZipFile(raw_path) as zf:
            data = zf.read(part.lstrip("/"))
    except (KeyError, OSError):
        return None
    return data, mimetypes.guess_type(part)[0]


def _fetch_src(raw_path: Path, locator: dict[str, Any]):
    """html/markdown: locator = {"src": ...}.

    A data: URI is decoded inline; a local path is resolved relative to the
    source document. A remote http(s) URL is left unresolved by design (no
    network fetch from the parsing pipeline)."""
    src = locator.get("src")
    if not src:
        return None
    if src.startswith("data:"):
        header, sep, encoded = src.partition(",")
        if not sep or ";base64" not in header:
            return None
        mime = header[len("data:"):].split(";")[0] or None
        try:
            return base64.b64decode(encoded), mime
        except Exception:
            return None
    if urlsplit(src).scheme in ("http", "https"):
        return None
    candidate = Path(src)
    if not candidate.is_absolute():
        candidate = raw_path.parent / candidate
    if not candidate.is_file():
        return None
    return candidate.read_bytes(), mimetypes.guess_type(str(candidate))[0]


def _fetch_pdf_region(raw_path: Path, locator: dict[str, Any], cache: dict):
    """pdf: locator = {"page": N, "bbox": [x0, top, x1, bottom], ...} in
    pdfplumber's native point units. Re-renders that page and crops -- there
    is no embedded-object extraction, so this mirrors the audit-crop path
    parsers/pdf_parser.py already uses for unverified figures.

    A "vlm_figure" locator with no bbox (the VLM called out a figure that
    neither pdfplumber's object model nor its own bbox guess can back)
    cannot be resolved to any bytes at all; that ImageBlock stays
    unresolved, same as parsers/pdf_parser.py's own unverified-figure path.
    """
    page_no = locator.get("page")
    bbox = locator.get("bbox")
    if page_no is None or bbox is None:
        return None
    dpi = _env_int("PDF_RENDER_DPI", 150)
    key = (str(raw_path), page_no, dpi)
    png = cache.get(key)
    if png is None:
        import pdfplumber

        try:
            with pdfplumber.open(raw_path) as pdf:
                png = pdf.pages[page_no - 1].to_image(resolution=dpi).original
        except Exception:
            return None
        cache[key] = png
    scale = dpi / 72.0
    x0, top, x1, bottom = bbox
    box = tuple(round(v) for v in (x0 * scale, top * scale, x1 * scale, bottom * scale))
    buf = io.BytesIO()
    png.crop(box).save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def _fetcher_for(fmt: str) -> Fetcher | None:
    if fmt in ("docx", "pptx", "xlsx"):
        return _fetch_zip_part
    if fmt in ("html", "markdown"):
        return _fetch_src
    if fmt == "pdf":
        cache: dict[Any, Any] = {}
        return lambda raw_path, locator: _fetch_pdf_region(raw_path, locator, cache)
    return None


def _ocr_via_vlm(vlm: VLMClient, data: bytes, mime: str) -> str:
    text = vlm.complete_vision(
        system=_OCR_SYSTEM, user="Transcribe this image.",
        images=[(mime, data)], max_tokens=_env_int("IMAGE_OCR_MAX_TOKENS", 1024),
    ).strip()
    return "" if text == "NO_TEXT" else text


def handle_images(doc: ParsedDocument, vlm: VLMClient | None = None,
                   llm: LLMClient | None = None) -> ParsedDocument:
    """Fill every unresolved ImageBlock's image_id/mime/width/height, and --
    when a VLM is available and the image resolves -- its ocr_text/
    ocr_meaningful.

    Mutates `doc` in place and returns it. Idempotent: an ImageBlock that
    already has an `image_id` is left untouched, so re-running the stage (or
    running it after a partial failure) neither re-fetches nor re-OCRs it.
    No-op when the document has no unresolved images, so no client/network
    is needed for image-free documents.
    """
    images = [im for im in _collect_images(doc) if im.image_id is None]
    if not images:
        return doc

    fetch = _fetcher_for(doc.fmt)
    if fetch is None:
        return doc
    raw_path = Path(doc.source_path)

    vlm_unavailable = False
    llm_unavailable = False

    for block in images:
        fetched = fetch(raw_path, block.locator)
        if fetched is None:
            continue
        data, mime = fetched
        mime = mime or block.mime
        block.image_id = _store_blob(data, mime)
        block.mime = mime
        try:
            with Image.open(io.BytesIO(data)) as img:
                block.width, block.height = img.size
        except Exception:
            pass

        if vlm is None and not vlm_unavailable:
            try:
                vlm = get_vlm_client()
            except LLMError:
                vlm_unavailable = True
        if vlm is None:
            continue
        try:
            raw_text = _ocr_via_vlm(vlm, data, mime or "image/png")
        except LLMError:
            continue
        if not raw_text:
            block.ocr_meaningful = False
            continue

        if llm is None and not llm_unavailable:
            try:
                llm = get_client()
            except LLMError:
                llm_unavailable = True
        if llm is None:
            continue
        meaningful, cleaned = check_ocr_text(llm, raw_text)
        block.ocr_meaningful = meaningful
        if meaningful:
            block.ocr_text = cleaned

    return doc
