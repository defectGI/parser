"""BaseParser interface and ParsedDocument IR definition.

This module is the parser's single "contract" file: every format parser produces
a `ParsedDocument` (IR), and all later stages (images/, tables/, webapp/) read and
enrich this IR. The IR is stored as JSON under `storage/parsed/*.json`.

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
  the IR; the LLM-check status / retry counter lives in `storage/db/` (not here).
* Lists are NOT a container block. The IR stays a flat block stream; list membership
  is metadata on ordinary blocks (`list_id`/`list_level`/`list_ordered`). This mirrors
  how docx (w:numId/w:ilvl) and pdf natively store lists, and keeps block-level content
  inside a list item (a table/image in a `<li>`) as a real TableBlock/ImageBlock instead
  of flattening it away. A "list" is reconstructed by grouping blocks that share a
  `list_id`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# IR schema version. Bump when the contract changes; readers migrate accordingly.
IR_VERSION = 1


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
        Row-major 2D matrix; each cell is text or None (cells covered by a merge).
        `n_rows`/`n_cols` are the logical dimensions.
    merges
        Merge regions; the top-left cell carries the content.
    """

    n_rows: int
    n_cols: int
    cells: list[list[str | None]] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "cells": self.cells,
            "merges": [m.to_dict() for m in self.merges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableData":
        return cls(
            n_rows=data["n_rows"],
            n_cols=data["n_cols"],
            cells=data.get("cells", []),
            merges=[Merge.from_dict(m) for m in data.get("merges", [])],
        )


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
    """

    id: str
    span: Span = field(default_factory=Span)

    list_id: str | None = None
    list_level: int | None = None
    list_ordered: bool | None = None

    # Subclasses override this.
    type: BlockType = field(init=False, default=BlockType.PARAGRAPH)

    def _base_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type.value, "id": self.id}
        span = self.span.to_dict()
        if span:
            d["span"] = span
        for key in ("list_id", "list_level", "list_ordered"):
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

    type: BlockType = field(init=False, default=BlockType.HEADING)

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "text": self.text, "level": self.level}

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "HeadingBlock":
        return cls(**cls._base_kwargs(data),
                   text=data.get("text", ""), level=data.get("level", 1))


@dataclass
class ParagraphBlock(Block):
    text: str = ""

    type: BlockType = field(init=False, default=BlockType.PARAGRAPH)

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "text": self.text}

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "ParagraphBlock":
        return cls(**cls._base_kwargs(data), text=data.get("text", ""))


@dataclass
class TableBlock(Block):
    table: TableData = field(default_factory=lambda: TableData(0, 0))
    # Filled by tables/table_describe.py; None at parse time.
    table_description: str | None = None

    type: BlockType = field(init=False, default=BlockType.TABLE)

    def to_dict(self) -> dict[str, Any]:
        d = {**self._base_dict(), "table": self.table.to_dict()}
        if self.table_description is not None:
            d["table_description"] = self.table_description
        return d

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "TableBlock":
        return cls(**cls._base_kwargs(data),
                   table=TableData.from_dict(data["table"]),
                   table_description=data.get("table_description"))


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
# Document (IR)
# ---------------------------------------------------------------------------


@dataclass
class ParsedDocument:
    """Parser output: the common intermediate representation (IR).

    doc_id
        Document identifier (assigned by the pipeline). `storage/parsed/{doc_id}.json`.
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
