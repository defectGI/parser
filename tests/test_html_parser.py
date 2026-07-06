"""html_parser.py: inline formatting -> runs, <pre> whitespace preservation, and
nested table / cell image preserved as real blocks in Cell.blocks (see
parsers/README.md, EKSIKLER.txt #7).
"""

from __future__ import annotations

from parsers.base import (
    HeadingBlock, ImageBlock, Mark, ParagraphBlock, TableBlock,
)
from parsers.html_parser import HtmlParser


def _parse(tmp_path, html: str, name: str = "doc.html"):
    p = tmp_path / name
    p.write_text(html)
    return HtmlParser().parse(p, "doc")


# --- inline formatting ---------------------------------------------------------


def test_paragraph_inline_marks(tmp_path):
    doc = _parse(
        tmp_path,
        "<p>This is <b>bold</b> and <i>italic</i> and <b><i>both</i></b> "
        "and <s>struck</s> text.</p>")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.text == "This is bold and italic and both and struck text."
    assert "".join(r.text for r in p.runs) == p.text
    marks = {r.text: r.marks for r in p.runs}
    assert marks["bold"] == (Mark.BOLD,)
    assert marks["italic"] == (Mark.ITALIC,)
    assert marks["both"] == (Mark.BOLD, Mark.ITALIC)
    assert marks["struck"] == (Mark.STRIKE,)


def test_heading_inline_marks(tmp_path):
    doc = _parse(tmp_path, "<h1>Hello <b>World</b></h1>")
    h = next(b for b in doc.blocks if isinstance(b, HeadingBlock))
    assert h.text == "Hello World"
    assert any(r.marks == (Mark.BOLD,) for r in h.runs)


def test_whitespace_collapses_same_as_before(tmp_path):
    doc = _parse(tmp_path, "<p>\n  Hello   \n   <b>world</b>\n</p>")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.text == "Hello world"


def test_unformatted_paragraph_has_no_runs(tmp_path):
    doc = _parse(tmp_path, "<p>plain text</p>")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.runs == []
    assert "runs" not in p.to_dict()


def test_superscript_subscript(tmp_path):
    doc = _parse(tmp_path, "<p>x<sup>2</sup> and H<sub>2</sub>O</p>")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.text == "x2 and H2O"
    marks = [(r.text, r.marks) for r in p.runs]
    assert ("2", (Mark.SUPERSCRIPT,)) in marks
    assert ("2", (Mark.SUBSCRIPT,)) in marks


# --- <pre> --------------------------------------------------------------------


def test_pre_preserves_whitespace(tmp_path):
    doc = _parse(tmp_path, "<pre>line1\n    line2   indented\nline3</pre>")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.text == "line1\n    line2   indented\nline3"


# --- table cells: nested table + image preserved as real blocks --------------


def test_nested_table_in_cell_preserved(tmp_path):
    doc = _parse(
        tmp_path,
        "<table><tr><td>"
        "<table><tr><td>inner1</td><td>inner2</td></tr></table>"
        "</td><td>outer</td></tr></table>")
    outer = next(b for b in doc.blocks if isinstance(b, TableBlock))
    nested_cell = outer.table.cells[0][0]
    assert len(nested_cell.blocks) == 1
    inner_table = nested_cell.blocks[0]
    assert isinstance(inner_table, TableBlock)
    assert inner_table.id == ""  # anonymous, mirrors docx's _build_cell
    assert inner_table.table.cells[0][0].plain_text() == "inner1"
    assert outer.table.cells[0][1].plain_text() == "outer"


def test_image_in_cell_preserved_as_block(tmp_path):
    doc = _parse(
        tmp_path,
        '<table><tr><td><img src="pic.png" alt="a pic"></td></tr></table>')
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    cell = table.table.cells[0][0]
    assert len(cell.blocks) == 1
    img = cell.blocks[0]
    assert isinstance(img, ImageBlock)
    assert img.id == ""
    assert img.locator == {"src": "pic.png"}
    assert img.alt_text == "a pic"
    # the image must NOT also appear as a top-level document block
    assert not any(isinstance(b, ImageBlock) for b in doc.blocks)


def test_table_merges_still_work(tmp_path):
    doc = _parse(
        tmp_path,
        "<table><tr><td colspan='2'>wide</td></tr>"
        "<tr><td>a</td><td>b</td></tr></table>")
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    assert table.table.cells[0][0].plain_text() == "wide"
    assert table.table.cells[0][1] is None  # covered by the colspan
    assert len(table.table.merges) == 1


def test_list_item_inline_marks(tmp_path):
    doc = _parse(tmp_path, "<ul><li>item <b>one</b></li></ul>")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.text == "item one"
    assert any(r.marks == (Mark.BOLD,) for r in p.runs)
