"""ParsedDocument (IR) -> Markdown.

The single consumer that turns the parsed IR into a human-readable Markdown
document. Render policy (decided with the user): emit plain GitHub-flavored
Markdown when it can faithfully carry the content, and fall back to inline HTML
only where Markdown has no equivalent.

* headings           -> `#`..`######`
* inline bold/italic /
  strikethrough      -> `**` / `*` / `~~`
* underline          -> `<u>...</u>` (Markdown has no underline syntax)
* super/subscript    -> the real unicode character when one exists (x2 -> x²),
                        else `<sup>`/`<sub>` (keeps meaning: x² is not x2)
* links              -> `[text](url)`
* code blocks        -> ``` fenced, carrying the language hint
* images             -> `![alt](image_id)` + an italic OCR line beneath when the
                        image carried meaningful OCR text
* tables             -> a GFM pipe table when it is rectangular and every cell is
                        a single paragraph; a raw `<table>` (with rowspan/colspan)
                        when it has merges or a cell holds block content
* lists              -> Markdown `-`/`1.` with indentation when every item is a
                        plain paragraph; `<ul>/<li>` when an item carries block
                        content (an image/table/code block inside the item)
* stray control chars-> escaped in plain text so `*`, `_`, `|`, `<` ... do not
                        trigger accidental formatting

The document's plain `text`/`runs`/structure is read straight from the IR; this
module adds no interpretation beyond the rendering choices above.
"""

from __future__ import annotations

import html as _html
import re

from parsers.base import (
    Block, Cell, CodeBlock, HeadingBlock, ImageBlock, InlineRun, Mark,
    ParagraphBlock, ParsedDocument, TableBlock, TableData,
)

# ---------------------------------------------------------------------------
# Inline text
# ---------------------------------------------------------------------------

# Characters that start a Markdown inline construct; escaped in plain text so a
# literal `*`/`_`/`|`/`<` etc. does not turn into emphasis/a table pipe/raw HTML.
_ESCAPE_RE = re.compile(r"([\\`*_~\[\]<>|])")

# A line whose first non-space char would start a block construct (heading,
# list, blockquote) if left bare; escaped only at the line start.
_LEADING_RE = re.compile(r"^(\s*)([#>+\-]|\d+[.)])(\s)")

_SUPERSCRIPT = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽",
    ")": "⁾", "n": "ⁿ", "i": "ⁱ",
}
_SUBSCRIPT = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍",
    ")": "₎", "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ", "h": "ₕ", "k": "ₖ",
    "l": "ₗ", "m": "ₘ", "n": "ₙ", "p": "ₚ", "s": "ₛ", "t": "ₜ",
}


def _to_script(text: str, table: dict[str, str]) -> str | None:
    """Return `text` mapped to unicode super/subscript, or None if any character
    has no such form (caller then falls back to a `<sup>`/`<sub>` tag)."""
    out = []
    for ch in text:
        mapped = table.get(ch)
        if mapped is None:
            return None
        out.append(mapped)
    return "".join(out)


