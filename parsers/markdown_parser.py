"""Markdown -> ParsedDocument.

The simplest parser, used to validate the IR and byte-offset handling end to end.
Works directly on the raw bytes so every block's `Span` points at the exact byte
range in the source file (part=None => the raw file itself).

Supported constructs (phase 1 scope, GFM-ish):
* ATX headings (`#`..`######`) and setext headings (a single text line followed
  by a `===`/`---` underline; a multi-line setext heading text is not detected —
  it falls back to a plain paragraph, same as before)
* fenced code blocks (``` / ~~~) -> `CodeBlock`; the info string after the opening
  fence (```` ```python ````) becomes `language`, and `#`/`|` inside code are not
  misread as headings/tables (the fence lines are not kept in the code text)
* pipe tables (header + `---` separator + rows) -> TableBlock; cells may escape
  a literal pipe as `\\|` and may contain `![alt](src)` images
* lists (`-`/`*`/`+`/`1.`/`1)`), nested by indent -> blocks carrying list_* metadata
* images `![alt](src)` -> `<imageN>` marker in text + a real ImageBlock
* inline links `[text](url)` -> a run carrying `link=url` (see base.py's InlineRun);
  the visible `text` stays in the block text. `![alt](src)` is an image, not a link.
* inline emphasis -> `runs` (see base.py's Mark/InlineRun): `**bold**`/`__bold__`,
  `*italic*`, `***bold+italic***`/`___bold+italic___`, `~~strike~~`. Deliberately
  NOT nested (a bold span's own content is not re-scanned for italic inside it)
  and single-underscore italic (`_word_`) is intentionally not matched — too
  prone to false positives inside snake_case_identifiers/urls/file_names that
  are common in real documents. `text` is always the delimiter-stripped clean
  view (join(r.text for r in runs) == text, matching every other parser).
* everything else -> ParagraphBlock

Not handled yet (left for later): blockquotes, reference-style links/images
(`[text][ref]`), inline HTML, nested/overlapping emphasis, multi-line setext
heading text.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .base import (
    BaseParser, ParsedDocument, Span, Cell, text_cell,
    HeadingBlock, ParagraphBlock, CodeBlock, TableBlock, ImageBlock, TableData,
    Mark, InlineRun, finalize_runs, runs_have_marks,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^(=+|-+)[ \t]*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_ORDERED = re.compile(r"\d+[.)]")
_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
_MARKER = re.compile(r"<image\d+>")
_FENCE = ("```", "~~~")
_ESCAPED_PIPE = "\x00ESCPIPE\x00"

# Lightweight, non-nested inline emphasis + inline links. Named-group
# backreferences (`(?P=name)`) tie each delimiter to its own closing match; only
# the winning alternative's groups are non-None on a given match. The link
# alternative `[text](url)` requires no preceding `!` so it never swallows an
# image (`![alt](src)` is resolved to an `<imageN>` marker before this runs); its
# `text` is kept verbatim (emphasis inside link text is not re-scanned, matching
# the no-nesting policy).
_EMPH = re.compile(
    r"(?P<bi>\*\*\*|___)(?P<bi_txt>.+?)(?P=bi)"
    r"|(?P<b>\*\*|__)(?P<b_txt>.+?)(?P=b)"
    r"|(?P<i>\*)(?P<i_txt>[^\s*](?:.*?[^\s*])?)(?P=i)"
    r"|(?P<st>~~)(?P<st_txt>.+?)(?P=st)"
    r"|(?<!\!)\[(?P<lk_txt>[^\]]+)\]\(\s*(?P<lk_url>[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)",
    re.DOTALL,
)


def _parse_inline(text: str) -> tuple[str, list[InlineRun]]:
    """Split `text` into InlineRuns on the emphasis markers above.

    Returns (clean_text, runs) where clean_text has the delimiters stripped and
    equals "".join(r.text for r in runs) — the same invariant docx/pptx already
    maintain. `runs` is the full run list (caller gates on `runs_have_marks`
    before attaching it to a block, same as every other parser).
    """
    runs: list[InlineRun] = []
    pos = 0
    for m in _EMPH.finditer(text):
        if m.start() < pos:
            continue  # nested inside an already-matched span; skip (no nesting)
        if m.start() > pos:
            runs.append(InlineRun(text[pos:m.start()]))
        if m.group("bi") is not None:
            runs.append(InlineRun(m.group("bi_txt"), (Mark.BOLD, Mark.ITALIC)))
        elif m.group("b") is not None:
            runs.append(InlineRun(m.group("b_txt"), (Mark.BOLD,)))
        elif m.group("i") is not None:
            runs.append(InlineRun(m.group("i_txt"), (Mark.ITALIC,)))
        elif m.group("st") is not None:
            runs.append(InlineRun(m.group("st_txt"), (Mark.STRIKE,)))
        else:
            runs.append(InlineRun(m.group("lk_txt"), (), m.group("lk_url")))
        pos = m.end()
    if pos < len(text):
        runs.append(InlineRun(text[pos:]))
    runs = finalize_runs(runs)
    return "".join(r.text for r in runs), runs


def _inline_cell(text: str) -> "Cell | None":
    """Like `text_cell`, but resolves inline emphasis into the cell's runs."""
    if not text:
        return text_cell(text)
    clean, runs = _parse_inline(text)
    marked = runs if runs_have_marks(runs) else []
    return Cell(blocks=[ParagraphBlock(id="", text=clean, runs=marked)])


