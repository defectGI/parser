"""DOCX -> ParsedDocument.

Uses python-docx for structure, plus direct XML access for the parts python-docx
does not expose:
* numbering (w:numPr) -> list_id (numId) / list_level (ilvl); ordered vs bullet is
  resolved from word/numbering.xml (numFmt). This is where choosing the B list model
  pays off: docx stores lists exactly this way.
* table merges via w:gridSpan (colspan) and w:vMerge (rowspan).
* inline images (a:blip r:embed) -> `<imageN>` marker + ImageBlock whose locator is
  the media part path (image_handler fetches the bytes later).

Byte offsets (Karar B, best-effort): each body-level paragraph/table is matched
positionally to its element in word/document.xml (via expat) and gets a Span with
part="word/document.xml"; if the counts don't line up the offsets are left unset.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree
from xml.parsers import expat

from .base import (
    BaseParser, ParsedDocument, Span,
    HeadingBlock, ParagraphBlock, TableBlock, ImageBlock, TableData, Merge,
)

_MARKER = re.compile(r"<image\d+>")
_HEADING_STYLE = re.compile(r"^Heading (\d)$")
_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _has_text(text: str) -> bool:
    return bool(_MARKER.sub("", text).strip())


def _cell_text(tc) -> str:
    return "".join(t.text or "" for t in tc.iter(qn("w:t"))).strip()


def _build_docx_table(tbl) -> TableData:
    """Build TableData from a w:tbl element, honoring gridSpan/vMerge merges."""
    grid: dict[tuple[int, int], str | None] = {}
    mergemap: dict[tuple[int, int], list[int]] = {}   # (r,c) -> [rowspan, colspan]
    vorigin: dict[int, tuple[int, int]] = {}          # col -> active vmerge origin
    ncols = 0

    trs = tbl.findall(qn("w:tr"))
    for r, tr in enumerate(trs):
        c = 0
        for tc in tr.findall(qn("w:tc")):
            tcPr = tc.find(qn("w:tcPr"))
            colspan, vmerge = 1, None
            if tcPr is not None:
                gs = tcPr.find(qn("w:gridSpan"))
                if gs is not None:
                    colspan = int(gs.get(qn("w:val")))
                vm = tcPr.find(qn("w:vMerge"))
                if vm is not None:
                    vmerge = vm.get(qn("w:val")) or "continue"

            if vmerge == "continue":
                origin = vorigin.get(c)
                for dc in range(colspan):
                    grid[(r, c + dc)] = None
                if origin and origin in mergemap:
                    mergemap[origin][0] += 1
                    for dc in range(colspan):
                        vorigin[c + dc] = origin
                c += colspan
                ncols = max(ncols, c)
                continue

            grid[(r, c)] = _cell_text(tc)
            for dc in range(1, colspan):
                grid[(r, c + dc)] = None
            if vmerge == "restart":
                mergemap[(r, c)] = [1, colspan]
                for dc in range(colspan):
                    vorigin[c + dc] = (r, c)
            else:
                if colspan > 1:
                    mergemap[(r, c)] = [1, colspan]
                for dc in range(colspan):
                    vorigin.pop(c + dc, None)
            c += colspan
            ncols = max(ncols, c)

    nrows = len(trs)
    merges = [Merge(row=r, col=c, rowspan=rs, colspan=cs)
              for (r, c), (rs, cs) in mergemap.items() if rs > 1 or cs > 1]
    cells = [[grid.get((r, c)) for c in range(ncols)] for r in range(nrows)]
    return TableData(n_rows=nrows, n_cols=ncols, cells=cells, merges=merges)


def _numbering_ordered(zf: zipfile.ZipFile) -> dict[tuple[str, int], bool]:
    """Map (numId, ilvl) -> ordered? by reading word/numbering.xml numFmt."""
    try:
        root = etree.fromstring(zf.read("word/numbering.xml"))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    absmap: dict[str, dict[int, str]] = {}
    for an in root.findall(qn("w:abstractNum")):
        aid = an.get(qn("w:abstractNumId"))
        lvls = {}
        for lvl in an.findall(qn("w:lvl")):
            il = int(lvl.get(qn("w:ilvl")))
            nf = lvl.find(qn("w:numFmt"))
            lvls[il] = nf.get(qn("w:val")) if nf is not None else "bullet"
        absmap[aid] = lvls
    out: dict[tuple[str, int], bool] = {}
    for num in root.findall(qn("w:num")):
        nid = num.get(qn("w:numId"))
        ab = num.find(qn("w:abstractNumId"))
        if ab is None:
            continue
        for il, fmt in absmap.get(ab.get(qn("w:val")), {}).items():
            out[(nid, il)] = fmt not in ("bullet", "none")
    return out


def _style_numbering(zf: zipfile.ZipFile) -> dict[str, tuple[str, int]]:
    """Map styleId -> (numId, ilvl) for styles that carry numbering.

    Style-based lists (e.g. "List Bullet"/"List Number") define w:numPr on the
    style in word/styles.xml, not on the paragraph. basedOn inheritance is resolved.
    """
    try:
        root = etree.fromstring(zf.read("word/styles.xml"))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    raw: dict[str, tuple[str | None, int | None, str | None]] = {}
    for st in root.findall(qn("w:style")):
        sid = st.get(qn("w:styleId"))
        numId, ilvl = None, None
        pPr = st.find(qn("w:pPr"))
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:
                n = numPr.find(qn("w:numId"))
                lv = numPr.find(qn("w:ilvl"))
                if n is not None:
                    numId = n.get(qn("w:val"))
                if lv is not None:
                    ilvl = int(lv.get(qn("w:val")))
        based = st.find(qn("w:basedOn"))
        raw[sid] = (numId, ilvl, based.get(qn("w:val")) if based is not None else None)

    def resolve(sid: str, seen: tuple = ()) -> tuple[str | None, int | None]:
        if sid not in raw or sid in seen:
            return None, None
        numId, ilvl, base = raw[sid]
        if numId is None and base:
            bnum, bilvl = resolve(base, seen + (sid,))
            numId = bnum
            ilvl = ilvl if ilvl is not None else bilvl
        return numId, ilvl

    out: dict[str, tuple[str, int]] = {}
    for sid in raw:
        numId, ilvl = resolve(sid)
        if numId is not None and numId != "0":
            out[sid] = (numId, ilvl or 0)
    return out


def _body_child_spans(xml: bytes) -> list[tuple[str, int, int]]:
    """Byte spans of direct <w:body> children (w:p / w:tbl), in document order."""
    spans: list[tuple[str, int, int]] = []
    open_stack: list[tuple[str, int]] = []
    parser = expat.ParserCreate()
    state = {"depth": 0, "body": None}

    def start(name, _attrs):
        state["depth"] += 1
        if name == "w:body":
            state["body"] = state["depth"]
        elif (state["body"] is not None and state["depth"] == state["body"] + 1
              and name in ("w:p", "w:tbl")):
            open_stack.append((name, parser.CurrentByteIndex))

    def end(name):
        if (state["body"] is not None and state["depth"] == state["body"] + 1
                and name in ("w:p", "w:tbl") and open_stack):
            nm, b0 = open_stack.pop()
            spans.append((nm, b0, parser.CurrentByteIndex))
        state["depth"] -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(xml, True)
    except expat.ExpatError:
        return []
    return spans


class DocxParser(BaseParser):
    extensions = (".docx",)
    mimetypes = (_MIME,)
    fmt = "docx"
    version = "docx/0.1"

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        raw_path = Path(raw_path)
        data = raw_path.read_bytes()
        raw_sha256 = hashlib.sha256(data).hexdigest()

        document = docx.Document(raw_path)
        with zipfile.ZipFile(raw_path) as zf:
            ordered_map = _numbering_ordered(zf)
            style_map = _style_numbering(zf)
            spans = _body_child_spans(zf.read("word/document.xml"))

        body = document.element.body
        items = []  # (kind, obj)
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                items.append(("p", Paragraph(child, document)))
            elif child.tag == qn("w:tbl"):
                items.append(("tbl", Table(child, document)))

        # Positional match of body items to their XML byte spans (best-effort).
        span_by_index: dict[int, Span] = {}
        if len(spans) == len(items) and all(
                s[0] == ("w:p" if k == "p" else "w:tbl")
                for s, (k, _) in zip(spans, items)):
            for i, (_, b0, b1) in enumerate(spans):
                span_by_index[i] = Span(part="word/document.xml",
                                        byte_start=b0, byte_end=b1)

        blocks: list = []
        bid = 0
        img_n = [0]

        def next_id() -> str:
            nonlocal bid
            s = f"b{bid}"
            bid += 1
            return s

        for i, (kind, obj) in enumerate(items):
            span = span_by_index.get(i, Span())
            if kind == "tbl":
                blocks.append(TableBlock(id=next_id(), span=span,
                                         table=_build_docx_table(obj._tbl)))
                continue

            # paragraph
            meta = self._list_meta(obj, ordered_map, style_map)
            text, images = self._para_content(obj, document, img_n)
            style = obj.style.name if obj.style else ""
            hm = _HEADING_STYLE.match(style or "")

            if hm and not meta and _has_text(text):
                blocks.append(HeadingBlock(
                    id=next_id(), span=span,
                    text=_MARKER.sub("", text).strip(), level=int(hm.group(1))))
            elif _has_text(text):
                blocks.append(ParagraphBlock(
                    id=next_id(), span=span, text=text.strip(), **meta))
            for image_index, part, mime in images:
                blocks.append(ImageBlock(
                    id=next_id(), image_index=image_index,
                    locator={"part": part}, mime=mime, **meta))

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype=_MIME,
            parser_version=self.version,
            blocks=blocks,
        )

    @staticmethod
    def _list_meta(paragraph, ordered_map, style_map) -> dict:
        pPr = paragraph._p.find(qn("w:pPr"))
        numId, ilvl = None, None
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:  # direct numbering on the paragraph
                numId_e = numPr.find(qn("w:numId"))
                ilvl_e = numPr.find(qn("w:ilvl"))
                if numId_e is not None:
                    numId = numId_e.get(qn("w:val"))
                ilvl = int(ilvl_e.get(qn("w:val"))) if ilvl_e is not None else 0
            if numId is None:  # fall back to the paragraph's style
                pStyle = pPr.find(qn("w:pStyle"))
                if pStyle is not None:
                    numId, ilvl = style_map.get(pStyle.get(qn("w:val")), (None, None))
        if numId is None or numId == "0":
            return {}
        ilvl = ilvl or 0
        return {"list_id": f"num{numId}", "list_level": ilvl,
                "list_ordered": ordered_map.get((numId, ilvl))}

    @staticmethod
    def _para_content(paragraph, document, img_n):
        """Return (text_with_markers, [(image_index, part_path, mime), ...])."""
        parts: list[str] = []
        images: list[tuple[int, str, str | None]] = []
        for run in paragraph.runs:
            parts.append(run.text or "")
            for blip in run._element.findall(".//" + qn("a:blip")):
                embed = blip.get(qn("r:embed"))
                if not embed:
                    continue
                img_n[0] += 1
                parts.append(f"<image{img_n[0]}>")
                part_path, mime = "?", None
                try:
                    ipart = document.part.related_parts[embed]
                    part_path = str(ipart.partname).lstrip("/")
                    mime = ipart.content_type
                except KeyError:
                    pass
                images.append((img_n[0], part_path, mime))
        return "".join(parts), images
