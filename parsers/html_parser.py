"""HTML -> ParsedDocument.

Dependency-free (stdlib `html.parser`). Being text-based, every block gets an
exact byte `Span` into the raw file: `HTMLParser.getpos()` gives (line, col) which
is mapped to a byte offset, and `get_starttag_text()` gives an element's exact
source length (used for `<img>` spans).

Handled:
* h1..h6 -> HeadingBlock; p/div/section/... -> ParagraphBlock
* table + tr + td/th with rowspan/colspan -> TableBlock (real merges); a table
  or image nested inside a cell becomes a real anonymous (id="") TableBlock /
  ImageBlock inside that cell's `Cell.blocks` (mirrors docx's `_build_cell`),
  not a text flattening / alt-text-only degradation
* ul/ol/li nested -> blocks carrying list_* metadata (B model); a table/image
  inside an <li> stays a real TableBlock/ImageBlock tagged with that list context
* img -> `<imageN>` marker in surrounding text + a real ImageBlock
* inline formatting -> `runs` (see base.py's Mark/InlineRun): `<b>`/`<strong>`
  -> bold, `<i>`/`<em>` -> italic, `<u>` -> underline, `<s>`/`<strike>`/`<del>`
  -> strike, `<sup>`/`<sub>` -> super/subscript. Not applied inside table cells
  (cell text stays plain, same as before) or fenced/pre content.
* a[href] -> a run carrying `link=href` (see base.py's InlineRun); the visible
  anchor text stays in the block text. Not applied inside table cells.
* pre -> `CodeBlock` with whitespace preserved verbatim; a `language-x`/`lang-x`
  class on the `<pre>` or its inner `<code>` becomes the `language` hint

Known limitations (v1): inline formatting inside a table cell is not modeled
(cell text stays plain, matching the pre-existing behavior for cells).
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

from .base import (
    BaseParser, ParsedDocument, Span, Cell, Block,
    HeadingBlock, ParagraphBlock, CodeBlock, TableBlock, ImageBlock, TableData, Merge,
    Mark, InlineRun, finalize_runs, runs_have_marks,
)

# `<pre><code class="language-python">` / `class="lang-python"` -> language hint
_CODE_LANG = re.compile(r"lang(?:uage)?-(\S+)")

_MARKER = re.compile(r"<image\d+>")
_WS = re.compile(r"(\s+)")
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
# Block-level tags that end/start a text block. Anything else is treated inline.
_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside",
          "blockquote", "pre", "figure", "figcaption", "hr", "ul", "ol", "li",
          "table", "thead", "tbody", "tfoot", "tr", "td", "th", *_HEADINGS}
_INLINE_MARKS = {
    "b": Mark.BOLD, "strong": Mark.BOLD,
    "i": Mark.ITALIC, "em": Mark.ITALIC,
    "u": Mark.UNDERLINE,
    "s": Mark.STRIKE, "strike": Mark.STRIKE, "del": Mark.STRIKE,
    "sup": Mark.SUPERSCRIPT,
    "sub": Mark.SUBSCRIPT,
}


def _has_text(text: str) -> bool:
    """True if text has content beyond image markers/whitespace."""
    return bool(_MARKER.sub("", text).strip())


def _collapse_ws_runs(chunks: list[str], chunk_marks: list[tuple],
                      chunk_links: list[str | None]) -> list[InlineRun]:
    """Collapse whitespace across `chunks` the same way
    ``" ".join("".join(chunks).split())`` would (the old behavior), while
    keeping each non-whitespace token's active marks + link so word boundaries
    survive an inline `<b>`/`<i>`/`<a>`/... tag straddling a `handle_data` call
    boundary."""
    parts: list[InlineRun] = []
    pending_space = False
    for chunk, marks, link in zip(chunks, chunk_marks, chunk_links):
        for tok in _WS.split(chunk):
            if not tok:
                continue
            if tok.isspace():
                pending_space = True
                continue
            if pending_space and parts:
                parts.append(InlineRun(" "))
            pending_space = False
            parts.append(InlineRun(tok, marks, link))
    return finalize_runs(parts)


def _cell_from_parts(parts: list) -> Cell:
    """Turn one <td>/<th>'s mixed content (raw text chunks interleaved with
    nested TableBlock/ImageBlock instances) into a Cell, collapsing the text
    chunks like a normal paragraph and preserving nested blocks in document
    order -- the html mirror of docx's `_build_cell`."""
    blocks: list[Block] = []
    buf: list[str] = []

    def flush_text() -> None:
        if buf:
            text = " ".join("".join(buf).split())
            if _has_text(text):
                blocks.append(ParagraphBlock(id="", text=text))
            buf.clear()

    for part in parts:
        if isinstance(part, str):
            buf.append(part)
        else:
            flush_text()
            blocks.append(part)
    flush_text()
    return Cell(blocks=blocks)


