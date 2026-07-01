"""PPTX -> ParsedDocument.

Uses python-pptx. Each slide's shapes are walked in document order:
* title placeholder -> HeadingBlock
* other text frames -> paragraphs; bulleted/numbered paragraphs become list items
  (list_id per shape, list_level = paragraph indent level, ordered from a:buAutoNum
  vs a:buChar)
* tables -> TableBlock with merges (a:gridSpan/rowSpan via python-pptx merge cells)
* pictures -> ImageBlock (locator = media part path; image_handler fetches bytes)

Locator: slides have no meaningful byte offset per shape, so blocks carry
Span(part="ppt/slides/slideN.xml", page=N) and leave byte offsets unset — the page
number is the reliable locator here (Karar B best-effort).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pptx
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

from .base import (
    BaseParser, ParsedDocument, Span,
    HeadingBlock, ParagraphBlock, TableBlock, ImageBlock, TableData, Merge,
)

_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _bullet_kind(paragraph) -> str:
    """'ordered' | 'bullet' | 'none' | 'inherit' from the paragraph's a:pPr."""
    pPr = paragraph._p.find(qn("a:pPr"))
    if pPr is None:
        return "inherit"
    if pPr.find(qn("a:buNone")) is not None:
        return "none"
    if pPr.find(qn("a:buAutoNum")) is not None:
        return "ordered"
    if pPr.find(qn("a:buChar")) is not None:
        return "bullet"
    return "inherit"


def _table_data(table) -> TableData:
    nrows, ncols = len(table.rows), len(table.columns)
    cells: list[list[str | None]] = [[None] * ncols for _ in range(nrows)]
    merges: list[Merge] = []
    for r in range(nrows):
        for c in range(ncols):
            cell = table.cell(r, c)
            if cell.is_spanned:
                continue  # covered by a merge origin -> stays None
            cells[r][c] = cell.text
            if cell.is_merge_origin and (cell.span_height > 1 or cell.span_width > 1):
                merges.append(Merge(row=r, col=c,
                                    rowspan=cell.span_height, colspan=cell.span_width))
    return TableData(n_rows=nrows, n_cols=ncols, cells=cells, merges=merges)


class PptxParser(BaseParser):
    extensions = (".pptx",)
    mimetypes = (_MIME,)
    fmt = "pptx"
    version = "pptx/0.1"

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        raw_path = Path(raw_path)
        data = raw_path.read_bytes()
        raw_sha256 = hashlib.sha256(data).hexdigest()

        prs = pptx.Presentation(raw_path)
        blocks: list = []
        bid = 0
        img_n = 0

        def next_id() -> str:
            nonlocal bid
            s = f"b{bid}"
            bid += 1
            return s

        for slide_no, slide in enumerate(prs.slides, start=1):
            span = Span(part=f"ppt/slides/slide{slide_no}.xml", page=slide_no)
            for shape in slide.shapes:
                if shape.has_table:
                    blocks.append(TableBlock(id=next_id(), span=span,
                                             table=_table_data(shape.table)))
                elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    img_n += 1
                    locator, mime = self._picture(shape, slide)
                    blocks.append(ImageBlock(
                        id=next_id(), span=span, image_index=img_n,
                        locator=locator, mime=mime,
                        alt_text=shape.name or None))
                elif shape.has_text_frame:
                    self._emit_text(shape, slide_no, span, blocks, next_id)

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype=_MIME,
            page_count=len(prs.slides._sldIdLst),
            parser_version=self.version,
            blocks=blocks,
        )

    @staticmethod
    def _is_title(shape) -> bool:
        if not shape.is_placeholder:
            return False
        try:
            return "TITLE" in str(shape.placeholder_format.type)
        except (AttributeError, ValueError):
            return False

    def _emit_text(self, shape, slide_no, span, blocks, next_id) -> None:
        is_title = self._is_title(shape)
        list_id = f"s{slide_no}_{shape.shape_id}"
        for para in shape.text_frame.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            if is_title:
                blocks.append(HeadingBlock(id=next_id(), span=span,
                                           text=text, level=1))
                continue
            kind = _bullet_kind(para)
            is_list = (kind in ("ordered", "bullet")
                       or (kind != "none" and (shape.is_placeholder or para.level > 0)))
            if is_list:
                ordered = True if kind == "ordered" else (False if kind == "bullet" else None)
                blocks.append(ParagraphBlock(
                    id=next_id(), span=span, text=text,
                    list_id=list_id, list_level=para.level, list_ordered=ordered))
            else:
                blocks.append(ParagraphBlock(id=next_id(), span=span, text=text))

    @staticmethod
    def _picture(shape, slide) -> tuple[dict, str | None]:
        locator: dict = {}
        mime = None
        try:
            mime = shape.image.content_type
        except (AttributeError, ValueError):
            pass
        blip = shape._element.find(".//" + qn("a:blip"))
        if blip is not None:
            embed = blip.get(qn("r:embed"))
            try:
                part = slide.part.related_part(embed)
                locator = {"part": str(part.partname).lstrip("/")}
            except KeyError:
                pass
        return locator, mime
