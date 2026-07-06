"""PDF -> ParsedDocument.

Unlike the other formats, a PDF has no single lossless path. Each page is
triaged and routed to one of three strategies (see parsers/README.md,
"PDF pipeline"):

* code path    born-digital, simple layout: pdfplumber only. Lossless,
               deterministic, no model involved.
* hybrid path  born-digital but complex (tables / multi-column): the page is
               rendered and read by a VLM with the page's own text layer given
               as grounding; every VLM block is then verified against that
               text layer.
* scanned path no usable text layer: render -> independent text detector
               (bboxes + text) -> VLM read -> every VLM line is cross-checked
               against the detector; suspicious lines are crop-and-reread by a
               second, independent VLM.

Provenance: every text-bearing block gets `Block.provenance`:
    "text-layer-verified"   content matched the PDF's own text layer
    "consensus-verified"    two independent readers agreed (VLM + detector /
                            second VLM)
    "unverified"            could not be independently confirmed; the block's
                            `source_crop` holds the sha256 of a page/region
                            render stored under `storage/images/` so a human
                            (or the citation pipeline) can audit the claim.
Verification never auto-resolves a disagreement: a block that two sources
dispute stays "unverified" with its crop, it is not silently "fixed".

Model access is provider-agnostic: everything goes through `llm.VLMClient`
(`llm.get_vlm_client()`); the parser never names a provider or model. With no
VLM configured the parser degrades gracefully: hybrid pages fall back to the
code path (their text layer is real, so the result is still honest), scanned
pages produce a full-page ImageBlock for the images/ OCR stage to pick up.

The scanned-path text detector is likewise pluggable (`TextDetector`
protocol); the default uses pytesseract when it is importable and silently
disappears when it is not (verification then leans on the second VLM alone).

Known v1 limitations: on hybrid and scanned pages alike, a VLM "table" spec
is paired with a pdfplumber-detected table positionally (i-th VLM table <->
i-th geometric table, both in reading order) since the VLM returns no
coordinates -- a page where the model over/under-counts tables falls back to
its own ungrounded grid for the unpaired ones; a VLM "figure" spec is paired
the same way with pdfplumber's embedded raster objects, giving the figure
both a true reading-order position and a geometric bbox when the counts line
up (an unpaired spec still becomes an ImageBlock, just unverified with an
audit crop -- tight to the VLM's own bbox guess when it gave one, else the
full page; an unpaired raster object is still appended, just without a
position). On scanned pages the raster that IS the scan itself (spanning
most of the page) is excluded from that pairing so it can't masquerade as a
verified figure. Inline formatting `runs` (bold/italic) are filled on the code
path from each word's own font name (e.g. "Arial-BoldMT") -- the PDF
equivalent of reading a docx run's rPr, deterministic and not a VLM guess.
Hybrid/scanned pages (VLM-read) do not fill `runs`: there is no independent
source to verify a bold/italic claim against, unlike text content itself.

Environment (all optional):
    PDF_VLM             "0" disables all VLM use (deterministic fallbacks only)
    PDF_RENDER_DPI      page render resolution for VLM/crops (default 150)
    PDF_CROP_DIR        blob dir for audit crops (default storage/images)
    PDF_VLM_MAX_TOKENS  token budget for a page read (default 8192)
    PDF_CONTAINMENT     token containment threshold, 0..1 (default 0.9)
    PDF_LINE_MATCH      per-line consensus similarity threshold (default 0.8)
    PDF_TESSERACT_LANG  language(s) for the default detector (e.g. "tur+eng")
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Protocol

import pdfplumber
from PIL import Image

from llm import LLMError, VLMClient, get_vlm_client

from .base import (
    BaseParser, ParsedDocument, Span, text_cell,
    Block, HeadingBlock, ImageBlock, ParagraphBlock, TableBlock, TableData,
    Merge, Mark, InlineRun, finalize_runs, runs_have_marks,
)

PROV_TEXT_LAYER = "text-layer-verified"
PROV_CONSENSUS = "consensus-verified"
PROV_UNVERIFIED = "unverified"

_UNSET = object()  # sentinel: "resolve from environment lazily"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return float(val)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Text normalization + fuzzy matching (pure helpers)
# ---------------------------------------------------------------------------

_PUNCT_EDGES = ".,;:!?()[]{}\"'`«»"


def _norm(text: str) -> str:
    """Whitespace-collapsed, casefolded view used for all comparisons."""
    return " ".join(text.split()).casefold()


def _tok_list(text: str) -> list[str]:
    """Normalized content tokens: edge punctuation stripped, pure-symbol
    tokens dropped (e.g. a bare "=", "/", "-", math arrows "→"/"↑").

    Two independent transcriptions of the same math/formula line routinely
    disagree on operator glyphs (→ vs ->, − vs -) while agreeing on every
    letter and digit; counting those glyphs as content tokens dragged short
    formula lines below the containment threshold even when fully correct.
    Digits/letters still need to match — this only drops tokens that carry
    no alphanumeric content, so exact-number verification (`_criticals`) is
    unaffected.
    """
    out = []
    for t in _norm(text).split():
        t = t.strip(_PUNCT_EDGES)
        if t and any(ch.isalnum() for ch in t):
            out.append(t)
    return out


def _counter(tokens: list[str]) -> Counter:
    return Counter(tokens)


def _containment(needle: list[str], hay: Counter) -> float:
    """Fraction of `needle` tokens present in `hay` (multiset semantics).

    Order-insensitive on purpose: the VLM may reflow line breaks or column
    order while still reading the same characters.
    """
    if not needle:
        return 1.0
    need = Counter(needle)
    found = sum(min(n, hay.get(t, 0)) for t, n in need.items())
    return found / sum(need.values())


def _criticals(text: str) -> Counter:
    """Digit-bearing tokens (numbers, dates, part numbers, units with counts).

    These are the tokens a hallucination hurts most, so they must match their
    independent source EXACTLY — fuzzy similarity is not enough for "500" vs
    "600".
    """
    return Counter(t for t in _tok_list(text) if any(ch.isdigit() for ch in t))


def _crit_ok(text: str, source_crit: Counter) -> bool:
    """Every critical token of `text` appears in the source (multiset)."""
    return not (_criticals(text) - source_crit)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ---------------------------------------------------------------------------
# VLM page reading: prompts + output parsing
# ---------------------------------------------------------------------------

_PAGE_SYSTEM = (
    "You transcribe one page of a document into structured blocks.\n"
    "Return ONLY a JSON array — no prose, no markdown fences. Each element is "
    "one of:\n"
    '  {"type": "heading", "level": 1, "text": "..."}\n'
    '  {"type": "paragraph", "text": "..."}\n'
    '  {"type": "list_item", "ordered": false, "level": 0, "text": "..."}\n'
    '  {"type": "table", "rows": [["cell", "cell"], ["cell", "cell"]]}\n'
    '  {"type": "figure", "bbox": [x0, y0, x1, y1]}\n'
    "Rules:\n"
    "- Keep reading order (in multi-column layouts finish the left column "
    "first).\n"
    "- Transcribe text EXACTLY as printed. Never correct, translate, "
    "summarize or invent anything.\n"
    "- Include every piece of text on the page exactly once.\n"
    "- Inside a paragraph, preserve the printed line breaks as \\n.\n"
    '- Every row of a table must have the same number of cells; use "" for '
    "empty cells.\n"
    "- For each photo, illustration, or diagram, emit one "
    '{"type": "figure"} entry at its position in reading order. Do not '
    "transcribe or describe its contents. If it occupies a clearly separate "
    "region of the page (not the whole page), also give its approximate "
    '"bbox" as fractions of the page width/height, [x0, y0, x1, y1] with '
    "0,0 at the top-left corner and 1,1 at the bottom-right; omit bbox "
    "otherwise."
)

_TRANSCRIBE_SYSTEM = (
    "You transcribe text from an image exactly as printed, one line per "
    "printed line. Output plain text only — no commentary, no formatting. "
    "If the image contains no text, output nothing."
)


def _hybrid_user(pageno: int, grounding: str) -> str:
    return (
        f"The attached image is page {pageno} of a PDF. The PDF's own "
        "extracted text layer for this page is given below as grounding: "
        "where the image and the text layer agree, copy the text layer's "
        "characters exactly.\n\n"
        f"TEXT LAYER:\n{grounding}\n\n"
        "Return the JSON array now."
    )


def _scanned_user(pageno: int) -> str:
    return (
        f"The attached image is page {pageno} of a scanned document. "
        "Transcribe it. Return the JSON array now."
    )


def _parse_vlm_blocks(raw: str | None) -> list[dict[str, Any]] | None:
    """Parse the VLM's JSON array into validated block specs, or None."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):  # tolerate a fenced reply despite instructions
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

    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        if btype == "heading":
            txt = str(item.get("text") or "").strip()
            if txt:
                level = item.get("level")
                level = level if isinstance(level, int) and 1 <= level <= 6 else 1
                out.append({"type": "heading", "text": txt, "level": level})
        elif btype == "paragraph":
            txt = str(item.get("text") or "").strip()
            if txt:
                out.append({"type": "paragraph", "text": txt})
        elif btype == "list_item":
            txt = str(item.get("text") or "").strip()
            if txt:
                level = item.get("level")
                level = level if isinstance(level, int) and level >= 0 else 0
                out.append({"type": "list_item", "text": txt, "level": level,
                            "ordered": bool(item.get("ordered"))})
        elif btype == "table":
            rows = item.get("rows")
            if isinstance(rows, list) and rows:
                clean = [[("" if c is None else str(c)) for c in r]
                         for r in rows if isinstance(r, list)]
                clean = [r for r in clean if any(c.strip() for c in r)]
                if clean:
                    out.append({"type": "table", "rows": clean})
        elif btype == "figure":
            spec: dict[str, Any] = {"type": "figure"}
            bbox = item.get("bbox")
            if (isinstance(bbox, list) and len(bbox) == 4
                    and all(isinstance(v, (int, float)) for v in bbox)):
                x0, y0, x1, y1 = (float(v) for v in bbox)
                if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1:
                    spec["bbox"] = (x0, y0, x1, y1)
            out.append(spec)
    return out or None


