"""markdown_parser.py: setext headings, escaped pipes, table-cell images,
and inline bold/italic/strike -> runs (see parsers/README.md).
"""

from __future__ import annotations

from parsers.base import HeadingBlock, ImageBlock, Mark, ParagraphBlock, TableBlock
from parsers.markdown_parser import MarkdownParser


def _parse(tmp_path, text: str, name: str = "doc.md"):
    p = tmp_path / name
    p.write_text(text)
    return MarkdownParser().parse(p, "doc")


# --- setext headings ---------------------------------------------------------


def test_setext_h1_and_h2(tmp_path):
    doc = _parse(tmp_path, "Title One\n=========\n\nTitle Two\n---------\n")
    headings = [b for b in doc.blocks if isinstance(b, HeadingBlock)]
    assert [(h.text, h.level) for h in headings] == [
        ("Title One", 1), ("Title Two", 2)]


def test_setext_not_triggered_after_list_item(tmp_path):
    # a `---` right after a list item is not (mis)read as a setext underline
    doc = _parse(tmp_path, "- item\n---\n")
    assert not any(isinstance(b, HeadingBlock) for b in doc.blocks)


def test_atx_heading_still_works(tmp_path):
    doc = _parse(tmp_path, "## Section\n")
    h = next(b for b in doc.blocks if isinstance(b, HeadingBlock))
    assert (h.text, h.level) == ("Section", 2)


# --- inline emphasis ----------------------------------------------------------


def test_bold_italic_strike_runs(tmp_path):
    doc = _parse(tmp_path, "This is **bold** and *italic* and ~~struck~~ text.")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.text == "This is bold and italic and struck text."
    assert "".join(r.text for r in p.runs) == p.text
    marks = {r.text: r.marks for r in p.runs}
    assert marks["bold"] == (Mark.BOLD,)
    assert marks["italic"] == (Mark.ITALIC,)
    assert marks["struck"] == (Mark.STRIKE,)


def test_bold_italic_combined(tmp_path):
    doc = _parse(tmp_path, "***both***")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert set(p.runs[0].marks) == {Mark.BOLD, Mark.ITALIC}


def test_single_underscore_italic_not_matched(tmp_path):
    # deliberate scope limit: single-underscore italic is not supported, to
    # avoid false positives on snake_case_identifiers.
    doc = _parse(tmp_path, "a snake_case_word and _not_italic_ either")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.runs == []
    assert "snake_case_word" in p.text


def test_asterisk_multiplication_not_matched(tmp_path):
    doc = _parse(tmp_path, "3 * 4 * 5 equals sixty")
    p = next(b for b in doc.blocks if isinstance(b, ParagraphBlock))
    assert p.runs == []
    assert p.text == "3 * 4 * 5 equals sixty"


def test_heading_inline_runs(tmp_path):
    doc = _parse(tmp_path, "# A **bold** heading\n")
    h = next(b for b in doc.blocks if isinstance(b, HeadingBlock))
    assert h.text == "A bold heading"
    assert any(r.marks == (Mark.BOLD,) for r in h.runs)


def test_list_item_runs(tmp_path):
    doc = _parse(tmp_path, "- item **one**\n- item two\n")
    paras = [b for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert paras[0].text == "item one"
    assert any(r.marks == (Mark.BOLD,) for r in paras[0].runs)
    assert paras[1].runs == []


# --- pipe tables: escaped pipes + cell images + cell runs --------------------


def test_escaped_pipe_in_cell(tmp_path):
    doc = _parse(tmp_path, "| A | B |\n|---|---|\n| x | y\\|z |\n")
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    assert table.table.cells[1][1].plain_text() == "y|z"


def test_table_cell_image_becomes_block(tmp_path):
    doc = _parse(tmp_path, "| A |\n|---|\n| ![alt](pic.png) |\n")
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    images = [b for b in doc.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    assert images[0].locator == {"src": "pic.png"}
    assert images[0].marker in table.table.cells[1][0].plain_text()


def test_table_cell_bold_runs(tmp_path):
    doc = _parse(tmp_path, "| A |\n|---|\n| **bold cell** |\n")
    table = next(b for b in doc.blocks if isinstance(b, TableBlock))
    cell = table.table.cells[1][0]
    assert cell.plain_text() == "bold cell"
    assert any(Mark.BOLD in r.marks for r in cell.blocks[0].runs)