def _build_table(rows: list[list[tuple[Cell, int, int]]]) -> TableData:
    """Expand HTML rows (each cell = (Cell, rowspan, colspan)) into a grid with
    merges. Covered cells become None; merges record the top-left origin."""
    grid: dict[tuple[int, int], Cell | None] = {}
    occupied: set[tuple[int, int]] = set()
    merges: list[Merge] = []
    ncols = 0
    for r, row in enumerate(rows):
        c = 0
        for cell, rs, cs in row:
            while (r, c) in occupied:
                c += 1
            grid[(r, c)] = cell
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
        # current text buffer (parallel lists: chunk text + its active marks + link)
        self.buf: list[str] = []
        self.buf_marks: list[tuple] = []
        self.buf_links: list[str | None] = []
        self.buf_start: int | None = None
        self.buf_end: int | None = None
        self.pending: list[tuple[int, str | None, str, int, int]] = []
        self.heading_level: int | None = None
        # inline formatting: stack of currently-open <b>/<i>/... marks
        self.mark_stack: list[Mark] = []
        # <a href>: stack of currently-open link targets (None for a bare <a>)
        self.link_stack: list[str | None] = []
        # <pre>: raw (uncollapsed) text buffer, active only while in_pre
        self.in_pre = False
        self.pre_buf: list[str] = []
        self.pre_start: int | None = None
        self.pre_lang: str | None = None
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

    def _append_text(self, s: str) -> None:
        self.buf.append(s)
        self.buf_marks.append(tuple(self.mark_stack))
        self.buf_links.append(self.link_stack[-1] if self.link_stack else None)

    # -- text buffer ----------------------------------------------------------

    def _flush(self) -> None:
        runs = _collapse_ws_runs(self.buf, self.buf_marks, self.buf_links)
        text = "".join(r.text for r in runs)
        imgs, start, end = self.pending, self.buf_start, self.buf_end
        self.buf, self.buf_marks, self.buf_links, self.pending = [], [], [], []
        self.buf_start, self.buf_end = None, None
        meta = self._list_meta()
        if self.heading_level and _has_text(text):
            has_marker = _MARKER.search(text) is not None
            htext = _MARKER.sub("", text).strip()
            self.blocks.append(HeadingBlock(
                id=self._next_id(), span=Span(byte_start=start, byte_end=end),
                text=htext, level=self.heading_level,
                # skip runs when a marker was stripped: run text would then no
                # longer join back up to `htext` (invariant in base.py)
                runs=(runs if runs_have_marks(runs) and not has_marker else [])))
        elif _has_text(text):
            self.blocks.append(ParagraphBlock(
                id=self._next_id(), span=Span(byte_start=start, byte_end=end),
                text=text, runs=(runs if runs_have_marks(runs) else []), **meta))
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
                self._append_text(" ")
            return
        if tag in _INLINE_MARKS:
            self.mark_stack.append(_INLINE_MARKS[tag])
            return
        if tag == "a":
            # a link is not a mark (it carries a URL); tracked on its own stack
            self.link_stack.append(a.get("href") or None)
            return
        if tag == "pre":
            self._flush()
            self.in_pre = True
            self.pre_buf = []
            m = _CODE_LANG.search(a.get("class") or "")
            self.pre_lang = m.group(1) if m else None
            self.pre_start = (self._byte(self.getpos())
                              + len((self.get_starttag_text() or "").encode("utf-8")))
            return
        if tag == "code" and self.in_pre:
            if self.pre_lang is None:  # language often sits on the inner <code>
                m = _CODE_LANG.search(a.get("class") or "")
                self.pre_lang = m.group(1) if m else None
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
            self._append_text(" ")
        # other self-closing tags: ignore

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = False
            return
        if tag == "img":
            return
        if tag in _INLINE_MARKS:
            mark = _INLINE_MARKS[tag]
            for idx in range(len(self.mark_stack) - 1, -1, -1):
                if self.mark_stack[idx] == mark:
                    del self.mark_stack[idx]
                    break
            return
        if tag == "a":
            if self.link_stack:
                self.link_stack.pop()
            return
        if tag == "pre":
            text = "".join(self.pre_buf)
            end = self._byte(self.getpos())
            if text:
                self.blocks.append(CodeBlock(
                    id=self._next_id(),
                    span=Span(byte_start=self.pre_start, byte_end=end),
                    text=text, language=self.pre_lang, **self._list_meta()))
            self.in_pre = False
            self.pre_buf = []
            self.pre_lang = None
            return
        if tag not in _BLOCK:
            return
        if tag in ("td", "th") and self.tables:
            t = self.tables[-1]
            rs, cs = t["cell_span"]
            t["row"].append((_cell_from_parts(t["cell"]), rs, cs))
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
        if self.in_pre:
            self.pre_buf.append(data)
            return
        if self.tables:
            if self._cell_open:
                self.tables[-1]["cell"].append(data)
            return  # text between cells (whitespace) is dropped
        if self.buf_start is None:
            if not data.strip():
                return  # ignore leading indentation/whitespace
            self.buf_start = self._byte(self.getpos())
        self._append_text(data)
        self.buf_end = self._byte(self.getpos()) + len(data.encode("utf-8"))

    # -- constructs -----------------------------------------------------------

    def _image(self, a: dict) -> None:
        alt = a.get("alt") or None
        src = a.get("src", "")
        b0 = self._byte(self.getpos())
        b1 = b0 + len((self.get_starttag_text() or "").encode("utf-8"))
        if self._cell_open:
            self._img += 1
            self.tables[-1]["cell"].append(ImageBlock(
                id="", span=Span(byte_start=b0, byte_end=b1),
                image_index=self._img, locator={"src": src}, alt_text=alt))
            return
        self._img += 1
        if self.buf_start is None:
            self.buf_start, self.buf_end = b0, b1
        self._append_text(f"<image{self._img}>")
        self.pending.append((self._img, alt, src, b0, b1))

    def _close_table(self) -> None:
        t = self.tables.pop()
        if t["row"]:  # a <tr> left unclosed
            t["rows"].append(t["row"])
        table = _build_table(t["rows"]) if t["rows"] else TableData(0, 0)
        end = self._byte(self.getpos()) + len("</table>")
        if self._cell_open:  # nested table -> a real TableBlock inside the cell
            self.tables[-1]["cell"].append(TableBlock(
                id="", span=Span(byte_start=t["start"], byte_end=end), table=table))
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
