"""Markdown -> ParsedDocument.

The simplest parser, used to validate the IR and byte-offset handling end to end.
Works directly on the raw bytes so every block's `Span` points at the exact byte
range in the source file (part=None => the raw file itself).

Supported constructs (phase 1 scope, GFM-ish):
* ATX headings (`#`..`######`)
* fenced code blocks (``` / ~~~) — kept as a paragraph, so `#`/`|` inside code
  are not misread as headings/tables
* pipe tables (header + `---` separator + rows) -> TableBlock
* lists (`-`/`*`/`+`/`1.`/`1)`), nested by indent -> blocks carrying list_* metadata
* images `![alt](src)` -> `<imageN>` marker in text + a real ImageBlock
* everything else -> ParagraphBlock

Not handled yet (left for later): blockquotes, setext headings, reference links,
inline HTML, escaped pipes inside table cells.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .base import (
    BaseParser, ParsedDocument, Span, text_cell,
    HeadingBlock, ParagraphBlock, TableBlock, ImageBlock, TableData,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_ORDERED = re.compile(r"\d+[.)]")
_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
_MARKER = re.compile(r"<image\d+>")
_FENCE = ("```", "~~~")


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
    """Split a pipe-table row into trimmed cell strings."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


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
                code = [text]
                j = i + 1
                while j < n and not lines[j][2].strip().startswith(fence):
                    code.append(lines[j][2])
                    j += 1
                if j < n:  # closing fence
                    code.append(lines[j][2])
                    blk_end = lines[j][1]
                    j += 1
                else:
                    blk_end = end
                blocks.append(ParagraphBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=blk_end),
                    text="\n".join(code)))
                i = j
                continue

            # --- heading ---
            m = _HEADING.match(text)
            if m:
                blocks.append(HeadingBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=end),
                    text=m.group(2).strip(), level=len(m.group(1))))
                i += 1
                continue

            # --- pipe table ---
            if "|" in text and i + 1 < n and _is_table_sep(lines[i + 1][2]):
                rows = [_split_row(text)]
                blk_end = lines[i + 1][1]
                j = i + 2
                while j < n and lines[j][2].strip() and "|" in lines[j][2]:
                    rows.append(_split_row(lines[j][2]))
                    blk_end = lines[j][1]
                    j += 1
                ncols = max(len(r) for r in rows)
                for r in rows:
                    r.extend([""] * (ncols - len(r)))
                blocks.append(TableBlock(
                    id=next_id(), span=Span(byte_start=start, byte_end=blk_end),
                    table=TableData(n_rows=len(rows), n_cols=ncols,
                                    cells=[[text_cell(x) for x in r]
                                           for r in rows])))
                i = j
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
                        blocks.append(ParagraphBlock(
                            id=next_id(), span=Span(byte_start=ls, byte_end=le),
                            text=item_text, **meta))
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
                blocks.append(ParagraphBlock(
                    id=next_id(), span=Span(byte_start=p_start, byte_end=p_end),
                    text=para_text))
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
