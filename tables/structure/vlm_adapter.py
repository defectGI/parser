"""VLM-based table structure adapter for TableStructureClient.

Uses whatever vision-language model is already configured for the rest of
the pipeline (llm.get_vlm_client() -- the same VLM_* env vars that drive OCR
in images/image_handler.py and the hybrid PDF path) to infer a table's
row/column grid, instead of a dedicated structure-recognition model like
TableFormer.

No extra dependency at all beyond what the parser already needs (llm/ is
stdlib-urllib-only, Pillow is already a parser dependency) -- this is the
"don't want a heavy install (torch/docling-ibm-models)" alternative: works
with any OpenAI-compatible VLM you already run (a local Ollama model, a
hosted API, ...), at the cost of being less numerically precise about cell
boundaries than a purpose-built structure model, since general-purpose VLMs
are not as reliable at pixel-exact grounding as TableFormer is. Cell TEXT is
still always pulled from the PDF's own digital layer regardless (see
pdf_parser.py's _digital_text_in_bbox) -- only the grid geometry comes from
the model here, same "structure from the model, text from the code" split
as every other adapter.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Sequence

from .base import DetectedCell, DetectedTable, TableStructureError, TextCellHint

_SYSTEM = (
    "You are given a cropped image of a single table from a document page. "
    "Describe its grid structure as a JSON array of cells, nothing else.\n"
    'Each cell: {"row": int, "col": int, "rowspan": int, "colspan": int, '
    '"bbox": [x0, y0, x1, y1]}\n'
    "row/col are 0-indexed grid positions. bbox is the cell's position as "
    "FRACTIONS of the image width/height (0.0 to 1.0, x0<x1, y0<y1, origin "
    "top-left) -- not pixels, not the cell's text content.\n"
    "A merged cell gets exactly one entry with rowspan/colspan > 1; do not "
    "add separate entries for the grid positions it covers.\n"
    "Output ONLY the JSON array -- no explanation, no markdown fence, no "
    "cell text."
)


class VLMStructureAdapter:
    """provider="vlm". Reuses the already-configured VLM
    (llm.get_vlm_client(), same provider/base_url/api_key as VLM_*) by
    default. `model` (TABLE_STRUCT_MODEL) overrides just the model id: grid/
    bbox extraction is a different, arguably harder task than that role's
    usual OCR transcription job, so a model that's not necessarily the best
    OCR reader (but reads structure/layout well) may be worth pointing at
    separately -- same Ollama/hosted server, different model pulled there.
    Leave unset to reuse VLM_MODEL as-is. `vlm_client` overrides the client
    entirely (tests inject a fake this way)."""

    def __init__(self, vlm_client=None, model: str | None = None) -> None:
        self._vlm = vlm_client
        self._model = model

    def _ensure_client(self):
        if self._vlm is None:
            from llm import LLMError, get_vlm_client

            try:
                self._vlm = get_vlm_client(model=self._model)
            except LLMError as exc:
                raise TableStructureError(f"no VLM configured: {exc}") from exc
        return self._vlm

    def detect(self, image: bytes, *,
               table_bboxes: Sequence[tuple[float, float, float, float]] = (),
               text_cells: Sequence[TextCellHint] = ()) -> list[DetectedTable]:
        if not table_bboxes:
            # Same contract as every adapter: this reads a grid within a
            # given region, it doesn't hunt for tables on the page itself.
            return []

        vlm = self._ensure_client()

        try:
            from PIL import Image
        except ImportError as exc:
            raise TableStructureError(
                "provider='vlm' requires Pillow (already a parser dependency)"
            ) from exc

        try:
            page_img = Image.open(io.BytesIO(image)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise TableStructureError(f"could not decode page image: {exc}") from exc

        tables: list[DetectedTable] = []
        for bbox in table_bboxes:
            x0, y0, x1, y1 = bbox
            crop = page_img.crop((max(0, int(x0)), max(0, int(y0)), int(x1), int(y1)))
            cw, ch = crop.size
            if cw <= 0 or ch <= 0:
                continue

            buf = io.BytesIO()
            crop.save(buf, format="PNG")

            try:
                raw = vlm.complete_vision(
                    system=_SYSTEM, user="Describe this table's grid.",
                    images=[("image/png", buf.getvalue())], max_tokens=4096)
            except Exception as exc:  # noqa: BLE001
                raise TableStructureError(f"VLM structure call failed: {exc}") from exc

            cell_specs = _parse_cells_json(raw)
            if cell_specs is None:
                # Unparseable response (not valid/expected JSON) is a model
                # failure, not "no table here" -- raise so the caller falls
                # back to pdfplumber's own geometry for the whole page
                # instead of silently dropping a table pdfplumber DID find.
                raise TableStructureError(
                    f"VLM returned an unparseable structure response: {raw[:200]!r}")
            if not cell_specs:
                continue  # valid response, model explicitly found no cells here

            cells = [
                DetectedCell(
                    row=c["row"], col=c["col"],
                    rowspan=c["rowspan"], colspan=c["colspan"],
                    bbox=(x0 + c["bbox"][0] * cw, y0 + c["bbox"][1] * ch,
                         x0 + c["bbox"][2] * cw, y0 + c["bbox"][3] * ch),
                )
                for c in cell_specs
            ]
            tables.append(DetectedTable(bbox=tuple(bbox), cells=cells))
        return tables


def _parse_cells_json(raw: str | None) -> list[dict] | None:
    """Tolerant JSON-array extraction -- the same fenced-code-block
    tolerance as parsers/pdf_parser.py's own _parse_vlm_blocks, duplicated
    (not imported) since tables/structure/ deliberately doesn't depend on
    parsers/."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            row, col = int(item["row"]), int(item["col"])
            bbox = [float(v) for v in item["bbox"]]
            if len(bbox) != 4:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "row": row, "col": col,
            "rowspan": max(1, int(item.get("rowspan", 1) or 1)),
            "colspan": max(1, int(item.get("colspan", 1) or 1)),
            "bbox": bbox,
        })
    return out