def _escape_md(text: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", text)


def _render_run(run: InlineRun, *, html: bool) -> str:
    """One InlineRun -> a Markdown (or HTML) fragment applying its marks + link."""
    marks = set(run.marks)

    # super/subscript first: prefer a real unicode char (needs no escaping/tag).
    if Mark.SUPERSCRIPT in marks:
        uni = _to_script(run.text, _SUPERSCRIPT)
        base = uni if uni is not None else f"<sup>{_html.escape(run.text)}</sup>"
    elif Mark.SUBSCRIPT in marks:
        uni = _to_script(run.text, _SUBSCRIPT)
        base = uni if uni is not None else f"<sub>{_html.escape(run.text)}</sub>"
    else:
        base = _html.escape(run.text) if html else _escape_md(run.text)

    if Mark.ITALIC in marks:
        base = f"<em>{base}</em>" if html else f"*{base}*"
    if Mark.BOLD in marks:
        base = f"<strong>{base}</strong>" if html else f"**{base}**"
    if Mark.STRIKE in marks:
        base = f"<del>{base}</del>" if html else f"~~{base}~~"
    if Mark.UNDERLINE in marks:
        base = f"<u>{base}</u>"  # no Markdown syntax -> HTML in both modes
    if run.link:
        base = (f'<a href="{_html.escape(run.link, quote=True)}">{base}</a>'
                if html else f"[{base}]({run.link})")
    return base


def _inline(text: str, runs: list[InlineRun], *, html: bool = False) -> str:
    """Render a text block's inline content. `runs` (when present) carry the
    formatting; otherwise the plain `text` is escaped as-is."""
    if not runs:
        return _html.escape(text) if html else _escape_md(text)
    return "".join(_render_run(r, html=html) for r in runs)


def _paragraph_md(block: ParagraphBlock | HeadingBlock) -> str:
    """A paragraph's inline text, with per-line leading-construct escaping so a
    line that happens to start with `#`/`-`/`>` is not read as a block marker."""
    body = _inline(block.text, block.runs)
    return "\n".join(_LEADING_RE.sub(r"\1\\\2\3", ln) for ln in body.split("\n"))


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _table_is_simple(table: TableData) -> bool:
    """True if the table is a plain rectangle Markdown can represent: no merges,
    and every cell is a single paragraph/heading (no nested table/image/code,
    no multi-block cell)."""
    if table.merges:
        return False
    for row in table.cells:
        for cell in row:
            if cell is None:  # a covered slot without a declared merge -> not simple
                return False
            if len(cell.blocks) > 1:
                return False
            for b in cell.blocks:
                if not isinstance(b, (ParagraphBlock, HeadingBlock)):
                    return False
    return True


def _cell_inline(cell: Cell) -> str:
    """A simple cell's single-line inline Markdown (empty for an empty cell)."""
    if not cell.blocks:
        return ""
    b = cell.blocks[0]
    text = _inline(getattr(b, "text", ""), getattr(b, "runs", []))
    return text.replace("\n", " ")


def _render_gfm(table: TableData) -> str:
    rows = table.cells
    ncols = table.n_cols or (len(rows[0]) if rows else 0)
    if ncols == 0:
        return ""

    def line(cells: list) -> str:
        vals = [(_cell_inline(c) if c is not None else "") for c in cells]
        vals += [""] * (ncols - len(vals))
        return "| " + " | ".join(vals) + " |"

    out = [line(rows[0]) if rows else "| " + " | ".join([""] * ncols) + " |",
           "| " + " | ".join(["---"] * ncols) + " |"]
    for row in rows[1:]:
        out.append(line(row))
    return "\n".join(out)


def _cell_to_html(cell: Cell) -> str:
    parts = []
    for b in cell.blocks:
        if isinstance(b, TableBlock):
            parts.append(_render_html_table(b.table))
        elif isinstance(b, ImageBlock):
            parts.append(_image_html(b))
        elif isinstance(b, CodeBlock):
            parts.append(f"<pre><code>{_html.escape(b.text)}</code></pre>")
        else:  # Paragraph / Heading
            parts.append(_inline(getattr(b, "text", ""), getattr(b, "runs", []),
                                 html=True))
    return "<br>".join(p for p in parts if p)


def _render_html_table(table: TableData) -> str:
    """A `<table>` that preserves rowspan/colspan and block-level cell content."""
    spans = {(m.row, m.col): (m.rowspan, m.colspan) for m in table.merges}
    out = ["<table>"]
    for r, row in enumerate(table.cells):
        out.append("<tr>")
        for c, cell in enumerate(row):
            if cell is None:  # covered by another cell's span
                continue
            attrs = ""
            if (r, c) in spans:
                rs, cs = spans[(r, c)]
                if rs > 1:
                    attrs += f' rowspan="{rs}"'
                if cs > 1:
                    attrs += f' colspan="{cs}"'
            out.append(f"<td{attrs}>{_cell_to_html(cell)}</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _render_table(block: TableBlock) -> str:
    table = block.table
    body = _render_gfm(table) if _table_is_simple(table) else _render_html_table(table)
    if block.table_description:
        return f"{body}\n\n*{_escape_md(block.table_description)}*"
    return body


# ---------------------------------------------------------------------------
# Images / code
# ---------------------------------------------------------------------------


def _image_src(block: ImageBlock) -> str:
    return block.image_id or block.marker


def _image_md(block: ImageBlock) -> str:
    alt = _escape_md(block.alt_text) if block.alt_text else ""
    out = f"![{alt}]({_image_src(block)})"
    if block.ocr_meaningful and block.ocr_text:
        out += f"\n\n*OCR: {_escape_md(block.ocr_text)}*"
    return out


def _image_html(block: ImageBlock) -> str:
    alt = _html.escape(block.alt_text, quote=True) if block.alt_text else ""
    attrs = f'src="{_html.escape(_image_src(block), quote=True)}" alt="{alt}"'
    if block.width:
        attrs += f' width="{block.width}"'
    return f"<img {attrs}>"


def _render_code(block: CodeBlock) -> str:
    lang = block.language or ""
    return f"```{lang}\n{block.text}\n```"


# ---------------------------------------------------------------------------
# Lists (reconstructed from the flat block stream via list_id/list_level)
# ---------------------------------------------------------------------------


def _list_is_simple(items: list[Block]) -> bool:
    """True if every item is a plain paragraph (Markdown can nest those by
    indentation); a table/image/code inside an item forces the HTML fallback."""
    return all(isinstance(b, ParagraphBlock) for b in items)


def _render_list_md(items: list[ParagraphBlock]) -> str:
    out: list[str] = []
    counters: dict[int, int] = {}
    for b in items:
        lvl = b.list_level or 0
        for k in [k for k in counters if k > lvl]:
            del counters[k]
        if b.list_ordered:
            counters[lvl] = counters.get(lvl, 0) + 1
            marker = f"{counters[lvl]}."
        else:
            marker = "-"
        out.append(f"{'  ' * lvl}{marker} {_inline(b.text, b.runs)}")
    return "\n".join(out)


def _block_to_li_html(block: Block) -> str:
    if isinstance(block, ImageBlock):
        return _image_html(block)
    if isinstance(block, TableBlock):
        return _render_html_table(block.table)
    if isinstance(block, CodeBlock):
        return f"<pre><code>{_html.escape(block.text)}</code></pre>"
    return _inline(getattr(block, "text", ""), getattr(block, "runs", []), html=True)


def _render_list_html(items: list[Block]) -> str:
    """Nest items into `<ul>/<ol>` by list_level; a deeper item is placed inside
    the preceding item's `<li>`."""

    def build(idx: int, level: int) -> tuple[str, int]:
        ordered = bool(items[idx].list_ordered)
        tag = "ol" if ordered else "ul"
        out = [f"<{tag}>"]
        while idx < len(items):
            lvl = items[idx].list_level or 0
            if lvl < level:
                break
            if lvl > level:
                sub, idx = build(idx, lvl)
                if len(out) > 1 and out[-1].endswith("</li>"):
                    out[-1] = out[-1][:-5] + sub + "</li>"
                else:
                    out.append(f"<li>{sub}</li>")
                continue
            out.append(f"<li>{_block_to_li_html(items[idx])}</li>")
            idx += 1
        out.append(f"</{tag}>")
        return "".join(out), idx

    body, _ = build(0, items[0].list_level or 0)
    return body


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def _render_block(block: Block) -> str:
    if isinstance(block, HeadingBlock):
        level = min(max(block.level, 1), 6)
        return f"{'#' * level} {_inline(block.text, block.runs)}"
    if isinstance(block, CodeBlock):
        return _render_code(block)
    if isinstance(block, TableBlock):
        return _render_table(block)
    if isinstance(block, ImageBlock):
        return _image_md(block)
    if isinstance(block, ParagraphBlock):
        return _paragraph_md(block)
    return ""


def to_markdown(doc: ParsedDocument) -> str:
    """Render a parsed document to a Markdown string."""
    parts: list[str] = []
    i = 0
    blocks = doc.blocks
    n = len(blocks)
    while i < n:
        block = blocks[i]
        lid = block.list_id
        if lid is not None:
            # gather the maximal consecutive run of blocks sharing this list_id
            j = i
            items: list[Block] = []
            while j < n and blocks[j].list_id == lid:
                items.append(blocks[j])
                j += 1
            if _list_is_simple(items):
                parts.append(_render_list_md(items))
            else:
                parts.append(_render_list_html(items))
            i = j
            continue
        rendered = _render_block(block)
        if rendered:
            parts.append(rendered)
        i += 1
    return "\n\n".join(parts) + "\n"
