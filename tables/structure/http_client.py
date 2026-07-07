"""Generic HTTP adapter for TableStructureClient.

Talks to any HTTP endpoint that accepts a page image and returns table
structure as JSON -- this is how a hosted/self-wrapped model (TableFormer
behind your own FastAPI service, a future model, ...) plugs in without this
repo ever depending on that model's own library. Implemented with stdlib
`urllib`, same as llm/openai_compat.py, so no extra HTTP dependency.

Request:  POST {base_url}  {"image_base64": "<base64-encoded image bytes>",
                            "table_bboxes": [[x0, top, x1, bottom], ...],
                            "text_cells": [{"text": "...", "bbox": [x0, top, x1, bottom]}, ...]}
Response: {"tables": [{"bbox": [x0, top, x1, bottom],
                        "cells": [{"row": 0, "col": 0, "rowspan": 1,
                                   "colspan": 1, "bbox": [x0, top, x1, bottom]},
                                  ...]},
                       ...]}
All bboxes are pixel coordinates of the image that was sent. `table_bboxes`
and `text_cells` are sent along for models that want them (see base.py's
TableStructureClient docstring) -- a server that does its own region
discovery/OCR just ignores whichever fields it doesn't need.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from .base import DetectedCell, DetectedTable, TableStructureError, TextCellHint


class HttpTableStructureClient:
    """POSTs a page image to `base_url`, parses the documented JSON contract
    above into `DetectedTable`/`DetectedCell` -- no model-specific types."""

    def __init__(self, *, base_url: str, api_key: str | None = None,
                timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def detect(self, image: bytes, *,
               table_bboxes: Sequence[tuple[float, float, float, float]] = (),
               text_cells: Sequence[TextCellHint] = ()) -> list[DetectedTable]:
        body = json.dumps({
            "image_base64": base64.b64encode(image).decode("ascii"),
            "table_bboxes": [list(b) for b in table_bboxes],
            "text_cells": [{"text": t.text, "bbox": list(t.bbox)} for t in text_cells],
        }).encode("utf-8")

        req = urllib.request.Request(self.base_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise TableStructureError(
                f"HTTP {exc.code} from {self.base_url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TableStructureError(f"cannot reach {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TableStructureError(
                f"non-JSON response from {self.base_url}: {exc}") from exc

        try:
            return [
                DetectedTable(
                    bbox=tuple(t["bbox"]),
                    cells=[
                        DetectedCell(
                            row=c["row"], col=c["col"],
                            rowspan=c.get("rowspan", 1),
                            colspan=c.get("colspan", 1),
                            bbox=tuple(c["bbox"]),
                        )
                        for c in t.get("cells", [])
                    ],
                )
                for t in payload["tables"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise TableStructureError(
                f"unexpected response shape: {payload!r:.500}") from exc