def _has_text(text: str) -> bool:
    """True if `text` has content beyond image markers/whitespace.

    A block that is only `<imageN>` markers is redundant: the ImageBlock itself
    carries the position and (later) ocr_text, so no paragraph is emitted for it.
    """
    return bool(_MARKER.sub("", text).strip())


def _is_table_sep(line: str) -> bool:
    """True if `line` is a GFM table separator row, e.g. `|---|:--:|`."""
    s = line.strip()
    if "-" not in s:
        return False
    cols = s.strip("|").split("|")
    return all(re.fullmatch(r":?-+:?", c.strip()) for c in cols) and bool(cols)


def _split_row(line: str) -> list[str]:
    """Split a pipe-table row into trimmed cell strings.

    A `\\|` inside a cell is a literal pipe, not a column separator (GFM).
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    s = s.replace("\\|", _ESCAPED_PIPE)
    return [c.strip().replace(_ESCAPED_PIPE, "|") for c in s.split("|")]


class MarkdownParser(BaseParser):
    extensions = (".md", ".markdown")
    mimetypes = ("text/markdown", "text/x-markdown")
    fmt = "markdown"
    version = "markdown/0.1"

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        data = Path(raw_path).read_bytes()
        raw_sha256 = hashlib.sha256(data).hexdigest()

        # Line table with exact byte ranges. `end` is exclusive and does not
        # include the trailing newline. \r (CRLF) stays in the byte count but is
        # stripped from the decoded text.
        lines: list[tuple[int, int, str]] = []
        pos = 0
        for raw_line in data.split(b"\n"):
            start = pos
            end = start + len(raw_line)
            lines.append((start, end, raw_line.decode("utf-8", "replace").rstrip("\r")))
            pos = end + 1

        blocks: list = []
        img_n = 0   # global image index -> <imageN> marker
        bid = 0     # block id counter

        def next_id() -> str:
            nonlocal bid
            s = f"b{bid}"
            bid += 1
            return s

        def resolve_images(line_text: str, line_start: int, region_start: int):
            """Replace `![alt](src)` in line_text[region_start:] with <imageN>.

            Returns (substituted_region_text, [(image_index, alt, src, b0, b1), ...])
            with precise per-image byte offsets in the source line.
            """
            nonlocal img_n
            found: list[tuple[int, str | None, str, int, int]] = []

            def repl(m: re.Match) -> str:
                nonlocal img_n
                img_n += 1
                alt = m.group(1) or None
                src = m.group(2)
                b0 = line_start + len(line_text[: region_start + m.start()].encode("utf-8"))
                b1 = line_start + len(line_text[: region_start + m.end()].encode("utf-8"))
                found.append((img_n, alt, src, b0, b1))
                return f"<image{img_n}>"

            return _IMAGE.sub(repl, line_text[region_start:]), found

        def emit_images(found, list_meta: dict) -> None:
            for image_index, alt, src, b0, b1 in found:
                blocks.append(ImageBlock(
                    id=next_id(), span=Span(byte_start=b0, byte_end=b1),
                    image_index=image_index, locator={"src": src}, alt_text=alt,
                    **list_meta,
                ))

        def resolve_cell_images(cell_text: str, row_start: int, row_end: int) -> str:
            """Replace `![alt](src)` in a table cell with `<imageN>` + ImageBlock.

            Cell-splitting already discards a cell's column position within its
            source line, so (unlike `resolve_images`) this uses the whole row's
            byte range as the image's Span — a Karar B best-effort approximation,
            same spirit as pptx/pdf/xlsx's page-or-element-level spans.
            """
            nonlocal img_n

            def repl(m: re.Match) -> str:
                nonlocal img_n
                img_n += 1
                alt = m.group(1) or None
                src = m.group(2)
                blocks.append(ImageBlock(
                    id=next_id(), span=Span(byte_start=row_start, byte_end=row_end),
                    image_index=img_n, locator={"src": src}, alt_text=alt))
                return f"<image{img_n}>"

            return _IMAGE.sub(repl, cell_text)

        i = 0
        n = len(lines)
        while i < n:
            start, end, text = lines[i]
            stripped = text.strip()

            if not stripped:
                i += 1
                continue

            # --- fenced code block ---
            if stripped.startswith(_FENCE):
                fence = stripped[:3]
                language = stripped[3:].strip() or None  # info string after the fence
                code: list[str] = []  # inner lines only; fences are not content
                j = i + 1
                while j < n and not lines[j][2].strip().startswith(fence):
                    code.append(lines[j][2])
                    j += 1
                if j < n:  # closing fence
                    blk_end = lines[j][1]
                    j += 1
                else:
                    blk_end = end
                blocks.append(CodeBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=blk_end),
                    text="\n".join(code), language=language))
                i = j
                continue

            # --- heading ---
            m = _HEADING.match(text)
            if m:
                clean, runs = _parse_inline(m.group(2).strip())
                blocks.append(HeadingBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=end),
                    text=clean, level=len(m.group(1)),
                    runs=runs if runs_have_marks(runs) else []))
                i += 1
                continue

            # --- pipe table ---
            if "|" in text and i + 1 < n and _is_table_sep(lines[i + 1][2]):
                row_recs = [(start, end, _split_row(text))]
                blk_end = lines[i + 1][1]
                j = i + 2
                while j < n and lines[j][2].strip() and "|" in lines[j][2]:
                    row_recs.append((lines[j][0], lines[j][1], _split_row(lines[j][2])))
                    blk_end = lines[j][1]
                    j += 1
                ncols = max(len(cells) for _, _, cells in row_recs)
                table_rows = []
                for r_start, r_end, cells in row_recs:
                    cells = cells + [""] * (ncols - len(cells))
                    table_rows.append(
                        [resolve_cell_images(c, r_start, r_end) for c in cells])
                blocks.append(TableBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=blk_end),
                    table=TableData(n_rows=len(table_rows), n_cols=ncols,
                                    cells=[[_inline_cell(x) for x in r]
                                           for r in table_rows])))
                i = j
                continue

            # --- setext heading (single text line + === / --- underline) ---
            if (i + 1 < n and not _LIST.match(text)
                    and lines[i + 1][2].strip()
                    and _SETEXT.match(lines[i + 1][2].strip())):
                level = 1 if lines[i + 1][2].strip()[0] == "=" else 2
                clean, runs = _parse_inline(stripped)
                blk_end = lines[i + 1][1]
                blocks.append(HeadingBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=blk_end),
                    text=clean, level=level,
                    runs=runs if runs_have_marks(runs) else []))
                i += 2
                continue

            # --- list (contiguous run shares one list_id) ---
            if _LIST.match(text):
                list_id = _new_list_id(blocks)
                while i < n and _LIST.match(lines[i][2]):
                    ls, le, lt = lines[i]
                    lm = _LIST.match(lt)
                    level = len(lm.group(1).expandtabs(4)) // 2
                    ordered = bool(_ORDERED.match(lm.group(2)))
                    meta = {"list_id": list_id, "list_level": level,
                            "list_ordered": ordered}
                    item_text, found = resolve_images(lt, ls, lm.start(3))
                    item_text = item_text.strip()
                    if _has_text(item_text):
                        clean, runs = _parse_inline(item_text)
                        blocks.append(ParagraphBlock(
                            id=next_id(), span=Span(byte_start=ls, byte_end=le),
                            text=clean, runs=runs if runs_have_marks(runs) else [],
                            **meta))
                    emit_images(found, meta)
                    i += 1
                continue

            # --- paragraph (consecutive plain lines) ---
            para: list[tuple[int, int, str]] = []
            p_start = start
            p_end = end
            while i < n:
                s, e, t = lines[i]
                st = t.strip()
                if (not st or _HEADING.match(t) or _LIST.match(t)
                        or st.startswith(_FENCE)
                        or ("|" in t and i + 1 < n and _is_table_sep(lines[i + 1][2]))):
                    break
                para.append((s, e, t))
                p_end = e
                i += 1

            out_lines: list[str] = []
            all_found: list = []
            for s, e, t in para:
                sub, found = resolve_images(t, s, 0)
                out_lines.append(sub)
                all_found.extend(found)
            para_text = "\n".join(out_lines).strip()
            if _has_text(para_text):
                clean, runs = _parse_inline(para_text)
                blocks.append(ParagraphBlock(
                    id=next_id(), span=Span(byte_start=p_start, byte_end=p_end),
                    text=clean, runs=runs if runs_have_marks(runs) else []))
            emit_images(all_found, {})

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype="text/markdown",
            parser_version=self.version,
            blocks=blocks,
        )


def _new_list_id(blocks: list) -> str:
    """Allocate a fresh list id not yet used by any block."""
    used = {b.list_id for b in blocks if getattr(b, "list_id", None)}
    k = 1
    while f"L{k}" in used:
        k += 1
    return f"L{k}"
