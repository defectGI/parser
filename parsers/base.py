"""BaseParser interface and ParsedDocument IR definition.

This module is the parser's single "contract" file: every format parser produces
a `ParsedDocument` (IR), and all later stages (images/, tables/, webapp/) read and
enrich this IR. The IR is stored as JSON under `storage/output/*.json`.

Design decisions
----------------
* Raw image bytes are NOT embedded in the IR; an image is referenced only by its
  `image_id` (sha256). The blob itself lives under `storage/images/`.
* Every block carries a `Span`: the byte range in the original file is preserved
  (markitdown was dropped because it lost this). For binary formats (docx/pptx/xlsx
  are zips) the offset may point into an internal stream; which stream is indicated
  by `Span.part`.
* Images are kept as block-level `ImageBlock` entries rather than inline strings, so
  the "text referring to an image -> image" link can be resolved via `Block.id`
  (the webapp's navigation requirement). At parse time `image_id`/`ocr_text`/
  `ocr_meaningful` are None; the images/ stage fills them.
* Tables are fully structured JSON including merges. `table_description` is kept in
  the IR (on TableBlock); the LLM-check status/retry state will likewise live in the
  IR when the tables/ stage adds it — there is no separate database.
* Lists are NOT a container block. The IR stays a flat block stream; list membership
  is metadata on ordinary blocks (`list_id`/`list_level`/`list_ordered`). This mirrors
  how docx (w:numId/w:ilvl) and pdf natively store lists, and keeps block-level content
  inside a list item (a table/image in a `<li>`) as a real TableBlock/ImageBlock instead
  of flattening it away. A "list" is reconstructed by grouping blocks that share a
  `list_id`.
* Provenance is additive metadata on any block. Deterministic parsers leave it
  None; the pdf parser labels every text-bearing block with how its content was
  obtained/confirmed: "text-layer-verified" (matched the PDF's own text layer),
  "consensus-verified" (two independent readers agreed), or "unverified"
  (could not be confirmed — `source_crop` then holds the sha256 of a page/region
  render in the blob store so the content can be audited). The citation pipeline
  reads its trust level from this tag.
* Inline formatting is additive, not a new tree. A text block keeps its plain `text`
  (the canonical view every consumer already uses) and gains an OPTIONAL `runs` list
  of `InlineRun`s carrying semantic marks (bold/italic/underline/strike/super/sub);
  `"".join(r.text for r in runs) == text`. Unformatted blocks store no `runs` (no JSON
  bloat). Only semantic marks are modeled — super/subscript because flattening them
  corrupts meaning (x² vs x2). Populated by docx; other parsers pending.
* `heading_path` gives every block its section breadcrumb (ancestor heading texts,
  outermost first) without requiring consumers to walk the flat block stream looking
  for the nearest preceding heading at each level. A parser builds this with a
  `HeadingStack` (below) as it emits blocks in reading order. None where a parser
  hasn't been wired to populate it yet, or for blocks before the first heading.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# IR schema version. Bump when the contract changes; readers migrate accordingly.
# v2: a table cell is no longer a bare string but a `Cell` (list of blocks), so a
# cell can hold nested tables/paragraphs losslessly. Readers still accept v1 (a
# cell serialized as a plain string) — see `_cell_from_dict`.
# v3: `Block.heading_path` added (ancestor heading texts). Absent/None on a block
# reads the same as "not populated by this parser yet" — no migration needed.
IR_VERSION = 3


class BlockType(str, Enum):
    """IR block types. `str`-based so it serializes to a plain string in JSON."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"


# ---------------------------------------------------------------------------
# Location / source tracking
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """A block's location in the original source.

    All fields are optional; if a format cannot provide a value it stays None.

    byte_start/byte_end
        Half-open byte range [start, end) in the source. For text formats
        (md/html) this points directly into the raw file. For binary formats it,
        together with `part`, denotes the offset within that internal stream.
    part
        Which stream the offsets address. None => the raw input file itself.
        E.g. "word/document.xml" for docx.
    page
        1-based page/slide number (pdf page, pptx slide). `page` is used for
        single-page ranges; blocks spanning multiple pages fill page_start/page_end.
    """

    byte_start: int | None = None
    byte_end: int | None = None
    part: str | None = None
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Span":
        if not data:
            return cls()
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Table structure
# ---------------------------------------------------------------------------


@dataclass
class Merge:
    """A merged cell region. The top-left cell (row, col) carries the content;
    the other covered cells are None in `cells`."""

    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"row": self.row, "col": self.col,
                "rowspan": self.rowspan, "colspan": self.colspan}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Merge":
        return cls(row=data["row"], col=data["col"],
                   rowspan=data.get("rowspan", 1), colspan=data.get("colspan", 1))


