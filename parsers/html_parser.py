"""HTML -> ParsedDocument.

Dependency-free (stdlib `html.parser`). Being text-based, every block gets an
exact byte `Span` into the raw file: `HTMLParser.getpos()` gives (line, col) which
is mapped to a byte offset, and `get_starttag_text()` gives an element's exact
source length (used for `<img>` spans).

Handled:
* h1..h6 -> HeadingBlock; p/div/section/... -> ParagraphBlock
* table + tr + td/th with rowspan/colspan -> TableBlock (real merges)
* ul/ol/li nested -> blocks carrying list_* metadata (B model); a table/image
  inside an <li> stays a real TableBlock/ImageBlock tagged with that list context
* img -> `<imageN>` marker in surrounding text + a real ImageBlock

Known limitations (v1): images inside table cells are reduced to their alt text;
nested tables (a table inside a cell) are flattened to text; <pre> whitespace is
collapsed like normal text.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

from .base import (
    BaseParser, ParsedDocument, Span,
    HeadingBlock, ParagraphBlock, TableBlock, ImageBlock, TableData, Merge,
)

_MARKER = re.compile(r"<image\d+>")
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
# Block-level tags that end/start a text block. Anything else is treated inline.
_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside",
          "blockquote", "pre", "figure", "figcaption", "hr", "ul", "ol", "li",
          "table", "thead", "tbody", "tfoot", "tr", "td", "th", *_HEADINGS}


def _has_text(text: str) -> bool:
    """True if text has content beyond image markers/whitespace."""
    return bool(_MARKER.sub("", text).strip())


def _build_table(rows: list[list[tuple[str, int, int]]]) -> TableData:
    """Expand HTML rows (each cell = (text, rowspan, colspan)) into a grid with
    merges. Covered cells become None; merges record the top-left origin."""
    grid: dict[tuple[int, int], str | None] = {}
    occupied: set[tuple[int, int]] = set()
    merges: list[Merge] = []
    ncols = 0
    for r, row in enumerate(rows):
        c = 0
        for text, rs, cs in row:
            while (r, c) in occupied:
                c += 1
            grid[(r, c)] = text
            if rs > 1 or cs > 1:
                merges.append(Merge(row=r, col=c, rowspan=rs, colspan=cs))
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        occupied.add((r + dr, c + dc))
                        grid[(r + dr, c + dc)] = None
            c += cs
            ncols = max(ncols, c)
    nrows = max((r for r, _ in grid), default=-1) + 1
    cells = [[grid.get((r, c)) for c in range(ncols)] for r in range(nrows)]
    return TableData(n_rows=nrows, n_cols=ncols, cells=cells, merges=merges)


class _Builder(HTMLParser):
    """Event-driven pass that turns HTML into an ordered block list."""

    def __init__(self, lines: list[str], line_start: list[int]) -> None:
        super().__init__(convert_charrefs=True)
        self.lines = lines
        self.line_start = line_start
        self.blocks: list = []
        self._bid = 0
        self._img = 0
        # current text buffer
        self.buf: list[str] = []
        self.buf_start: int | None = None
        self.buf_end: int | None = None
        self.pending: list[tuple[int, str | None, str, int, int]] = []
        self.heading_level: int | None = None
        # list nesting: stack of `ordered` bools; one list_id per outermost list
        self.list_stack: list[bool] = []
        self.list_id: str | None = None
        self._list_seq = 0
        # tables: stack of dicts (supports nested tables)
        self.tables: list[dict] = []
        self.skip = False  # inside <script>/<style>

    # -- offset + id helpers --------------------------------------------------

    def _byte(self, pos: tuple[int, int]) -> int:
        line, col = pos
        return self.line_start[line - 1] + len(self.lines[line - 1][:col].encode("utf-8"))

    def _next_id(self) -> str:
        s = f"b{self._bid}"
        self._bid += 1
        return s

    def _list_meta(self) -> dict:
        if not self.list_stack:
            return {}
        return {"list_id": self.list_id, "list_level": len(self.list_stack) - 1,
                "list_ordered": self.list_stack[-1]}

    @property
    def _cell_open(self) -> bool:
        return bool(self.tables) and self.tables[-1]["cell_open"]

    # -- text buffer ----------------------------------------------------------

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).split())
        imgs, start, end = self.pending, self.buf_start, self.buf_end
        self.buf, self.pending, self.buf_start, self.buf_end = [], [], None, None
        meta = self._list_meta()
        if self.heading_level and _has_text(text):
            self.blocks.append(HeadingBlock(
                id=self._next_id(), span=Span(byte_start=start, byte_end=end),
                text=_MARKER.sub("", text).strip(), level=self.heading_level))
        elif _has_text(text):
            self.blocks.append(ParagraphBlock(
                id=self._next_id(), span=Span(byte_start=start, byte_end=end),
                text=text, **meta))
        for idx, alt, src, b0, b1 in imgs:
            self.blocks.append(ImageBlock(
                id=self._next_id(), span=Span(byte_start=b0, byte_end=b1),
                image_index=idx, locator={"src": src}, alt_text=alt, **meta))

    # -- events ---------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip = True
            return
        if tag == "img":
            self._image(a)
            return
        if tag == "br":
            if not self._cell_open:
                self.buf.append(" ")
            return
        if tag not in _BLOCK:
            return  # inline tag: let its text accumulate

        self._flush()
        if tag in _HEADINGS:
            self.heading_level = _HEADINGS[tag]
        elif tag in ("ul", "ol"):
            if not self.list_stack:
                self._list_seq += 1
                self.list_id = f"L{self._list_seq}"
            self.list_stack.append(tag == "ol")
        elif tag == "table":
            self.tables.append({"rows": [], "row": [], "cell": [],
                                "cell_open": False, "cell_span": (1, 1),
                                "start": self._byte(self.getpos()),
                                "meta": self._list_meta()})
        elif tag == "tr" and self.tables:
            self.tables[-1]["row"] = []
        elif tag in ("td", "th") and self.tables:
            t = self.tables[-1]
            t["cell"] = []
            t["cell_open"] = True
            t["cell_span"] = (int(a.get("rowspan", 1) or 1),
                              int(a.get("colspan", 1) or 1))

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "img":
            self._image(dict(attrs))
        elif tag == "br" and not self._cell_open:
            self.buf.append(" ")
        # other self-closing tags: ignore

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = False
            return
        if tag == "img" or tag not in _BLOCK:
            return
        if tag in ("td", "th") and self.tables:
            t = self.tables[-1]
            rs, cs = t["cell_span"]
            t["row"].append((" ".join("".join(t["cell"]).split()), rs, cs))
            t["cell_open"] = False
            return
        if tag == "tr" and self.tables:
            t = self.tables[-1]
            if t["row"]:
                t["rows"].append(t["row"])
                t["row"] = []
            return
        if tag == "table" and self.tables:
            self._close_table()
            return

        self._flush()
        if tag in _HEADINGS:
            self.heading_level = None
        elif tag in ("ul", "ol") and self.list_stack:
            self.list_stack.pop()
            if not self.list_stack:
                self.list_id = None

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.tables:
            if self._cell_open:
                self.tables[-1]["cell"].append(data)
            return  # text between cells (whitespace) is dropped
        if self.buf_start is None:
            if not data.strip():
                return  # ignore leading indentation/whitespace
            self.buf_start = self._byte(self.getpos())
        self.buf.append(data)
        self.buf_end = self._byte(self.getpos()) + len(data.encode("utf-8"))

    # -- constructs -----------------------------------------------------------

    def _image(self, a: dict) -> None:
        alt = a.get("alt") or None
        src = a.get("src", "")
        if self._cell_open:  # v1: image in a cell -> its alt text
            if alt:
                self.tables[-1]["cell"].append(alt)
            return
        self._img += 1
        b0 = self._byte(self.getpos())
        b1 = b0 + len((self.get_starttag_text() or "").encode("utf-8"))
        if self.buf_start is None:
            self.buf_start, self.buf_end = b0, b1
        self.buf.append(f"<image{self._img}>")
        self.pending.append((self._img, alt, src, b0, b1))

    def _close_table(self) -> None:
        t = self.tables.pop()
        if t["row"]:  # a <tr> left unclosed
            t["rows"].append(t["row"])
        table = _build_table(t["rows"]) if t["rows"] else TableData(0, 0)
        end = self._byte(self.getpos()) + len("</table>")
        if self._cell_open:  # nested table -> flatten into parent cell (v1)
            flat = " ".join(c for row in table.cells for c in row if c)
            self.tables[-1]["cell"].append(flat)
            return
        self.blocks.append(TableBlock(
            id=self._next_id(), span=Span(byte_start=t["start"], byte_end=end),
            table=table, **t["meta"]))


class HtmlParser(BaseParser):
    extensions = (".html", ".htm", ".xhtml")
    mimetypes = ("text/html", "application/xhtml+xml")
    fmt = "html"
    version = "html/0.1"

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        data = Path(raw_path).read_bytes()
        raw_sha256 = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8", "replace")

        lines = text.split("\n")
        line_start = [0] * (len(lines) + 1)
        for i, ln in enumerate(lines):
            line_start[i + 1] = line_start[i] + len(ln.encode("utf-8")) + 1

        builder = _Builder(lines, line_start)
        builder.feed(text)
        builder.close()
        builder._flush()  # trailing text

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype="text/html",
            parser_version=self.version,
            blocks=builder.blocks,
        )
