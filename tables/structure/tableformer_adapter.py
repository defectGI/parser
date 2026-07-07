"""IBM TableFormer adapter for TableStructureClient.

Isolation boundary: this is the ONLY file in the repo that imports
`docling_ibm_models` (IBM's TableFormer inference package, PyPI:
docling-ibm-models). Every import is lazy (inside `_ensure_loaded`), so the
rest of the codebase -- and anyone not using provider="tableformer" -- never
needs docling-ibm-models or its own heavy dependencies (torch, opencv, ...)
installed at all. Nothing outside this file ever sees IBM's own types; the
public surface is `detect()` -> `DetectedTable`/`DetectedCell` (base.py),
same as every other adapter.

Confirmed from docling-ibm-models (github.com/docling-project/docling-ibm-models,
docling_ibm_models/tableformer/data_management/tf_predictor.py, and its own
tests/test_tf_predictor.py) as of 2026-07:
    - Model weights + a matching `tm_config.json` are downloaded on first use
      from the `ds4sd/docling-models` HF repo (huggingface_hub, already a
      transitive dependency of docling-ibm-models -- no extra install).
    - `TFPredictor(config, device="cpu", num_threads=4)`; `config` is exactly
      the downloaded `tm_config.json`, with `config["model"]["save_dir"]` set
      to the directory it lives in (that's where the `.safetensors` weights
      are too).
    - `predictor.multi_table_predict(iocr_page, table_bboxes, do_matching=True,
      correct_overlapping_cells=False, sort_row_col_indexes=True)` where:
        iocr_page = {"image": <page as a cv2/BGR ndarray>,
                     "tokens": [{"id": int, "text": str, "bbox": [l,t,r,b]}, ...],
                     "width": int, "height": int}
        table_bboxes = [[x0, y0, x1, y1], ...]  # candidate table REGIONS on
                                                  # the page -- TableFormer
                                                  # refines the grid inside a
                                                  # region, it doesn't find
                                                  # regions itself
      returns a list (one entry per table_bbox, same order) of
      {"tf_responses": [...], "predict_details": {...}}; each tf_response is
      a cell dict with bbox (dict with l/t/r/b), row_span, col_span,
      start_row_offset_idx, end_row_offset_idx, start_col_offset_idx,
      end_col_offset_idx.

*** One thing to verify empirically on first real run (couldn't run actual
inference while writing this -- see plan doc / conversation) ***: whether
`iocr_page["tokens"]`/`table_bboxes` bbox coordinates are top-left-origin
image-pixel space (matching this repo's `TextCellHint`/rendered-PNG
convention, used here) or bottom-left-origin PDF-point space (per a stray
docstring in tf_cell_matcher.py's CellMatcher class that describes an older
internal representation). If detected tables land vertically mirrored,
flip the y-axis here (`y' = page_height - y`) before calling
multi_table_predict, and un-flip the returned cell bboxes the same way --
that's the only change this bug would need, isolated to this file.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from .base import DetectedCell, DetectedTable, TableStructureError, TextCellHint


class TableFormerAdapter:
    """provider="tableformer". `model` selects the TableFormer variant --
    "fast" (default) or "accurate" (docling's own TableFormerMode split) --
    passed straight through to pick which HF-downloaded config/weights to use."""

    def __init__(self, model: str | None = None, device: str = "cpu") -> None:
        self._variant = model or "fast"
        self._device = device
        self._predictor = None  # lazy

    def _ensure_loaded(self):
        if self._predictor is not None:
            return self._predictor

        try:
            from docling_ibm_models.tableformer.data_management.tf_predictor import (
                TFPredictor,
            )
        except ImportError as exc:
            raise TableStructureError(
                "provider='tableformer' requires the 'docling-ibm-models' "
                "package (pip install docling-ibm-models)"
            ) from exc

        try:
            from huggingface_hub import snapshot_download

            download_path = snapshot_download(
                repo_id="ds4sd/docling-models", revision="v2.1.0",
                allow_patterns=[f"model_artifacts/tableformer/{self._variant}/*"],
            )
        except Exception as exc:  # noqa: BLE001
            raise TableStructureError(
                f"failed to download TableFormer weights: {exc}") from exc

        save_dir = os.path.join(
            download_path, "model_artifacts", "tableformer", self._variant)
        config_path = os.path.join(save_dir, "tm_config.json")
        try:
            import json

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except OSError as exc:
            raise TableStructureError(
                f"TableFormer config not found at {config_path}: {exc}") from exc
        config["model"]["save_dir"] = save_dir

        try:
            self._predictor = TFPredictor(config, device=self._device)
        except Exception as exc:  # noqa: BLE001
            raise TableStructureError(
                f"failed to load TableFormer model: {exc}") from exc
        return self._predictor

    def detect(self, image: bytes, *,
               table_bboxes: Sequence[tuple[float, float, float, float]] = (),
               text_cells: Sequence[TextCellHint] = ()) -> list[DetectedTable]:
        if not table_bboxes:
            # TableFormer refines a grid WITHIN a given region; it doesn't
            # locate tables on its own, so with no candidate regions there
            # is nothing for it to do (see base.py's TableStructureClient
            # docstring).
            return []

        predictor = self._ensure_loaded()

        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise TableStructureError(
                "provider='tableformer' requires opencv-python and numpy "
                "(installed automatically with docling-ibm-models)"
            ) from exc

        arr = np.frombuffer(image, dtype=np.uint8)
        page_image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if page_image is None:
            raise TableStructureError("could not decode page image for TableFormer")
        height, width = page_image.shape[:2]

        tokens = [
            {"id": i, "text": t.text, "bbox": list(t.bbox)}
            for i, t in enumerate(text_cells)
        ]
        iocr_page = {
            "image": page_image,
            "tokens": tokens,
            "width": width,
            "height": height,
        }
        bboxes = [list(b) for b in table_bboxes]  # multi_table_predict mutates in place

        try:
            multi_tf_output = predictor.multi_table_predict(
                iocr_page, bboxes, do_matching=True,
                correct_overlapping_cells=False, sort_row_col_indexes=True)
        except Exception as exc:  # noqa: BLE001
            raise TableStructureError(f"TableFormer inference failed: {exc}") from exc

        tables: list[DetectedTable] = []
        for region_bbox, out in zip(table_bboxes, multi_tf_output):
            cells = _cells_from_tf_responses(out.get("tf_responses") or [])
            tables.append(DetectedTable(bbox=tuple(region_bbox), cells=cells))
        return tables


def _cells_from_tf_responses(tf_responses: list[dict]) -> list[DetectedCell]:
    """TFPredictor's flat cell-response list -> DetectedCell -- field names
    per tf_predictor.py's _generate_tf_response (bbox.l/t/r/b, row_span,
    col_span, start/end_row/col_offset_idx), confirmed against the real
    installed package source."""
    cells: list[DetectedCell] = []
    for c in tf_responses:
        bbox = c.get("bbox") or {}
        if not bbox:
            continue
        cells.append(DetectedCell(
            row=c["start_row_offset_idx"],
            col=c["start_col_offset_idx"],
            rowspan=max(1, c["end_row_offset_idx"] - c["start_row_offset_idx"]),
            colspan=max(1, c["end_col_offset_idx"] - c["start_col_offset_idx"]),
            bbox=(bbox["l"], bbox["t"], bbox["r"], bbox["b"]),
        ))
    return cells