@dataclass
class TableData:
    """Structured table content.

    cells
        Row-major 2D matrix; each entry is a `Cell` (its content is a list of
        blocks) or None for a slot covered by a merge. `n_rows`/`n_cols` are the
        logical dimensions. A cell is block-structured rather than plain text so a
        table nested inside a cell is preserved losslessly; use `Cell.plain_text()`
        for a flat text view.
    merges
        Merge regions; the top-left cell carries the content.
    """

    n_rows: int
    n_cols: int
    cells: list[list["Cell | None"]] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "cells": [[c.to_dict() if c is not None else None for c in row]
                      for row in self.cells],
            "merges": [m.to_dict() for m in self.merges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableData":
        return cls(
            n_rows=data["n_rows"],
            n_cols=data["n_cols"],
            cells=[[_cell_from_dict(c) for c in row]
                   for row in data.get("cells", [])],
            merges=[Merge.from_dict(m) for m in data.get("merges", [])],
        )


# ---------------------------------------------------------------------------
# Inline text formatting
# ---------------------------------------------------------------------------


class Mark(str, Enum):
    """An inline character style. `str`-based so it serializes to a plain string.

    Only *semantic* styles are modeled — ones that can change meaning, not pure
    cosmetics (color, font, highlight). SUPERSCRIPT/SUBSCRIPT matter because
    flattening them corrupts text (x2 vs x², H2O vs H₂O).
    """

    BOLD = "bold"
    ITALIC = "italic"
    UNDERLINE = "underline"
    STRIKE = "strike"
    SUPERSCRIPT = "superscript"
    SUBSCRIPT = "subscript"


@dataclass
class InlineRun:
    """A maximal text span sharing the same set of active marks.

    A text block keeps its plain `text` as the canonical view (unchanged for every
    text-only consumer); `runs` is an *optional* parallel detail whose concatenated
    text equals `text`. A run with no marks is just unstyled text.
    """

    text: str
    marks: tuple[Mark, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "marks": [m.value for m in self.marks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InlineRun":
        return cls(text=data.get("text", ""),
                   marks=tuple(Mark(m) for m in data.get("marks", [])))


def runs_have_marks(runs: list[InlineRun]) -> bool:
    """True if any run carries formatting (else `runs` is redundant with `text`)."""
    return any(r.marks for r in runs)


def finalize_runs(runs: list[InlineRun]) -> list[InlineRun]:
    """Merge adjacent runs with identical marks, drop empties, and strip the
    leading/trailing whitespace of the whole sequence so that
    `"".join(r.text for r in result)` equals the block's stripped plain text."""
    merged: list[InlineRun] = []
    for r in runs:
        if not r.text:
            continue
        if merged and merged[-1].marks == r.marks:
            merged[-1] = InlineRun(merged[-1].text + r.text, r.marks)
        else:
            merged.append(InlineRun(r.text, r.marks))
    if merged:
        merged[0] = InlineRun(merged[0].text.lstrip(), merged[0].marks)
        merged[-1] = InlineRun(merged[-1].text.rstrip(), merged[-1].marks)
        merged = [r for r in merged if r.text]
    return merged


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """Common base for all blocks.

    id
        Stable block identifier within the document (e.g. "b0", "b1"). Used to
        establish links (webapp, chunker). The parser assigns these in order.
    span
        Location in the original source.

    List membership (see module docstring, "Lists"): a list is not a container
    block. Any block may be a list item; blocks sharing a `list_id` form one list.
    All three fields are None for blocks that are not part of a list.

    list_id
        Groups blocks belonging to the same list (docx w:numId analogue).
    list_level
        0-based nesting depth of the item (docx w:ilvl analogue).
    list_ordered
        True => numbered item, False => bullet. May vary per item within a list.

    heading_path
        Ancestor heading texts, outermost first (e.g. ["MIL-STD-1553 Bus
        Couplers", "Product Overview"]) — see module docstring, "heading_path".
        None if this parser doesn't populate it, or the block precedes any
        heading.

    Provenance (see module docstring): both fields are None for deterministic
    formats; the pdf parser fills them.

    provenance
        "text-layer-verified" | "consensus-verified" | "unverified".
    source_crop
        sha256 of a page/region render in the blob store (storage/images/)
        backing an unverified block, for human audit.
    """

    id: str
    span: Span = field(default_factory=Span)

    list_id: str | None = None
    list_level: int | None = None
    list_ordered: bool | None = None

    heading_path: list[str] | None = None

    provenance: str | None = None
    source_crop: str | None = None

    # Subclasses override this.
    type: BlockType = field(init=False, default=BlockType.PARAGRAPH)

    def _base_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type.value, "id": self.id}
        span = self.span.to_dict()
        if span:
            d["span"] = span
        for key in ("list_id", "list_level", "list_ordered",
                    "heading_path", "provenance", "source_crop"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        return d

    @staticmethod
    def _base_kwargs(data: dict[str, Any]) -> dict[str, Any]:
        """Shared base fields for subclass deserialization."""
        return {
            "id": data["id"],
            "span": Span.from_dict(data.get("span")),
            "list_id": data.get("list_id"),
            "list_level": data.get("list_level"),
            "list_ordered": data.get("list_ordered"),
            "heading_path": data.get("heading_path"),
            "provenance": data.get("provenance"),
            "source_crop": data.get("source_crop"),
        }

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Block":
        btype = BlockType(data["type"])
        cls = _BLOCK_CLASSES[btype]
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Block":  # pragma: no cover
        raise NotImplementedError


@dataclass
class HeadingBlock(Block):
    text: str = ""
    level: int = 1  # 1 = top-level heading
    # Optional inline formatting detail; empty => no formatting captured/present.
    runs: list[InlineRun] = field(default_factory=list)

    type: BlockType = field(init=False, default=BlockType.HEADING)

    def to_dict(self) -> dict[str, Any]:
        d = {**self._base_dict(), "text": self.text, "level": self.level}
        if self.runs:
            d["runs"] = [r.to_dict() for r in self.runs]
        return d

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "HeadingBlock":
        return cls(**cls._base_kwargs(data),
                   text=data.get("text", ""), level=data.get("level", 1),
                   runs=[InlineRun.from_dict(r) for r in data.get("runs", [])])


class HeadingStack:
    """Tracks the current ancestor-heading breadcrumb while a parser emits
    blocks in reading order, for populating `Block.heading_path`.

    Call `enter(level, text)` for every heading as it's emitted (this also
    returns that heading's own `heading_path`, i.e. its ancestors), and read
    `path()` for every other block. A level-N heading closes any open heading
    at level >= N, mirroring how heading nesting works in every format here
    (docx outline levels, pdf font-size ranks, pptx placeholder levels, ...).
    """

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def enter(self, level: int, text: str) -> list[str] | None:
        path = self.path()
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, text))
        return path

    def path(self) -> list[str] | None:
        return [text for _, text in self._stack] if self._stack else None


@dataclass
class ParagraphBlock(Block):
    text: str = ""
    # Optional inline formatting detail; empty => no formatting captured/present.
    runs: list[InlineRun] = field(default_factory=list)

    type: BlockType = field(init=False, default=BlockType.PARAGRAPH)

    def to_dict(self) -> dict[str, Any]:
        d = {**self._base_dict(), "text": self.text}
        if self.runs:
            d["runs"] = [r.to_dict() for r in self.runs]
        return d

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "ParagraphBlock":
        return cls(**cls._base_kwargs(data), text=data.get("text", ""),
                   runs=[InlineRun.from_dict(r) for r in data.get("runs", [])])


@dataclass
class TableBlock(Block):
    table: TableData = field(default_factory=lambda: TableData(0, 0))
    # Filled by tables/table_describe.py; all None at parse time.
    table_description: str | None = None
    # LLM-check outcome (no separate DB):
    # "ok" | "flagged" | "empty" (no extractable cell text, LLM never called)
    # | None (check not run).
    describe_status: str | None = None
    describe_attempts: int | None = None

    type: BlockType = field(init=False, default=BlockType.TABLE)

    def to_dict(self) -> dict[str, Any]:
        d = {**self._base_dict(), "table": self.table.to_dict()}
        for key in ("table_description", "describe_status", "describe_attempts"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        return d

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "TableBlock":
        return cls(**cls._base_kwargs(data),
                   table=TableData.from_dict(data["table"]),
                   table_description=data.get("table_description"),
                   describe_status=data.get("describe_status"),
                   describe_attempts=data.get("describe_attempts"))


@dataclass
class ImageBlock(Block):
    """Block-level placeholder for an image in the document flow.

    marker
        Sequential `<imageN>` marker within the document. `image_index` = N.
    locator
        Where the raw image lives in the source (image_handler fetches bytes with
        this). Format-specific free-form dict: e.g. {"part": "word/media/image1.png"}
        or {"page": 3, "xref": 12} or {"slide": 2, "shape": 4}.
    image_id, ocr_text, ocr_meaningful, mime, width, height
        Filled by the images/ stage; None at parse time. `ocr_meaningful is None`
        => not yet processed. False => blob+db record is kept but the text is not
        indexed.
    """

    image_index: int = 0
    locator: dict[str, Any] = field(default_factory=dict)
    alt_text: str | None = None

    # Resolution fields filled by the images/ stage:
    image_id: str | None = None
    ocr_text: str | None = None
    ocr_meaningful: bool | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None

    type: BlockType = field(init=False, default=BlockType.IMAGE)

    @property
    def marker(self) -> str:
        return f"<image{self.image_index}>"

    def to_dict(self) -> dict[str, Any]:
        d = {**self._base_dict(), "image_index": self.image_index,
             "locator": self.locator}
        for key in ("alt_text", "image_id", "ocr_text", "ocr_meaningful",
                    "mime", "width", "height"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        return d

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "ImageBlock":
        return cls(
            **cls._base_kwargs(data),
            image_index=data.get("image_index", 0),
            locator=data.get("locator", {}),
            alt_text=data.get("alt_text"),
            image_id=data.get("image_id"),
            ocr_text=data.get("ocr_text"),
            ocr_meaningful=data.get("ocr_meaningful"),
            mime=data.get("mime"),
            width=data.get("width"),
            height=data.get("height"),
        )


_BLOCK_CLASSES: dict[BlockType, type[Block]] = {
    BlockType.HEADING: HeadingBlock,
    BlockType.PARAGRAPH: ParagraphBlock,
    BlockType.TABLE: TableBlock,
    BlockType.IMAGE: ImageBlock,
}


# ---------------------------------------------------------------------------
# Table cell content
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    """Content of one table cell.

    A cell body is structurally the same as a document body: an ordered list of
    blocks. So a cell can legally hold a paragraph, a nested `TableBlock`, or a
    paragraph+table+paragraph sequence — nothing about a cell is "text only".
    Merge-covered slots are represented as None in `TableData.cells`, never as a
    `Cell`.

    Text-only consumers must not walk `blocks`; call `plain_text()` instead — it
    is the single place that decides how a nested table degrades to text.
    """

    blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"blocks": [b.to_dict() for b in self.blocks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cell":
        return cls(blocks=[Block.from_dict(b) for b in data.get("blocks", [])])

    def plain_text(self) -> str:
        """Flat text view of the cell.

        Paragraph/heading blocks contribute their text; a nested table is rendered
        inline (GitHub-flavored markdown, or `Header: value` pairs when it itself
        contains a table); an image contributes its `<imageN>` marker. Blocks are
        joined with newlines. This is the ONLY answer to "what if the cell holds a
        table?" for text-only consumers.
        """
        parts: list[str] = []
        images: list[ImageBlock] = []
        for b in self.blocks:
            if isinstance(b, TableBlock):
                parts.append(_render_cell_table(b.table))
            elif isinstance(b, ImageBlock):
                images.append(b)  # deferred: its marker is usually already in text
            else:  # ParagraphBlock / HeadingBlock
                parts.append(getattr(b, "text", ""))
        text = "\n".join(p for p in parts if p)
        for img in images:  # only surface a marker the paragraph text didn't carry
            if img.marker not in text:
                text = f"{text}\n{img.marker}" if text else img.marker
        return text


def text_cell(text: str | None) -> "Cell | None":
    """Wrap plain text as a `Cell`. None (merge-covered/absent) stays None; an
    empty string becomes an empty cell (no paragraph)."""
    if text is None:
        return None
    return Cell(blocks=[ParagraphBlock(id="", text=text)] if text else [])


def _cell_from_dict(data: Any) -> "Cell | None":
    """Deserialize one cell slot, tolerant of the v1 schema (a bare string)."""
    if data is None:
        return None
    if isinstance(data, str):  # legacy v1: cell was a plain string
        return text_cell(data)
    return Cell.from_dict(data)


def _render_cell_table(table: "TableData") -> str:
    """Inline text rendering of a table found inside a cell."""
    if _table_has_subtable(table):
        return _render_header_value(table)  # markdown can't nest a table
    return _render_gfm(table)


def _table_has_subtable(table: "TableData") -> bool:
    return any(
        isinstance(b, TableBlock)
        for row in table.cells for c in row if c is not None
        for b in c.blocks
    )


def _cell_text(c: "Cell | None") -> str:
    return "" if c is None else c.plain_text().replace("\n", " ").strip()


def _render_gfm(table: "TableData") -> str:
    rows = table.cells
    if not rows:
        return ""
    out = ["| " + " | ".join(_cell_text(c).replace("|", r"\|") for c in rows[0]) + " |",
           "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for row in rows[1:]:
        out.append("| " + " | ".join(_cell_text(c).replace("|", r"\|") for c in row) + " |")
    return "\n".join(out)


def _render_header_value(table: "TableData") -> str:
    rows = table.cells
    if not rows:
        return ""
    headers = [_cell_text(c) for c in rows[0]]
    groups: list[str] = []
    for row in rows[1:]:
        pairs = []
        for i, c in enumerate(row):
            head = headers[i] if i < len(headers) and headers[i] else f"col{i}"
            # a cell may itself contain a table -> plain_text recurses one level down
            val = "" if c is None else c.plain_text()
            pairs.append(f"{head}: {val}")
        groups.append("\n".join(pairs))
    return "\n\n".join(groups)


# ---------------------------------------------------------------------------
# Document (IR)
# ---------------------------------------------------------------------------


@dataclass
class ParsedDocument:
    """Parser output: the common intermediate representation (IR).

    doc_id
        Document identifier (assigned by the pipeline). `storage/output/{doc_id}.json`.
    source_path
        Path/name of the raw input under `storage/raw/` (provenance).
    fmt
        Format tag: "docx" | "pptx" | "xlsx" | "html" | "pdf" | "markdown".
    raw_sha256
        Hash of the raw file (document-level provenance/dedup).
    access_level
        Access level; images inherit this (see images/).
    blocks
        Ordered list of blocks (document reading order).
    """

    doc_id: str
    source_path: str
    fmt: str
    raw_sha256: str | None = None
    mimetype: str | None = None
    page_count: int | None = None
    access_level: str | None = None
    parser_version: str | None = None
    ir_version: int = IR_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)

    # -- Convenience accessors ------------------------------------------------

    def images(self) -> list[ImageBlock]:
        return [b for b in self.blocks if isinstance(b, ImageBlock)]

    def tables(self) -> list[TableBlock]:
        return [b for b in self.blocks if isinstance(b, TableBlock)]

    def block_by_id(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def list_items(self, list_id: str) -> list[Block]:
        """Blocks belonging to one list (reconstructs a list from `list_id`)."""
        return [b for b in self.blocks if b.list_id == list_id]

    # -- Serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ir_version": self.ir_version,
            "doc_id": self.doc_id,
            "source_path": self.source_path,
            "fmt": self.fmt,
            "blocks": [b.to_dict() for b in self.blocks],
        }
        for key in ("raw_sha256", "mimetype", "page_count", "access_level",
                    "parser_version"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedDocument":
        return cls(
            doc_id=data["doc_id"],
            source_path=data["source_path"],
            fmt=data["fmt"],
            raw_sha256=data.get("raw_sha256"),
            mimetype=data.get("mimetype"),
            page_count=data.get("page_count"),
            access_level=data.get("access_level"),
            parser_version=data.get("parser_version"),
            ir_version=data.get("ir_version", IR_VERSION),
            metadata=data.get("metadata", {}),
            blocks=[Block.from_dict(b) for b in data.get("blocks", [])],
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "ParsedDocument":
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "ParsedDocument":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Parser contract
# ---------------------------------------------------------------------------


class BaseParser(ABC):
    """Abstract interface every format parser conforms to.

    A parser has a single responsibility: convert a raw file into the
    `ParsedDocument` IR. Enrichments like OCR, table description, and LLM checks
    are the job of later stages; the parser leaves those fields None.

    Subclasses fill the `extensions`/`mimetypes` class variables and the `parse()`
    method. `registry.py` uses `supports()` for selection.
    """

    #: File extensions this parser handles (with dot, lowercase): (".docx",)
    extensions: tuple[str, ...] = ()
    #: Mimetypes handled.
    mimetypes: tuple[str, ...] = ()
    #: Format tag (ParsedDocument.fmt).
    fmt: str = ""

    @abstractmethod
    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        """Convert the raw file at `raw_path` into IR. `doc_id` comes from the pipeline."""
        raise NotImplementedError

    @classmethod
    def supports(cls, *, extension: str | None = None,
                 mimetype: str | None = None) -> bool:
        """Is this extension or mimetype supported by this parser?"""
        if extension and extension.lower() in cls.extensions:
            return True
        if mimetype and mimetype in cls.mimetypes:
            return True
        return False