# ---------------------------------------------------------------------------
# Scanned-path text detector (pluggable)
# ---------------------------------------------------------------------------


@dataclass
class DetectedLine:
    """One detected text line: recognized text + bbox in rendered-image px."""

    text: str
    bbox: tuple[float, float, float, float]  # (x0, top, x1, bottom)


class TextDetector(Protocol):
    """Independent text detector/reader used to cross-check the VLM."""

    def detect(self, png: bytes) -> list[DetectedLine]:
        ...


class TesseractDetector:
    """Default detector: pytesseract line boxes + text (optional dependency)."""

    def __init__(self, lang: str | None = None) -> None:
        self.lang = lang or os.getenv("PDF_TESSERACT_LANG") or None

    def detect(self, png: bytes) -> list[DetectedLine]:
        import pytesseract  # deferred: optional dependency

        data = pytesseract.image_to_data(
            Image.open(io.BytesIO(png)), lang=self.lang,
            output_type=pytesseract.Output.DICT)
        lines: dict[tuple, list[int]] = {}
        for k in range(len(data["text"])):
            if not str(data["text"][k]).strip():
                continue
            key = (data["block_num"][k], data["par_num"][k], data["line_num"][k])
            lines.setdefault(key, []).append(k)
        out: list[DetectedLine] = []
        for key in sorted(lines):
            idxs = lines[key]
            text = " ".join(str(data["text"][k]).strip() for k in idxs)
            x0 = min(data["left"][k] for k in idxs)
            top = min(data["top"][k] for k in idxs)
            x1 = max(data["left"][k] + data["width"][k] for k in idxs)
            bottom = max(data["top"][k] + data["height"][k] for k in idxs)
            out.append(DetectedLine(text=text, bbox=(x0, top, x1, bottom)))
        return out


