"""render/markdown.py: IR (ParsedDocument) -> Markdown, per the agreed render
policy (plain Markdown where it suffices, inline HTML only where it cannot)."""

from __future__ import annotations

from parsers.base import (
    Cell, CodeBlock, HeadingBlock, ImageBlock, InlineRun, Mark, Merge,
    ParagraphBlock, ParsedDocument, TableBlock, TableData, text_cell,
)
from render.markdown import to_markdown


def _doc(*blocks) -> ParsedDocument:
    doc = ParsedDocument(doc_id="d", source_path="x", fmt="markdown")
    for k, b in enumerate(blocks):
        b.id = f"b{k}"
    doc.blocks = list(blocks)
    return doc


def _md(*blocks) -> str:
    return to_markdown(_doc(*blocks))


# --- headings / paragraphs / inline marks ------------------------------------


def test_heading_levels():
    assert _md(HeadingBlock(id="", text="Title", level=1)).strip() == "# Title"
    assert _md(HeadingBlock(id="", text="Sub", level=3)).strip() == "### Sub"


def test_heading_level_clamped():
    assert _md(HeadingBlock(id="", text="X", level=9)).strip() == "###### X"


def test_inline_bold_italic_strike():
    p = ParagraphBlock(id="", text="a b c", runs=[
        InlineRun("a ", ()), InlineRun("b", (Mark.BOLD,)), InlineRun(" c", ())])
    assert _md(p).strip() == "a **b** c"
    p2 = ParagraphBlock(id="", text="x", runs=[InlineRun("x", (Mark.ITALIC,))])
    assert _md(p2).strip() == "*x*"
    p3 = ParagraphBlock(id="", text="x", runs=[InlineRun("x", (Mark.STRIKE,))])
    assert _md(p3).strip() == "~~x~~"


def test_bold_italic_combined():
    p = ParagraphBlock(id="", text="x", runs=[
        InlineRun("x", (Mark.BOLD, Mark.ITALIC))])
    assert _md(p).strip() == "***x***"


def test_underline_is_html():
    p = ParagraphBlock(id="", text="x", runs=[InlineRun("x", (Mark.UNDERLINE,))])
    assert _md(p).strip() == "<u>x</u>"


def test_link_run():
    p = ParagraphBlock(id="", text="see docs here", runs=[
        InlineRun("see ", ()), InlineRun("docs", (), "https://e.com"),
        InlineRun(" here", ())])
    assert _md(p).strip() == "see [docs](https://e.com) here"


# --- super / subscript --------------------------------------------------------


def test_superscript_unicode_when_available():
    p = ParagraphBlock(id="", text="x2", runs=[
        InlineRun("x", ()), InlineRun("2", (Mark.SUPERSCRIPT,))])
    assert _md(p).strip() == "x²"


def test_subscript_unicode_h2o():
    p = ParagraphBlock(id="", text="H2O", runs=[
        InlineRun("H", ()), InlineRun("2", (Mark.SUBSCRIPT,)), InlineRun("O", ())])
    assert _md(p).strip() == "H₂O"


def test_superscript_falls_back_to_html_tag():
    # 'z' has no unicode superscript form -> <sup>
    p = ParagraphBlock(id="", text="Az", runs=[
        InlineRun("A", ()), InlineRun("z", (Mark.SUPERSCRIPT,))])
    assert _md(p).strip() == "A<sup>z</sup>"


# --- escaping -----------------------------------------------------------------


def test_control_chars_escaped():
    p = ParagraphBlock(id="", text="a*b_c | d")
    assert _md(p).strip() == r"a\*b\_c \| d"


def test_leading_block_marker_escaped():
    p = ParagraphBlock(id="", text="# not a heading")
    assert _md(p).strip() == r"\# not a heading"


# --- code ---------------------------------------------------------------------


def test_code_block_with_language():
    c = CodeBlock(id="", text="x = 1\ny = 2", language="python")
    assert _md(c).strip() == "```python\nx = 1\ny = 2\n```"


