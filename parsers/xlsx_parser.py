"""XLSX -> ParsedDocument.

Uses openpyxl. Each worksheet becomes a HeadingBlock (sheet name) followed by one
TableBlock covering the sheet's used range. Merged cell ranges map directly to
`TableData.merges`; openpyxl already reports covered cells as None, matching the IR
convention.

Byte offsets (Karar B, best-effort): the TableBlock's Span points at the
`<sheetData>` element inside `xl/worksheets/sheetN.xml` when that part can be
located and parsed; otherwise the byte offsets are left unset.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import openpyxl

from .base import (
    BaseParser, ParsedDocument, Span, HeadingBlock, TableBlock, TableData, Merge,
)
from ._ooxml import element_byte_range

_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _cell(value) -> str | None:
    if value is None:
        return None
    return str(value)


class XlsxParser(BaseParser):
    extensions = (".xlsx",)
    mimetypes = (_MIME,)
    fmt = "xlsx"
    version = "xlsx/0.1"

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        raw_path = Path(raw_path)
        data = raw_path.read_bytes()
        raw_sha256 = hashlib.sha256(data).hexdigest()

        wb = openpyxl.load_workbook(raw_path, data_only=True)
        # Raw sheet XML parts, in workbook order, for best-effort byte offsets.
        sheet_spans = self._sheet_spans(raw_path)

        blocks: list = []
        bid = 0

        def next_id() -> str:
            nonlocal bid
            s = f"b{bid}"
            bid += 1
            return s

        for idx, ws in enumerate(wb.worksheets):
            if ws.max_row is None or ws.max_column is None:
                continue  # empty sheet
            min_r, min_c = ws.min_row, ws.min_column
            max_r, max_c = ws.max_row, ws.max_column
            rows = [
                [_cell(v) for v in row]
                for row in ws.iter_rows(min_row=min_r, max_row=max_r,
                                        min_col=min_c, max_col=max_c,
                                        values_only=True)
            ]
            if not rows or all(c is None for r in rows for c in r):
                continue  # no data

            merges = []
            for rng in ws.merged_cells.ranges:
                merges.append(Merge(
                    row=rng.min_row - min_r, col=rng.min_col - min_c,
                    rowspan=rng.max_row - rng.min_row + 1,
                    colspan=rng.max_col - rng.min_col + 1))

            table = TableData(n_rows=len(rows), n_cols=max_c - min_c + 1,
                              cells=rows, merges=merges)

            blocks.append(HeadingBlock(id=next_id(), text=ws.title, level=1))
            part, brange = sheet_spans.get(idx, (None, None))
            span = Span(part=part,
                        byte_start=brange[0] if brange else None,
                        byte_end=brange[1] if brange else None)
            blocks.append(TableBlock(id=next_id(), span=span, table=table))

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype=_MIME,
            parser_version=self.version,
            metadata={"sheets": [ws.title for ws in wb.worksheets]},
            blocks=blocks,
        )

    @staticmethod
    def _sheet_spans(raw_path: Path) -> dict[int, tuple[str, tuple[int, int] | None]]:
        """Best-effort: map worksheet index -> (part_name, sheetData byte range).

        Sheets are matched positionally to xl/worksheets/sheetN.xml. This is a
        heuristic (reordered/deleted sheets can shift it), so failures just yield
        no byte offsets rather than wrong ones.
        """
        out: dict[int, tuple[str, tuple[int, int] | None]] = {}
        try:
            with zipfile.ZipFile(raw_path) as z:
                names = sorted(
                    n for n in z.namelist()
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
                for idx, name in enumerate(names):
                    brange = element_byte_range(z.read(name), "sheetData")
                    out[idx] = (name, brange)
        except (zipfile.BadZipFile, KeyError, OSError):
            pass
        return out