def _default_detector() -> TextDetector | None:
    try:
        import pytesseract  # noqa: F401
    except ModuleNotFoundError:
        return None
    return TesseractDetector()


# ---------------------------------------------------------------------------
# Render / crop / blob store
# ---------------------------------------------------------------------------


def _store_crop(data: bytes) -> str:
    """Write an audit crop into the image blob store; return its sha256.

    Same store as document images (`storage/images/`, immutable, dedup by
    content hash) so the webapp/citation pipeline resolves both the same way.
    """
    sha = hashlib.sha256(data).hexdigest()
    root = Path(os.getenv("PDF_CROP_DIR", "storage/images"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sha}.png"
    if not path.exists():
        path.write_bytes(data)
    return sha


def _crop_png(png: bytes, bbox: tuple[float, float, float, float],
              margin: int = 8) -> bytes:
    """Crop a rendered page PNG to `bbox` (px) with a small margin."""
    img = Image.open(io.BytesIO(png))
    x0 = max(0, int(bbox[0]) - margin)
    y0 = max(0, int(bbox[1]) - margin)
    x1 = min(img.width, int(bbox[2]) + margin)
    y1 = min(img.height, int(bbox[3]) + margin)
    if x1 <= x0 or y1 <= y0:
        return png
    buf = io.BytesIO()
    img.crop((x0, y0, x1, y1)).save(buf, format="PNG")
    return buf.getvalue()


def _union_bbox(boxes: list[tuple[float, float, float, float]]
                ) -> tuple[float, float, float, float]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _figure_crop(png: bytes, locator: dict[str, Any]) -> bytes:
    """Audit crop for an unpaired figure: tight to the VLM's own bbox guess
    (`vlm_bbox`, normalized fractions of the page) when it gave one, else the
    full rendered page -- the only honest fallback when nothing bounds it."""
    vlm_bbox = locator.pop("vlm_bbox", None)
    if vlm_bbox is None:
        return png
    img = Image.open(io.BytesIO(png))
    x0, y0, x1, y1 = vlm_bbox
    bbox = (x0 * img.width, y0 * img.height, x1 * img.width, y1 * img.height)
    return _crop_png(png, bbox)


# ---------------------------------------------------------------------------
# Code path layout analysis (pure helpers over pdfplumber word dicts)
# ---------------------------------------------------------------------------


@dataclass
class _Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    runs: list[InlineRun] = field(default_factory=list)


def _wsize(w: dict) -> float:
    return float(w.get("size") or 10.0)


def _word_marks(w: dict) -> tuple[Mark, ...]:
    """Bold/italic from the word's own font name -- the PDF equivalent of
    reading a docx run's rPr: deterministic, straight from the source's own
    metadata, not a visual guess. Covers the near-universal PDF font-naming
    convention (e.g. "Arial-BoldMT", "TimesNewRomanPS-BoldItalicMT")."""
    name = (w.get("fontname") or "").lower()
    marks: list[Mark] = []
    if "bold" in name:
        marks.append(Mark.BOLD)
    if "italic" in name or "oblique" in name:
        marks.append(Mark.ITALIC)
    return tuple(marks)


def _line_runs(group: list[dict]) -> list[InlineRun]:
    """Per-word marks -> merged InlineRuns whose concatenation equals the
    line's `" ".join(w["text"] for w in group)` text exactly."""
    runs = [InlineRun(w["text"] if i == 0 else " " + w["text"], _word_marks(w))
            for i, w in enumerate(group)]
    return finalize_runs(runs)


def _concat_runs(a: list[InlineRun], b: list[InlineRun]) -> list[InlineRun]:
    """`a` + a joining space + `b`, as run lists (merging identical-mark runs
    across the join). Used to glue per-line runs into a multi-line block."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    return finalize_runs(a + [InlineRun(" " + b[0].text, b[0].marks)] + b[1:])


def _slice_runs(runs: list[InlineRun], start: int) -> list[InlineRun]:
    """Drop the first `start` characters (e.g. a stripped bullet marker)."""
    out: list[InlineRun] = []
    pos = 0
    for r in runs:
        end = pos + len(r.text)
        if end > start:
            out.append(InlineRun(r.text[max(0, start - pos):], r.marks))
        pos = end
    return finalize_runs(out)


def _cluster_lines(words: list[dict]) -> list[_Line]:
    """Group word boxes into visual lines by their `top` coordinate."""
    lines: list[list[dict]] = []
    cur: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if cur and w["top"] - cur[0]["top"] > max(2.0, 0.5 * _wsize(w)):
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)

    out: list[_Line] = []
    for group in lines:
        group.sort(key=lambda w: w["x0"])
        out.append(_Line(
            text=" ".join(w["text"] for w in group),
            x0=min(w["x0"] for w in group),
            x1=max(w["x1"] for w in group),
            top=min(w["top"] for w in group),
            bottom=max(w["bottom"] for w in group),
            size=median(_wsize(w) for w in group),
            runs=_line_runs(group),
        ))
    return out


def _find_gutter(words: list[dict], page_width: float) -> float | None:
    """X coordinate of a two-column gutter, or None for single-column pages.

    Looks for a near-empty vertical band in the middle of the page with a
    substantial share of the words on each side. A small tolerance lets a
    centered title span the gutter without hiding it.
    """
    if len(words) < 40 or page_width <= 0:
        return None
    n = 200
    cover = [0] * n
    for w in words:
        b0 = max(0, min(n - 1, int(w["x0"] / page_width * n)))
        b1 = max(0, min(n - 1, int(w["x1"] / page_width * n)))
        for b in range(b0, b1 + 1):
            cover[b] += 1
    lo, hi = int(n * 0.30), int(n * 0.70)
    allow = max(1, int(0.02 * len(words)))
    best_width, best_center = 0, None
    i = lo
    while i <= hi:
        if cover[i] <= allow:
            j = i
            while j <= hi and cover[j] <= allow:
                j += 1
            if j - i > best_width:
                best_width, best_center = j - i, (i + j) / 2
            i = j
        else:
            i += 1
    if best_center is None or best_width < n * 0.025:
        return None
    gx = best_center / n * page_width
    left = sum(1 for w in words if (w["x0"] + w["x1"]) / 2 < gx)
    if min(left, len(words) - left) < 0.25 * len(words):
        return None
    return gx


# A line starting like a bullet / numbered item. The marker is stripped from
# the text; membership goes to list_* metadata (see base.py, "Lists").
_BULLET_RE = re.compile(r"^\s*([•◦▪‣●○∙·*–—-]|\d{1,3}[.)]|[A-Za-z][.)])\s+")


@dataclass
class _Spec:
    """One prospective text block before Block/id assignment."""

    kind: str            # "heading" | "paragraph" | "item"
    text: str
    top: float
    x0: float = 0.0
    level: int = 1       # heading level
    list_group: int = -1
    list_level: int = 0
    list_ordered: bool = False
    runs: list[InlineRun] = field(default_factory=list)


def _is_heading(line: _Line, body_size: float) -> bool:
    return (line.size >= body_size * 1.15 and line.size >= body_size + 0.4
            and len(line.text.split()) <= 14 and bool(line.text.strip()))


def _specs_from_lines(lines: list[_Line]) -> list[_Spec]:
    """Classify visual lines into heading / paragraph / list-item specs."""
    if not lines:
        return []
    body = median(l.size for l in lines)
    heading_sizes = sorted({round(l.size, 1) for l in lines
                            if _is_heading(l, body)}, reverse=True)

    specs: list[_Spec] = []
    para: list[_Line] = []
    prev: _Line | None = None
    group = -1

    def flush_para() -> None:
        nonlocal para
        if para:
            runs: list[InlineRun] = []
            for l in para:
                runs = _concat_runs(runs, l.runs)
            specs.append(_Spec(kind="paragraph",
                               text=" ".join(l.text for l in para),
                               top=para[0].top, x0=min(l.x0 for l in para),
                               runs=runs))
            para = []

    for line in lines:
        gap = line.top - prev.bottom if prev is not None else 0.0
        if _is_heading(line, body):
            flush_para()
            level = min(6, heading_sizes.index(round(line.size, 1)) + 1)
            specs.append(_Spec(kind="heading", text=line.text.strip(),
                               top=line.top, x0=line.x0, level=level,
                               runs=line.runs))
        elif (m := _BULLET_RE.match(line.text)):
            flush_para()
            if not (specs and specs[-1].kind == "item") or gap > 1.6 * body:
                group += 1
            marker = m.group(1)
            specs.append(_Spec(kind="item", text=line.text[m.end():].strip(),
                               top=line.top, x0=line.x0, list_group=group,
                               list_ordered=marker[0].isalnum(),
                               runs=_slice_runs(line.runs, m.end())))
        elif (specs and specs[-1].kind == "item" and not para
                and line.x0 > specs[-1].x0 + 1 and gap <= 0.9 * body):
            specs[-1].text += " " + line.text  # wrapped continuation of an item
            specs[-1].runs = _concat_runs(specs[-1].runs, line.runs)
        else:
            if para and gap > 0.6 * body:
                flush_para()
            para.append(line)
        prev = line
    flush_para()

    # List levels: within one contiguous list run, indent buckets -> depth.
    for g in {s.list_group for s in specs if s.kind == "item"}:
        xs: list[float] = []
        for s in specs:
            if s.kind == "item" and s.list_group == g:
                if not any(abs(s.x0 - x) <= 4 for x in xs):
                    xs.append(s.x0)
        xs.sort()
        for s in specs:
            if s.kind == "item" and s.list_group == g:
                s.list_level = next(i for i, x in enumerate(xs)
                                    if abs(s.x0 - x) <= 4)
    return specs


def _in_boxes(w: dict, boxes: list[tuple]) -> bool:
    cx = (w["x0"] + w["x1"]) / 2
    cy = (w["top"] + w["bottom"]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


def _span(vals: list[float], i0: int, hi: float, tol: float = 1.0) -> int:
    """How many grid steps (starting at i0) a cell's far edge `hi` covers,
    given the sorted grid boundary positions `vals`."""
    n = 1
    while i0 + n < len(vals) and vals[i0 + n] < hi - tol:
        n += 1
    return n


def _table_to_data(table) -> TableData:
    """pdfplumber Table -> TableData, reconstructing merges from cell geometry.

    pdfplumber gives no rowspan/colspan directly, but a merged cell shows up
    as one bbox spanning multiple grid boundaries (`table.cells`), while the
    grid positions it covers have no bbox of their own (`table.rows[r].cells`
    is None there) -- exactly the signal needed to rebuild `Merge` entries.
    A grid position with no bbox that *no* other cell's span covers is a
    genuine borderless/blank cell, not a merge, and stays an empty `Cell`.
    """
    raw_cells = table.cells
    if not raw_cells:
        return TableData(n_rows=0, n_cols=0, cells=[])

    xs = sorted({c[0] for c in raw_cells})
    tops = sorted({c[1] for c in raw_cells})
    nrows, ncols = len(tops), len(xs)

    chars = table.page.chars
    grid: list[list] = [[None] * ncols for _ in range(nrows)]
    covered: set[tuple[int, int]] = set()
    merges: list[Merge] = []

    for r, row in enumerate(table.rows):
        row_chars = [ch for ch in chars
                     if row.bbox[1] <= (ch["top"] + ch["bottom"]) / 2 < row.bbox[3]]
        for c, bbox in enumerate(row.cells):
            if bbox is None:
                continue
            colspan = _span(xs, c, bbox[2])
            rowspan = _span(tops, r, bbox[3])
            cell_chars = [ch for ch in row_chars
                          if bbox[0] <= (ch["x0"] + ch["x1"]) / 2 < bbox[2]]
            text = pdfplumber.utils.extract_text(cell_chars) if cell_chars else ""
            grid[r][c] = text_cell(text)
            if rowspan > 1 or colspan > 1:
                merges.append(Merge(row=r, col=c, rowspan=rowspan, colspan=colspan))
                for dr in range(rowspan):
                    for dc in range(colspan):
                        if dr or dc:
                            covered.add((r + dr, c + dc))

    for r in range(nrows):
        for c in range(ncols):
            if grid[r][c] is None and (r, c) not in covered:
                grid[r][c] = text_cell("")

    return TableData(n_rows=nrows, n_cols=ncols, cells=grid, merges=merges)


def _page_images(page) -> list[dict]:
    """Embedded raster objects worth keeping (tiny decorations skipped)."""
    out = []
    for im in page.images:
        if im["x1"] - im["x0"] < 8 or im["bottom"] - im["top"] < 8:
            continue
        out.append(im)
    return out


def _img_locator(pageno: int, im: dict) -> dict[str, Any]:
    loc: dict[str, Any] = {
        "page": pageno,
        "bbox": [round(float(im["x0"]), 2), round(float(im["top"]), 2),
                 round(float(im["x1"]), 2), round(float(im["bottom"]), 2)],
    }
    if im.get("name"):
        loc["name"] = str(im["name"])
    return loc


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _Counters:
    """Document-wide id counters (block ids, image markers, list ids)."""

    def __init__(self) -> None:
        self._block = 0
        self._image = 0
        self._list = 0

    def bid(self) -> str:
        s = f"b{self._block}"
        self._block += 1
        return s

    def img(self) -> int:
        self._image += 1
        return self._image

    def lid(self) -> str:
        self._list += 1
        return f"L{self._list}"


class PdfParser(BaseParser):
    extensions = (".pdf",)
    mimetypes = ("application/pdf",)
    fmt = "pdf"
    version = "pdf/0.1"

    def __init__(self, *, vlm: VLMClient | None = _UNSET,
                 vlm2: VLMClient | None = _UNSET,
                 detector: TextDetector | None = _UNSET) -> None:
        # All three are injectable for tests; the _UNSET sentinel means
        # "resolve lazily from the environment on first use" while an explicit
        # None disables that component outright.
        self._vlm = vlm
        self._vlm2 = vlm2
        self._detector = detector
        self._vlm_failures = 0

    # -- component resolution --------------------------------------------

    def _primary(self) -> VLMClient | None:
        if self._vlm is _UNSET:
            if not _env_bool("PDF_VLM", True):
                self._vlm = None
            else:
                try:
                    self._vlm = get_vlm_client()
                except LLMError:
                    self._vlm = None
        return self._vlm

    def _secondary(self) -> VLMClient | None:
        if self._vlm2 is _UNSET:
            if not _env_bool("PDF_VLM", True):
                self._vlm2 = None
            else:
                try:
                    self._vlm2 = get_vlm_client("secondary")
                except LLMError:
                    self._vlm2 = None
        return self._vlm2

    def _get_detector(self) -> TextDetector | None:
        if self._detector is _UNSET:
            self._detector = _default_detector()
        return self._detector

    # -- top level ---------------------------------------------------------

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        raw_path = Path(raw_path)
        raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()

        ct = _Counters()
        blocks: list[Block] = []
        page_meta: list[dict[str, Any]] = []

        with pdfplumber.open(raw_path) as pdf:
            page_count = len(pdf.pages)
            for pageno, page in enumerate(pdf.pages, start=1):
                route, info, prep = self._triage(page)
                meta = {"page": pageno, "route": route, "used": route, **info}
                if route == "empty":
                    meta["used"] = "empty"
                elif route == "code":
                    blocks.extend(self._parse_code(page, pageno, prep, ct))
                elif route == "hybrid":
                    blocks.extend(
                        self._parse_hybrid(page, pageno, prep, ct, meta))
                else:  # scanned
                    blocks.extend(self._parse_scanned(page, pageno, ct, meta))
                page_meta.append(meta)

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype="application/pdf",
            page_count=page_count,
            parser_version=self.version,
            metadata={"pdf_pages": page_meta},
            blocks=blocks,
        )

    # -- triage -------------------------------------------------------------

    def _triage(self, page) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Route one page: "empty" | "code" | "hybrid" | "scanned"."""
        words = page.extract_words(extra_attrs=["size", "fontname"])
        n_chars = len(page.chars)
        area = float(page.width) * float(page.height) or 1.0
        img_frac = 0.0
        for im in page.images:
            frac = (im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) / area
            img_frac = max(img_frac, frac)
        text_frac = sum((w["x1"] - w["x0"]) * (w["bottom"] - w["top"])
                        for w in words) / area

        tables: list = []
        gutter: float | None = None
        if n_chars == 0 and img_frac < 0.2:
            route = "empty"
        elif n_chars < 20 and img_frac >= 0.2:
            route = "scanned"  # no usable text layer over a page-sized raster
        elif img_frac > 0.8 and text_frac < 0.05:
            route = "scanned"  # thin/partial OCR layer glued onto a scan
        else:
            tables = page.find_tables()
            gutter = _find_gutter(words, float(page.width))
            route = "hybrid" if (tables or gutter is not None) else "code"

        info = {"chars": n_chars,
                "text_coverage": round(text_frac, 3),
                "image_coverage": round(img_frac, 3)}
        prep = {"words": words, "tables": tables, "gutter": gutter}
        return route, info, prep

    # -- code path -----------------------------------------------------------

    def _parse_code(self, page, pageno: int, prep: dict, ct: _Counters
                    ) -> list[Block]:
        tables = prep.get("tables") or []
        tboxes = [t.bbox for t in tables]
        words = [w for w in (prep.get("words") or [])
                 if not _in_boxes(w, tboxes)]
        gutter = prep.get("gutter")

        if gutter is None:
            columns = [words]
        else:
            columns = [
                [w for w in words if (w["x0"] + w["x1"]) / 2 < gutter],
                [w for w in words if (w["x0"] + w["x1"]) / 2 >= gutter],
            ]

        def col_of(x_center: float) -> int:
            return 0 if gutter is None or x_center < gutter else 1

        # (column, top, block): reading order = column-major, then top-down.
        entries: list[tuple[int, float, Block]] = []
        for ci, cwords in enumerate(columns):
            specs = _specs_from_lines(_cluster_lines(cwords))
            group_ids: dict[int, str] = {}
            for sp in specs:
                span = Span(page=pageno)
                runs = sp.runs if runs_have_marks(sp.runs) else []
                if sp.kind == "heading":
                    b: Block = HeadingBlock(id="", span=span, text=sp.text,
                                            level=sp.level, runs=runs,
                                            provenance=PROV_TEXT_LAYER)
                elif sp.kind == "item":
                    gid = group_ids.setdefault(sp.list_group, ct.lid())
                    b = ParagraphBlock(id="", span=span, text=sp.text,
                                       list_id=gid, list_level=sp.list_level,
                                       list_ordered=sp.list_ordered, runs=runs,
                                       provenance=PROV_TEXT_LAYER)
                else:
                    b = ParagraphBlock(id="", span=span, text=sp.text, runs=runs,
                                       provenance=PROV_TEXT_LAYER)
                entries.append((ci, sp.top, b))

        for t in tables:
            b = TableBlock(id="", span=Span(page=pageno),
                           table=_table_to_data(t), provenance=PROV_TEXT_LAYER)
            entries.append((col_of((t.bbox[0] + t.bbox[2]) / 2), t.bbox[1], b))

        for im in _page_images(page):
            b = ImageBlock(id="", span=Span(page=pageno), image_index=0,
                           locator=_img_locator(pageno, im))
            entries.append((col_of((im["x0"] + im["x1"]) / 2), im["top"], b))

        entries.sort(key=lambda e: (e[0], e[1]))
        out: list[Block] = []
        for _, _, b in entries:
            b.id = ct.bid()
            if isinstance(b, ImageBlock):
                b.image_index = ct.img()
            out.append(b)
        return out

    # -- VLM plumbing ---------------------------------------------------------

    def _read_page(self, vlm: VLMClient, png: bytes, user: str
                   ) -> list[dict[str, Any]] | None:
        max_tokens = _env_int("PDF_VLM_MAX_TOKENS", 8192)
        for attempt in range(2):
            try:
                raw = vlm.complete_vision(
                    system=_PAGE_SYSTEM, user=user, images=[("image/png", png)],
                    max_tokens=max_tokens)
            except LLMError:
                self._vlm_failures += 1
                if self._vlm_failures >= 2:
                    self._vlm = None  # transport is down; stop trying
                return None
            specs = _parse_vlm_blocks(raw)
            if specs is not None:
                return specs
            user = user + "\n\nReturn ONLY the JSON array, nothing else."
        return None

    def _reread(self, image: bytes) -> str | None:
        """Ask the secondary model to independently transcribe an image."""
        vlm2 = self._secondary()
        if vlm2 is None:
            return None
        try:
            return vlm2.complete_vision(
                system=_TRANSCRIBE_SYSTEM,
                user="Transcribe the attached image exactly.",
                images=[("image/png", image)], max_tokens=2048)
        except LLMError:
            self._vlm2 = None  # don't retry a dead endpoint per line
            return None

    def _second_reader(self, png: bytes):
        """Lazy, cached full-page second transcription -> (tokens, criticals)."""
        cache: dict[str, tuple[Counter, Counter] | None] = {}

        def get() -> tuple[Counter, Counter] | None:
            if "v" not in cache:
                text = self._reread(png)
                cache["v"] = ((_counter(_tok_list(text)), _criticals(text))
                              if text and text.strip() else None)
            return cache["v"]

        return get

    def _render(self, page) -> bytes:
        dpi = _env_int("PDF_RENDER_DPI", 150)
        img = page.to_image(resolution=dpi).original
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _blocks_from_vlm(self, specs: list[dict[str, Any]], pageno: int,
                         ct: _Counters, geom_tables: list | None = None,
                         geom_images: list[dict] | None = None,
                         emit_figures: bool = False,
                         ) -> list[Block]:
        """VLM block specs -> IR blocks (ids assigned; VLM order = reading order).

        `geom_tables` (hybrid path only): pdfplumber tables detected on this
        page, in reading order. The VLM has no coordinates for its own
        "table" specs, so its i-th table is paired positionally with the i-th
        geometric table; when a pairing exists, the geometric, merge-aware
        `TableData` (real text-layer chars, deterministic rowspan/colspan)
        replaces the VLM's flattened JSON grid outright, rather than trusting
        the model's guess. A count mismatch just falls back to the VLM's own
        rows for the unpaired tables.

        `geom_images`/`emit_figures` (hybrid and scanned paths): when
        `emit_figures` is set, a `"figure"` spec becomes an `ImageBlock` at
        its true reading-order position, positionally paired with the front
        of `geom_images` the same way tables are paired above -- matched
        entries are popped from the caller's list in place, so the caller
        can tell what is left unpaired and append it separately. A
        `"figure"` spec with nothing left to pair (the VLM saw more figures
        than pdfplumber's object model has raster entries for, e.g. a vector
        drawing, or -- on a scanned page -- a sub-figure baked into the
        page's own background raster) still becomes an `ImageBlock`, just
        with no `bbox` in its locator -- the caller marks those unverified
        with an audit crop. If the spec carried its own (ungrounded) `bbox`
        estimate -- normalized [x0, y0, x1, y1] fractions of the page --
        it's kept in the locator as `vlm_bbox` so the caller can crop the
        audit image tight to the figure instead of dumping the whole page;
        it never promotes the block out of "unverified" since it's the
        model's own guess, not an independent geometric source. `emit_figures`
        defaults to False so a caller that doesn't pass `geom_images` never
        has to think about figures at all.
        """
        geom_tables = sorted(geom_tables or [], key=lambda t: (t.bbox[1], t.bbox[0]))
        geom_images = geom_images if geom_images is not None else []
        blocks: list[Block] = []
        cur_list: str | None = None
        table_i = 0
        for sp in specs:
            span = Span(page=pageno)
            if sp["type"] == "list_item":
                if cur_list is None:
                    cur_list = ct.lid()
                blocks.append(ParagraphBlock(
                    id=ct.bid(), span=span, text=sp["text"], list_id=cur_list,
                    list_level=sp["level"], list_ordered=sp["ordered"]))
                continue
            cur_list = None
            if sp["type"] == "heading":
                blocks.append(HeadingBlock(id=ct.bid(), span=span,
                                           text=sp["text"], level=sp["level"]))
            elif sp["type"] == "paragraph":
                blocks.append(ParagraphBlock(id=ct.bid(), span=span,
                                             text=sp["text"]))
            elif sp["type"] == "figure":
                if not emit_figures:
                    continue
                if geom_images:
                    locator = _img_locator(pageno, geom_images.pop(0))
                else:
                    locator = {"page": pageno, "region": "vlm_figure"}
                    if "bbox" in sp:
                        locator["vlm_bbox"] = list(sp["bbox"])
                blocks.append(ImageBlock(id=ct.bid(), span=span,
                                         image_index=ct.img(), locator=locator))
            else:  # table
                if table_i < len(geom_tables):
                    table = _table_to_data(geom_tables[table_i])
                else:
                    rows = sp["rows"]
                    n_cols = max(len(r) for r in rows)
                    cells = [[text_cell(r[i] if i < len(r) else "")
                              for i in range(n_cols)] for r in rows]
                    table = TableData(n_rows=len(rows), n_cols=n_cols,
                                      cells=cells)
                table_i += 1
                blocks.append(TableBlock(id=ct.bid(), span=span, table=table))
        return blocks

    # -- hybrid path -----------------------------------------------------------

    def _parse_hybrid(self, page, pageno: int, prep: dict, ct: _Counters,
                      meta: dict[str, Any]) -> list[Block]:
        vlm = self._primary()
        if vlm is None:
            meta["used"] = "code"
            meta["note"] = "no VLM configured; code-path fallback"
            return self._parse_code(page, pageno, prep, ct)

        png = self._render(page)
        grounding = page.extract_text() or ""
        specs = self._read_page(vlm, png, _hybrid_user(pageno, grounding))
        if specs is None:
            meta["used"] = "code"
            meta["note"] = "VLM page read failed; code-path fallback"
            return self._parse_code(page, pageno, prep, ct)

        page_tokens = _counter(_tok_list(grounding))
        page_crit = _criticals(grounding)
        contain = _env_float("PDF_CONTAINMENT", 0.9)
        second = self._second_reader(png)

        # Sorted so positional pairing with the VLM's own reading-order
        # "figure" specs (see _blocks_from_vlm) lines up top-to-bottom.
        images = sorted(_page_images(page), key=lambda im: (im["top"], im["x0"]))
        blocks = self._blocks_from_vlm(specs, pageno, ct,
                                       geom_tables=prep.get("tables"),
                                       geom_images=images, emit_figures=True)
        for b in blocks:
            if isinstance(b, ImageBlock):
                if "bbox" not in b.locator:
                    # VLM claimed a figure pdfplumber's object model can't
                    # back geometrically (e.g. a vector drawing) -- honest
                    # unverified crop, same audit pattern as disputed text.
                    b.provenance = PROV_UNVERIFIED
                    b.source_crop = _store_crop(_figure_crop(png, b.locator))
                continue
            text = _block_text(b)
            tokens = _tok_list(text)
            if (page_tokens and _containment(tokens, page_tokens) >= contain
                    and _crit_ok(text, page_crit)):
                b.provenance = PROV_TEXT_LAYER
            else:
                sec = second()
                if (sec and _containment(tokens, sec[0]) >= contain
                        and not (_criticals(text) - sec[1])):
                    b.provenance = PROV_CONSENSUS
                else:
                    b.provenance = PROV_UNVERIFIED
                    b.source_crop = _store_crop(png)
            _flatten_newlines(b)

        # Anything the VLM didn't call out as a figure still gets a block --
        # deterministic, from the PDF's own object model -- just without a
        # reading-order position, since nothing anchors it in the VLM's flow.
        for im in images:
            blocks.append(ImageBlock(id=ct.bid(), span=Span(page=pageno),
                                     image_index=ct.img(),
                                     locator=_img_locator(pageno, im)))
        return blocks

    # -- scanned path ------------------------------------------------------------

    def _parse_scanned(self, page, pageno: int, ct: _Counters,
                       meta: dict[str, Any]) -> list[Block]:
        vlm = self._primary()
        specs = None
        png: bytes | None = None
        if vlm is not None:
            png = self._render(page)
            specs = self._read_page(vlm, png, _scanned_user(pageno))
        if specs is None or png is None:
            # No VLM (or the read failed): keep the page as one full-page
            # image so the images/ OCR stage can still recover its text.
            meta["used"] = "image-fallback"
            meta["note"] = ("no VLM configured; page kept as image"
                            if vlm is None else
                            "VLM page read failed; page kept as image")
            return [ImageBlock(
                id=ct.bid(), span=Span(page=pageno), image_index=ct.img(),
                locator={"page": pageno, "region": "full_page",
                         "bbox": [0, 0, round(float(page.width), 2),
                                  round(float(page.height), 2)]})]

        detector = self._get_detector()
        det_lines: list[DetectedLine] = []
        if detector is not None:
            try:
                det_lines = detector.detect(png)
            except Exception:
                # Missing tesseract binary etc.: fall back to VLM2-only checks.
                self._detector = None
        det_text = "\n".join(dl.text for dl in det_lines)
        det_tokens = _counter(_tok_list(det_text))
        det_crit = _criticals(det_text)
        second = self._second_reader(png)

        # Real embedded raster objects, excluding ones spanning most of the
        # page: on a true scan that IS the scan itself (the whole-page
        # background raster), not a distinct callout figure, and must not be
        # allowed to "win" the positional pairing below.
        page_area = float(page.width) * float(page.height) or 1.0
        images = sorted(
            (im for im in _page_images(page)
             if (im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) <= 0.6 * page_area),
            key=lambda im: (im["top"], im["x0"]))

        blocks = self._blocks_from_vlm(specs, pageno, ct,
                                       geom_images=images, emit_figures=True)
        if not blocks:
            # Defensive: every spec type now yields a block once figures are
            # emitted, so this shouldn't trigger in practice any more, but a
            # page that somehow produces nothing must still not vanish.
            meta["used"] = "image-fallback"
            meta["note"] = "VLM page read had no transcribable text; page kept as image"
            return [ImageBlock(
                id=ct.bid(), span=Span(page=pageno), image_index=ct.img(),
                locator={"page": pageno, "region": "full_page",
                         "bbox": [0, 0, round(float(page.width), 2),
                                  round(float(page.height), 2)]})]
        for b in blocks:
            if isinstance(b, ImageBlock):
                if "bbox" not in b.locator:
                    # No independent source can confirm "there is a figure
                    # here" the way the detector confirms text -- honest
                    # unverified crop, same audit pattern as disputed text.
                    b.provenance = PROV_UNVERIFIED
                    b.source_crop = _store_crop(_figure_crop(png, b.locator))
                continue
            failed_boxes: list[tuple[float, float, float, float]] = []
            all_ok = True
            for line in _block_lines(b):
                ok, bbox = self._verify_line(line, det_lines, det_tokens,
                                             det_crit, png, second)
                if not ok:
                    all_ok = False
                    if bbox is not None:
                        failed_boxes.append(bbox)
            if all_ok:
                b.provenance = PROV_CONSENSUS
            else:
                b.provenance = PROV_UNVERIFIED
                crop = (_crop_png(png, _union_bbox(failed_boxes))
                        if failed_boxes else png)
                b.source_crop = _store_crop(crop)
            _flatten_newlines(b)

        # Any real sub-figure the VLM didn't call out still gets a block --
        # deterministic, from the PDF's own object model -- just without a
        # reading-order position.
        for im in images:
            blocks.append(ImageBlock(id=ct.bid(), span=Span(page=pageno),
                                     image_index=ct.img(),
                                     locator=_img_locator(pageno, im)))
        return blocks

    def _verify_line(self, line: str, det_lines: list[DetectedLine],
                     det_tokens: Counter, det_crit: Counter, png: bytes,
                     second) -> tuple[bool, tuple | None]:
        """Check one VLM line against the independent sources.

        Returns (ok, bbox): `bbox` is the mapped detector region when the line
        mapped somewhere but could not be confirmed (used for the audit crop).
        No source ever "wins" a disagreement — an unconfirmed line just stays
        unverified.
        """
        if not _norm(line):
            return True, None
        line_ok = _env_float("PDF_LINE_MATCH", 0.8)
        contain = _env_float("PDF_CONTAINMENT", 0.9)

        best: DetectedLine | None = None
        best_r = 0.0
        for dl in det_lines:
            r = _ratio(line, dl.text)
            if r > best_r:
                best_r, best = r, dl

        # 1. Direct consensus with the detector's own reading of that line.
        if best is not None and best_r >= line_ok and _crit_ok(line, det_crit):
            return True, None
        # 1b. Reflowed lines: containment against the detector's full text.
        if (det_tokens and _containment(_tok_list(line), det_tokens) >= contain
                and _crit_ok(line, det_crit)):
            return True, None
        # 2. Mapped but disputed region: crop-and-reread with the second model.
        if best is not None and best_r >= 0.5:
            reread = self._reread(_crop_png(png, best.bbox))
            if reread and _crit_ok(line, _criticals(reread)) and (
                    _ratio(line, reread) >= line_ok
                    or _containment(_tok_list(line),
                                    _counter(_tok_list(reread))) >= contain):
                return True, None
            return False, best.bbox
        # 3. Maps to no detector region (hallucination suspicion): only an
        #    independent full second reading can still confirm it.
        sec = second()
        if (sec and _containment(_tok_list(line), sec[0]) >= contain
                and not (_criticals(line) - sec[1])):
            return True, None
        return False, None


# ---------------------------------------------------------------------------
# Block text views used by verification
# ---------------------------------------------------------------------------


def _block_text(b: Block) -> str:
    """Flat text of a block for containment checks."""
    if isinstance(b, TableBlock):
        return " ".join(
            c.plain_text() for row in b.table.cells for c in row if c is not None)
    return getattr(b, "text", "")


def _block_lines(b: Block) -> list[str]:
    """Printed lines of a block (the unit the scanned path verifies)."""
    if isinstance(b, TableBlock):
        return [" ".join(c.plain_text() for c in row if c is not None)
                for row in b.table.cells]
    text = getattr(b, "text", "")
    return [ln for ln in text.split("\n") if ln.strip()]


def _flatten_newlines(b: Block) -> None:
    """Collapse the VLM's preserved line breaks to the IR's canonical
    single-space text (matching every other parser) once verification, which
    needed the printed lines, is done."""
    if isinstance(b, (ParagraphBlock, HeadingBlock)):
        b.text = " ".join(b.text.split())