def test_code_block_no_language():
    c = CodeBlock(id="", text="plain")
    assert _md(c).strip() == "```\nplain\n```"


# --- images -------------------------------------------------------------------


def test_image_basic():
    img = ImageBlock(id="", image_index=1, alt_text="schema", image_id="img_a1")
    assert _md(img).strip() == "![schema](img_a1)"


def test_image_with_ocr():
    img = ImageBlock(id="", image_index=1, image_id="img_a1",
                     ocr_text="Bus coupler", ocr_meaningful=True)
    assert _md(img).strip() == "![](img_a1)\n\n*OCR: Bus coupler*"


def test_image_ocr_not_meaningful_omitted():
    img = ImageBlock(id="", image_index=1, image_id="img_a1",
                     ocr_text="noise", ocr_meaningful=False)
    assert _md(img).strip() == "![](img_a1)"


# --- tables -------------------------------------------------------------------


def _simple_table() -> TableBlock:
    cells = [[text_cell("A"), text_cell("B")], [text_cell("1"), text_cell("2")]]
    return TableBlock(id="", table=TableData(n_rows=2, n_cols=2, cells=cells))


def test_simple_table_is_gfm():
    md = _md(_simple_table()).strip()
    assert md == "| A | B |\n| --- | --- |\n| 1 | 2 |"


def test_table_with_merge_is_html():
    cells = [[text_cell("Header"), None], [text_cell("a"), text_cell("b")]]
    t = TableBlock(id="", table=TableData(
        n_rows=2, n_cols=2, cells=cells, merges=[Merge(row=0, col=0, colspan=2)]))
    md = _md(t)
    assert "<table>" in md
    assert 'colspan="2"' in md
    assert "Header" in md


def test_nested_table_forces_html():
    inner = TableBlock(id="", table=TableData(
        n_rows=1, n_cols=1, cells=[[text_cell("inner")]]))
    outer_cell = Cell(blocks=[inner])
    t = TableBlock(id="", table=TableData(
        n_rows=1, n_cols=1, cells=[[outer_cell]]))
    md = _md(t)
    assert md.count("<table>") == 2  # outer + nested
    assert "inner" in md


def test_table_description_appended():
    t = _simple_table()
    t.table_description = "A 2x2 grid"
    md = _md(t)
    assert md.strip().endswith("*A 2x2 grid*")


# --- lists --------------------------------------------------------------------


def test_simple_unordered_list_is_markdown():
    items = [
        ParagraphBlock(id="", text="one", list_id="L1", list_level=0,
                       list_ordered=False),
        ParagraphBlock(id="", text="two", list_id="L1", list_level=0,
                       list_ordered=False),
    ]
    assert _md(*items).strip() == "- one\n- two"


def test_ordered_and_nested_list_markdown():
    items = [
        ParagraphBlock(id="", text="a", list_id="L1", list_level=0,
                       list_ordered=True),
        ParagraphBlock(id="", text="a.i", list_id="L1", list_level=1,
                       list_ordered=True),
        ParagraphBlock(id="", text="b", list_id="L1", list_level=0,
                       list_ordered=True),
    ]
    assert _md(*items).strip() == "1. a\n  1. a.i\n2. b"


def test_list_with_image_item_is_html():
    items = [
        ParagraphBlock(id="", text="item", list_id="L1", list_level=0,
                       list_ordered=False),
        ImageBlock(id="", image_index=1, image_id="img_x", list_id="L1",
                   list_level=0, list_ordered=False),
    ]
    md = _md(*items)
    assert "<ul>" in md and "<li>" in md
    assert "img_x" in md


# --- end to end ---------------------------------------------------------------


def test_document_blocks_joined_with_blank_lines():
    md = _md(HeadingBlock(id="", text="T", level=1),
             ParagraphBlock(id="", text="body"))
    assert md == "# T\n\nbody\n"
