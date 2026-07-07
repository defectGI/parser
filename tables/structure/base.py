"""Provider-agnostic table STRUCTURE interface.

Mirrors llm/base.py's shape: the rest of the codebase talks to a structure
model only through `TableStructureClient` and the two neutral dataclasses
below. No adapter's own third-party types (e.g. IBM's docling-ibm-models
output objects) ever leak past this module -- each adapter translates into
`DetectedTable`/`DetectedCell` internally and nothing else.

This is a STRUCTURE-only contract: a client returns the table's row/column
grid and cell bounding boxes, never cell text. The caller (parsers/pdf_parser.py)
pulls the actual text for each cell straight from the PDF's own digital text
layer via PyMuPDF -- "structure from the model, text from the code" -- so a
misread digit/letter in the model's own OCR can never corrupt table content.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class TableStructureError(Exception):
    """Any failure while talking to a table-structure model (transport, bad
    payload, model not loaded)."""


@dataclass
class TextCellHint:
    """One piece of digital text already extracted from the PDF (e.g. one of
    pdfplumber's `extract_words()` entries), in the same PIXEL coordinate
    space as the image passed to `detect()`.

    Some structure models (IBM's TableFormer in particular) don't work from
    the image alone -- they match their own visual structure prediction
    against real text cells to place them in the grid, and expect those text
    cells as an input rather than reading them off the bitmap themselves.
    This hint list lets a caller hand over text it already has (for free,
    it already ran the PDF's text extraction) instead of the model having to
    re-derive it via its own OCR. Purely optional: an adapter that has no use
    for it (e.g. a pure vision-only model behind an HTTP endpoint) ignores it.
    """

    text: str
    bbox: tuple[float, float, float, float]


@dataclass
class DetectedCell:
    """One cell of a detected table grid.

    `bbox` is (x0, top, x1, bottom) in PIXEL coordinates of the image passed
    to `TableStructureClient.detect()` -- the caller converts to PDF point
    coordinates before extracting text (see pdf_parser.py's
    `_digital_text_in_bbox`).
    """

    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


@dataclass
class DetectedTable:
    """One detected table region: its own bbox plus every cell in its grid."""

    bbox: tuple[float, float, float, float]
    cells: list[DetectedCell] = field(default_factory=list)


@runtime_checkable
class TableStructureClient(Protocol):
    """Image-in / structure-out contract.

    `image` is the page rendered to bytes (e.g. PNG, same rendering
    pdf_parser.py already produces for its VLM calls).

    `table_bboxes` are candidate table REGIONS on that page -- (x0, top, x1,
    bottom) in the same pixel space as `image` -- typically from a
    line-geometry detector like pdfplumber's `find_tables()`. Some structure
    models (TableFormer in particular) don't discover table regions
    themselves; they refine the row/column grid *within* a region they're
    told about. Pass `()` for a model that does its own region discovery
    (e.g. a full layout model behind an HTTP endpoint). Returned tables
    correspond positionally to the given `table_bboxes` when both are
    non-empty (same order, same length) -- a model that discovers its own
    regions instead just returns whatever it found.

    `text_cells` is an optional, best-effort list of already-extracted
    digital text the caller happens to have (see `TextCellHint`) -- pass
    `()` if you have none; adapters that don't need it simply ignore the
    argument.

    Raises `TableStructureError` on failure so callers can fall back to a
    line-geometry detector instead of failing the whole document.
    """

    def detect(self, image: bytes, *,
               table_bboxes: Sequence[tuple[float, float, float, float]] = (),
               text_cells: Sequence[TextCellHint] = ()) -> list[DetectedTable]:
        ...
