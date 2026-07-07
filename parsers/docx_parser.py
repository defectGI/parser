"""DOCX -> ParsedDocument.

Uses python-docx for structure, plus direct XML access for the parts python-docx
does not expose:
* numbering (w:numPr) -> list_id (numId) / list_level (ilvl); ordered vs bullet is
  resolved from word/numbering.xml (numFmt). This is where choosing the B list model
  pays off: docx stores lists exactly this way.
* table merges via w:gridSpan (colspan) and w:vMerge (rowspan).
* table cells are block-structured (`Cell.blocks`): a cell's paragraphs, its images,
  and any *nested* w:tbl (parsed recursively into a real TableBlock) are preserved
  in order — never flattened into one string. Depth is bounded by `_MAX_TABLE_DEPTH`.

Paragraph/cell content is read by `_walk_para`, which walks a w:p in document order:
* text from both w:t and Office Math m:t (math is no longer dropped);
* inline formatting from each run's w:rPr (bold/italic/underline/strike/super/sub)
  -> `InlineRun`s on the block's `runs` (plain `text` stays the canonical view; only
  direct rPr is read, style-inherited marks are not resolved yet);
* images from both modern DrawingML (a:blip r:embed) and legacy/OLE VML
  (v:imagedata r:id) -> `<imageN>` marker + ImageBlock (locator = media part path,
  image_handler fetches bytes later). Under mc:AlternateContent only the Choice
  branch is walked, so an object shipping both renderings is not counted twice.
Body and cell images share one `<imageN>` counter, so indices run in reading order.

Byte offsets (Karar B, best-effort): each body-level paragraph/table is matched
positionally to its element in word/document.xml (via expat) and gets a Span with
part="word/document.xml"; if the counts don't line up the offsets are left unset.
Nested/cell-internal blocks carry no byte offsets (located by table/row/col/index).
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree
from xml.parsers import expat

from .base import (
    BaseParser, ParsedDocument, Span, Cell,
    HeadingBlock, ParagraphBlock, TableBlock, ImageBlock, TableData, Merge,
    Mark, InlineRun, finalize_runs, runs_have_marks,
)
from .heading_heuristics import (
    CAPTION as _CAPTION,
    is_all_caps as _is_all_caps,
    is_title_case as _is_title_case,
    looks_like_date as _looks_like_date,
    numbering_level as _numbering_level,
)

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"<image\d+>")
_HEADING_STYLE = re.compile(r"^Heading (\d)$")
_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Max table nesting depth before we stop recursing and flatten the rest to text.
# A cell nested deeper than this is pathological (likely malformed/adversarial);
# we degrade gracefully instead of recursing unbounded.
_MAX_TABLE_DEPTH = 10

# Element tags handled while walking a paragraph. Besides the well-known w:/a:/r:
# namespaces (registered in python-docx), we need three that are NOT registered
# there, so they are spelled out in Clark notation:
#   * VML imagedata  — legacy/OLE raster previews (v:imagedata r:id), not a:blip
#   * markup-compat   — mc:AlternateContent wraps a modern (Choice) + legacy
#                       (Fallback) rendering of the SAME object; we take Choice
#                       only, which is what avoids counting an image twice.
_W_R = qn("w:r")
_W_RPR = qn("w:rPr")
_W_T = qn("w:t")
_M_T = qn("m:t")                                  # Office Math text
_W_TBL = qn("w:tbl")
_A_BLIP = qn("a:blip")
_V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_MC_ALT = _MC + "AlternateContent"
_MC_CHOICE = _MC + "Choice"
_MC_FALLBACK = _MC + "Fallback"

# w:rPr toggle values that mean "explicitly OFF" (a run turning a style-inherited
# mark back off). Absent val => on.
_OFF = {"0", "false", "off", "none"}


def _toggle_on(el) -> bool:
    if el is None:
        return False
    val = el.get(qn("w:val"))
    return val is None or val.lower() not in _OFF


def _run_marks(r) -> tuple[Mark, ...]:
    """Semantic inline marks of a w:r, read from its direct w:rPr.

    Only direct run properties are read; style-inherited formatting (rPr on the
    paragraph/character style) is not resolved yet.
    """
    rpr = r.find(_W_RPR)
    if rpr is None:
        return ()
    marks: list[Mark] = []
    if _toggle_on(rpr.find(qn("w:b"))):
        marks.append(Mark.BOLD)
    if _toggle_on(rpr.find(qn("w:i"))):
        marks.append(Mark.ITALIC)
    u = rpr.find(qn("w:u"))
    if u is not None and (u.get(qn("w:val")) or "single") not in _OFF:
        marks.append(Mark.UNDERLINE)
    if _toggle_on(rpr.find(qn("w:strike"))) or _toggle_on(rpr.find(qn("w:dstrike"))):
        marks.append(Mark.STRIKE)
    va = rpr.find(qn("w:vertAlign"))
    if va is not None:
        v = va.get(qn("w:val"))
        if v == "superscript":
            marks.append(Mark.SUPERSCRIPT)
        elif v == "subscript":
            marks.append(Mark.SUBSCRIPT)
    return tuple(marks)


def _has_text(text: str) -> bool:
    return bool(_MARKER.sub("", text).strip())


def _walk_para(p, document, img_n) -> tuple[list[InlineRun],
                                            list[tuple[int, str, str | None]]]:
    """Walk a w:p element in document order, returning (runs, images).

    `runs` are InlineRuns whose concatenated text is the paragraph's plain text
    (with `<imageN>` markers inline); each run carries the semantic marks of its
    w:r (bold/italic/underline/strike/super/subscript). Captures both plain text
    (w:t) and Office Math text (m:t; unmarked), and both modern (a:blip r:embed)
    and legacy/OLE (v:imagedata r:id) images. Under mc:AlternateContent only the
    Choice branch is walked, so an object shipping both renderings isn't counted
    twice. Adjacent same-mark runs are merged and the sequence is edge-stripped.
    """
    runs: list[InlineRun] = []
    images: list[tuple[int, str, str | None]] = []

    def emit_image(rid: str | None) -> None:
        if not rid:
            return
        img_n[0] += 1
        runs.append(InlineRun(f"<image{img_n[0]}>"))  # markers are never styled
        part_path, mime = "?", None
        try:
            ipart = document.part.related_parts[rid]
            part_path = str(ipart.partname).lstrip("/")
            mime = ipart.content_type
        except KeyError:
            pass
        images.append((img_n[0], part_path, mime))

    def visit(el, marks: tuple[Mark, ...]) -> None:
        tag = el.tag
        if tag == _W_R:  # a run defines the marks for the text it contains
            m = _run_marks(el)
            for child in el:
                if child.tag != _W_RPR:
                    visit(child, m)
            return
        if tag in (_W_T, _M_T):
            if el.text:
                runs.append(InlineRun(el.text, marks))
            return
        if tag == _A_BLIP:
            emit_image(el.get(qn("r:embed")))
            return
        if tag == _V_IMAGEDATA:
            emit_image(el.get(qn("r:id")))
            return
        if tag == _W_TBL:
            return  # a paragraph never owns a table; guard against odd nesting
        if tag == _MC_ALT:
            branch = el.find(_MC_CHOICE)
            if branch is None:
                branch = el.find(_MC_FALLBACK)
            if branch is not None:
                for child in branch:
                    visit(child, marks)
            return
        for child in el:
            visit(child, marks)

    for child in p:
        visit(child, ())
    return finalize_runs(runs), images


def _flatten_tbl_text(tbl) -> str:
    """Last-resort text of a whole w:tbl (used only past the depth guard)."""
    return " ".join(
        txt for txt in ("".join(t.text or "" for t in tr.iter(qn("w:t"))).strip()
                        for tr in tbl.findall(qn("w:tr"))) if txt)


def _build_cell(tc, document, img_n, depth: int) -> Cell:
    """Build a Cell from a w:tc element as an ordered list of blocks.

    Each child paragraph becomes a ParagraphBlock (math and images included via
    `_walk_para`, mirroring the body); an image in the cell also emits an
    ImageBlock sibling, exactly like body images. Each *nested table* becomes a
    real TableBlock (recursively parsed) — never flattened into the parent cell's
    text. Cell-internal blocks are anonymous (id="") since nothing addresses them
    individually yet; they are located by (table, row, col, index).
    """
    blocks: list = []
    for child in tc.iterchildren():
        if child.tag == qn("w:p"):
            runs, images = _walk_para(child, document, img_n)
            text = "".join(r.text for r in runs)
            if _has_text(text):
                blocks.append(ParagraphBlock(
                    id="", text=text,
                    runs=runs if runs_have_marks(runs) else []))
            for image_index, part, mime in images:
                blocks.append(ImageBlock(
                    id="", image_index=image_index,
                    locator={"part": part}, mime=mime))
        elif child.tag == qn("w:tbl"):
            if depth + 1 > _MAX_TABLE_DEPTH:
                logger.warning(
                    "nested table depth exceeds %d; flattening to text",
                    _MAX_TABLE_DEPTH)
                text = _flatten_tbl_text(child).strip()
                if text:
                    blocks.append(ParagraphBlock(id="", text=text))
            else:
                blocks.append(TableBlock(
                    id="", table=_build_docx_table(child, document, img_n,
                                                   depth + 1)))
    return Cell(blocks=blocks)


def _build_docx_table(tbl, document, img_n, depth: int = 0) -> TableData:
    """Build TableData from a w:tbl element, honoring gridSpan/vMerge merges."""
    grid: dict[tuple[int, int], Cell | None] = {}
    mergemap: dict[tuple[int, int], list[int]] = {}   # (r,c) -> [rowspan, colspan]
    vorigin: dict[int, tuple[int, int]] = {}          # col -> active vmerge origin
    ncols = 0

    trs = tbl.findall(qn("w:tr"))
    for r, tr in enumerate(trs):
        c = 0
        for tc in tr.findall(qn("w:tc")):
            tcPr = tc.find(qn("w:tcPr"))
            colspan, vmerge = 1, None
            if tcPr is not None:
                gs = tcPr.find(qn("w:gridSpan"))
                if gs is not None:
                    colspan = int(gs.get(qn("w:val")))
                vm = tcPr.find(qn("w:vMerge"))
                if vm is not None:
                    vmerge = vm.get(qn("w:val")) or "continue"

            if vmerge == "continue":
                origin = vorigin.get(c)
                for dc in range(colspan):
                    grid[(r, c + dc)] = None
                if origin and origin in mergemap:
                    mergemap[origin][0] += 1
                    for dc in range(colspan):
                        vorigin[c + dc] = origin
                c += colspan
                ncols = max(ncols, c)
                continue

            grid[(r, c)] = _build_cell(tc, document, img_n, depth)
            for dc in range(1, colspan):
                grid[(r, c + dc)] = None
            if vmerge == "restart":
                mergemap[(r, c)] = [1, colspan]
                for dc in range(colspan):
                    vorigin[c + dc] = (r, c)
            else:
                if colspan > 1:
                    mergemap[(r, c)] = [1, colspan]
                for dc in range(colspan):
                    vorigin.pop(c + dc, None)
            c += colspan
            ncols = max(ncols, c)

    nrows = len(trs)
    merges = [Merge(row=r, col=c, rowspan=rs, colspan=cs)
              for (r, c), (rs, cs) in mergemap.items() if rs > 1 or cs > 1]
    cells = [[grid.get((r, c)) for c in range(ncols)] for r in range(nrows)]
    return TableData(n_rows=nrows, n_cols=ncols, cells=cells, merges=merges)


# ---------------------------------------------------------------------------
# Heading detection for paragraphs that fake a heading via formatting.
#
# People often mark a heading by formatting a normal paragraph (bold, all-caps,
# centered, isolated by blank lines, numbered) instead of using a Heading style.
# We score such clues and promote the paragraph to a HeadingBlock above a
# threshold. Definitive signals (w:outlineLvl, Title/Subtitle style) bypass the
# score. Level: outline level or numbering depth when present, else H1 for a
# notably-larger-than-body font, else the configured default (H2). A bare
# date/time line and figure/table captions are gated out.
#
# Weights/threshold/gates are configurable via HeadingConfig (never hardcoded).
# ---------------------------------------------------------------------------


@dataclass
class HeadingConfig:
    """Tunable knobs for detecting formatting-faked headings.

    Nothing here is baked into the scoring logic — pass an instance to
    ``DocxParser(heading_config=...)`` to override any weight/threshold; the
    defaults are the values settled by corpus calibration. Set a clue's weight to
    0 to disable it; the ``enable_*`` flags turn whole gates off.
    """

    threshold: float = 7.0
    max_words: int = 20          # a candidate longer than this is prose, not a heading
    default_level: int = 2       # promoted heading with no level signal -> H2

    # positive clue weights (points added when the clue fires)
    bold: float = 3.0
    number: float = 3.0
    caps: float = 2.0
    center: float = 2.0
    titlecase: float = 1.5       # Every Significant Word Capitalized (mixed case)
    titlecase_max_words: int = 7
    short: float = 2.0           # <= 7 words
    vshort: float = 1.0          # extra when <= 4 words (very short = short + vshort)
    isolation: float = 1.5       # blank paragraph both before and after
    keepnext: float = 1.0
    underline: float = 1.0
    no_punct: float = 1.0        # ends without terminal punctuation (near-always-on: low)
    large_font: float = 2.0      # font >= large_font_ratio  x body baseline
    xlarge_font: float = 3.0     # font >= xlarge_font_ratio x body baseline

    # negative clue weights
    sentence: float = -3.0       # ends with . ? ! -> looks like a sentence
    italic: float = -2.0         # fully italic -> emphasis/term/species/citation

    # font-size clue, relative to the document body baseline
    large_font_ratio: float = 1.15
    xlarge_font_ratio: float = 1.40   # also promotes level -> H1

    # gates
    enable_font: bool = True       # score/level by font size vs body baseline
    enable_date_gate: bool = True   # a bare date/time line is never a heading


DEFAULT_HEADING_CONFIG = HeadingConfig()

_TITLE_STYLE = re.compile(r"title|subtitle|heading|head|ba[şs]l[ıi]k", re.I)


def _default_font_size(zf: zipfile.ZipFile) -> int | None:
    """Body baseline font size (half-points) from styles.xml docDefaults/Normal."""
    try:
        root = etree.fromstring(zf.read("word/styles.xml"))
    except (KeyError, etree.XMLSyntaxError):
        return None

    def sz_of(rpr) -> int | None:
        if rpr is None:
            return None
        s = rpr.find(qn("w:sz"))
        v = s.get(qn("w:val")) if s is not None else None
        return int(v) if v and v.isdigit() else None

    dd = root.find(qn("w:docDefaults"))
    if dd is not None:
        rprd = dd.find(qn("w:rPrDefault"))
        if rprd is not None:
            v = sz_of(rprd.find(qn("w:rPr")))
            if v:
                return v
    for st in root.findall(qn("w:style")):
        name = st.find(qn("w:name"))
        if st.get(qn("w:styleId")) == "Normal" or (
                name is not None and name.get(qn("w:val")) == "Normal"):
            v = sz_of(st.find(qn("w:rPr")))
            if v:
                return v
    return None


def _para_font_size(p) -> int | None:
    """Largest explicit run font size (half-points) among the paragraph's text runs."""
    sizes = []
    for r in p.iter(qn("w:r")):
        rpr = r.find(qn("w:rPr"))
        s = rpr.find(qn("w:sz")) if rpr is not None else None
        v = s.get(qn("w:val")) if s is not None else None
        if v and v.isdigit() and any((t.text or "").strip() for t in r.iter(qn("w:t"))):
            sizes.append(int(v))
    return max(sizes) if sizes else None

# Calibration hook: when set to a list, every scored candidate is recorded here
# (text, score, fired clues, decision). None in production -> zero overhead, and
# nothing about scoring ever reaches the serialized IR.
_CAL_REPORT: list | None = None


def _heading_signals(obj, runs, text: str, blank_before: bool, blank_after: bool,
                     baseline: int | None) -> dict:
    """Collect the transient heading clues for one body paragraph."""
    pPr = obj._p.find(qn("w:pPr"))
    outline, centered, keepnext = None, False, False
    if pPr is not None:
        o = pPr.find(qn("w:outlineLvl"))
        if o is not None:
            v = o.get(qn("w:val"))
            lvl = int(v) if v is not None else 0
            outline = lvl if 0 <= lvl <= 8 else None   # 9 = "body text" sentinel
        jc = pPr.find(qn("w:jc"))
        if jc is not None and jc.get(qn("w:val")) == "center":
            centered = True
        keepnext = pPr.find(qn("w:keepNext")) is not None
    textruns = [r for r in runs if r.text.strip() and not _MARKER.fullmatch(r.text)]
    bold_all = bool(textruns) and all(Mark.BOLD in r.marks for r in textruns)
    underline_all = bool(textruns) and all(Mark.UNDERLINE in r.marks for r in textruns)
    italic_all = bool(textruns) and all(Mark.ITALIC in r.marks for r in textruns)
    size = _para_font_size(obj._p)
    font_ratio = (size / baseline) if (size and baseline) else 0.0
    stripped = text.rstrip()
    last = stripped[-1] if stripped else ""
    return {
        "text": text,
        "wc": len(text.split()),
        "caps": _is_all_caps(text),
        "titlecase": _is_title_case(text),
        "num": _numbering_level(text),
        "bold": bold_all,
        "underline": underline_all,
        "italic": italic_all,
        "outline": outline,
        "center": centered,
        "keepnext": keepnext,
        "font_ratio": font_ratio,
        "is_date": _looks_like_date(text),
        "blank_before": blank_before,
        "blank_after": blank_after,
        "ends_sentence": last in ".?!",
        "no_terminal": bool(last) and last not in ".?!,;:",
        "sentences": sum(text.count(ch) for ch in ".?!"),
    }


def _classify_heading(sig: dict, style_name: str, has_following: bool,
                      cfg: HeadingConfig) -> tuple[bool, int | None, str]:
    """Return (is_heading, level, source). `source` in
    {"outline","style","score",""} — for calibration/debug only."""
    fired: list[str] = []
    score = 0.0
    is_h, level, source = False, None, ""

    if sig["outline"] is not None:                         # definitive
        is_h, level, source, fired = True, min(sig["outline"] + 1, 6), "outline", ["outlineLvl"]
    elif style_name and _TITLE_STYLE.search(style_name):   # definitive
        sub = "sub" in style_name.lower()
        is_h, source = True, "style"
        level = 2 if sub else 1
        fired = [f"style:{style_name}"]
    else:                                                  # scored
        gate = (has_following and sig["wc"] > 0
                and sig["sentences"] < 2
                and not _CAPTION.match(sig["text"])
                and not (cfg.enable_date_gate and sig["is_date"])
                and (sig["num"] is not None or sig["wc"] <= cfg.max_words))
        if gate:
            def add(key: str, cond: bool, label: str | None = None) -> None:
                nonlocal score
                if cond:
                    score += getattr(cfg, key)
                    fired.append(label or key)
            add("bold", sig["bold"])
            add("number", sig["num"] is not None)
            add("caps", sig["caps"])
            add("titlecase", sig["titlecase"] and sig["wc"] <= cfg.titlecase_max_words)
            add("center", sig["center"])
            add("italic", sig["italic"])
            if sig["wc"] <= 4:
                score += cfg.short + cfg.vshort; fired.append("vshort")
            elif sig["wc"] <= 7:
                score += cfg.short; fired.append("short")
            add("isolation", sig["blank_before"] and sig["blank_after"])
            add("keepnext", sig["keepnext"])
            add("underline", sig["underline"])
            if cfg.enable_font and sig["font_ratio"] >= cfg.xlarge_font_ratio:
                add("xlarge_font", True)
            elif cfg.enable_font and sig["font_ratio"] >= cfg.large_font_ratio:
                add("large_font", True)
            if sig["ends_sentence"]:
                score += cfg.sentence; fired.append("sentence-")
            elif sig["no_terminal"]:
                score += cfg.no_punct; fired.append("no_punct")
            if score >= cfg.threshold:
                is_h, source = True, "score"
                if sig["num"] is not None:
                    level = sig["num"]
                elif cfg.enable_font and sig["font_ratio"] >= cfg.xlarge_font_ratio:
                    level = 1                       # notably larger than body -> top level
                else:
                    level = cfg.default_level

    if _CAL_REPORT is not None:
        _CAL_REPORT.append({"text": sig["text"][:70], "wc": sig["wc"],
                            "score": round(score, 1), "fired": fired,
                            "decision": is_h, "level": level, "source": source})
    return is_h, level, source


def _numbering_ordered(zf: zipfile.ZipFile) -> dict[tuple[str, int], bool]:
    """Map (numId, ilvl) -> ordered? by reading word/numbering.xml numFmt."""
    try:
        root = etree.fromstring(zf.read("word/numbering.xml"))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    absmap: dict[str, dict[int, str]] = {}
    for an in root.findall(qn("w:abstractNum")):
        aid = an.get(qn("w:abstractNumId"))
        lvls = {}
        for lvl in an.findall(qn("w:lvl")):
            il = int(lvl.get(qn("w:ilvl")))
            nf = lvl.find(qn("w:numFmt"))
            lvls[il] = nf.get(qn("w:val")) if nf is not None else "bullet"
        absmap[aid] = lvls
    out: dict[tuple[str, int], bool] = {}
    for num in root.findall(qn("w:num")):
        nid = num.get(qn("w:numId"))
        ab = num.find(qn("w:abstractNumId"))
        if ab is None:
            continue
        for il, fmt in absmap.get(ab.get(qn("w:val")), {}).items():
            out[(nid, il)] = fmt not in ("bullet", "none")
    return out


def _style_numbering(zf: zipfile.ZipFile) -> dict[str, tuple[str, int]]:
    """Map styleId -> (numId, ilvl) for styles that carry numbering.

    Style-based lists (e.g. "List Bullet"/"List Number") define w:numPr on the
    style in word/styles.xml, not on the paragraph. basedOn inheritance is resolved.
    """
    try:
        root = etree.fromstring(zf.read("word/styles.xml"))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    raw: dict[str, tuple[str | None, int | None, str | None]] = {}
    for st in root.findall(qn("w:style")):
        sid = st.get(qn("w:styleId"))
        numId, ilvl = None, None
        pPr = st.find(qn("w:pPr"))
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:
                n = numPr.find(qn("w:numId"))
                lv = numPr.find(qn("w:ilvl"))
                if n is not None:
                    numId = n.get(qn("w:val"))
                if lv is not None:
                    ilvl = int(lv.get(qn("w:val")))
        based = st.find(qn("w:basedOn"))
        raw[sid] = (numId, ilvl, based.get(qn("w:val")) if based is not None else None)

    def resolve(sid: str, seen: tuple = ()) -> tuple[str | None, int | None]:
        if sid not in raw or sid in seen:
            return None, None
        numId, ilvl, base = raw[sid]
        if numId is None and base:
            bnum, bilvl = resolve(base, seen + (sid,))
            numId = bnum
            ilvl = ilvl if ilvl is not None else bilvl
        return numId, ilvl

    out: dict[str, tuple[str, int]] = {}
    for sid in raw:
        numId, ilvl = resolve(sid)
        if numId is not None and numId != "0":
            out[sid] = (numId, ilvl or 0)
    return out


def _body_child_spans(xml: bytes) -> list[tuple[str, int, int]]:
    """Byte spans of direct <w:body> children (w:p / w:tbl), in document order."""
    spans: list[tuple[str, int, int]] = []
    open_stack: list[tuple[str, int]] = []
    parser = expat.ParserCreate()
    state = {"depth": 0, "body": None}

    def start(name, _attrs):
        state["depth"] += 1
        if name == "w:body":
            state["body"] = state["depth"]
        elif (state["body"] is not None and state["depth"] == state["body"] + 1
              and name in ("w:p", "w:tbl")):
            open_stack.append((name, parser.CurrentByteIndex))

    def end(name):
        if (state["body"] is not None and state["depth"] == state["body"] + 1
                and name in ("w:p", "w:tbl") and open_stack):
            nm, b0 = open_stack.pop()
            spans.append((nm, b0, parser.CurrentByteIndex))
        state["depth"] -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(xml, True)
    except expat.ExpatError:
        return []
    return spans


class DocxParser(BaseParser):
    extensions = (".docx",)
    mimetypes = (_MIME,)
    fmt = "docx"
    version = "docx/0.1"

    def __init__(self, heading_config: HeadingConfig | None = None) -> None:
        # Heading-detection knobs; defaults are the calibrated values. Override with
        # DocxParser(HeadingConfig(threshold=8, bold=4, ...)).
        self.heading_config = heading_config or DEFAULT_HEADING_CONFIG

    def parse(self, raw_path: str | Path, doc_id: str) -> ParsedDocument:
        raw_path = Path(raw_path)
        data = raw_path.read_bytes()
        raw_sha256 = hashlib.sha256(data).hexdigest()

        document = docx.Document(raw_path)
        with zipfile.ZipFile(raw_path) as zf:
            ordered_map = _numbering_ordered(zf)
            style_map = _style_numbering(zf)
            font_baseline = _default_font_size(zf)
            spans = _body_child_spans(zf.read("word/document.xml"))

        body = document.element.body
        items = []  # (kind, obj)
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                items.append(("p", Paragraph(child, document)))
            elif child.tag == qn("w:tbl"):
                items.append(("tbl", Table(child, document)))

        # Positional match of body items to their XML byte spans (best-effort).
        span_by_index: dict[int, Span] = {}
        if len(spans) == len(items) and all(
                s[0] == ("w:p" if k == "p" else "w:tbl")
                for s, (k, _) in zip(spans, items)):
            for i, (_, b0, b1) in enumerate(spans):
                span_by_index[i] = Span(part="word/document.xml",
                                        byte_start=b0, byte_end=b1)

        blocks: list = []
        bid = 0
        img_n = [0]

        def next_id() -> str:
            nonlocal bid
            s = f"b{bid}"
            bid += 1
            return s

        # A body paragraph is "blank" if it has no text; used for the heading
        # isolation clue (blank line before/after) without emitting the blanks.
        blank_item = [kind == "p" and not obj.text.strip() for kind, obj in items]

        # Non-list body paragraphs that might be formatting-faked headings.
        candidates: list[tuple] = []

        for i, (kind, obj) in enumerate(items):
            span = span_by_index.get(i, Span())
            if kind == "tbl":
                blocks.append(TableBlock(
                    id=next_id(), span=span,
                    table=_build_docx_table(obj._tbl, document, img_n)))
                continue

            # paragraph
            meta = self._list_meta(obj, ordered_map, style_map)
            runs, images = _walk_para(obj._p, document, img_n)
            text = "".join(r.text for r in runs)
            style = obj.style.name if obj.style else ""
            hm = _HEADING_STYLE.match(style or "")

            if hm and not meta and _has_text(text):
                hruns = [r for r in runs if not _MARKER.fullmatch(r.text)]
                blocks.append(HeadingBlock(
                    id=next_id(), span=span,
                    text=_MARKER.sub("", text).strip(), level=int(hm.group(1)),
                    runs=finalize_runs(hruns) if runs_have_marks(hruns) else []))
            elif _has_text(text):
                pb = ParagraphBlock(
                    id=next_id(), span=span, text=text,
                    runs=runs if runs_have_marks(runs) else [], **meta)
                blocks.append(pb)
                if not meta:  # list items are never headings
                    blank_before = i > 0 and blank_item[i - 1]
                    blank_after = i < len(items) - 1 and blank_item[i + 1]
                    candidates.append((pb, obj, style, blank_before, blank_after))
            for image_index, part, mime in images:
                blocks.append(ImageBlock(
                    id=next_id(), image_index=image_index,
                    locator={"part": part}, mime=mime, **meta))

        self._promote_headings(blocks, candidates, self.heading_config,
                               font_baseline)

        return ParsedDocument(
            doc_id=doc_id,
            source_path=str(raw_path),
            fmt=self.fmt,
            raw_sha256=raw_sha256,
            mimetype=_MIME,
            parser_version=self.version,
            blocks=blocks,
        )

    @staticmethod
    def _promote_headings(blocks: list, candidates: list,
                          cfg: HeadingConfig, baseline: int | None) -> None:
        """Convert formatting-faked-heading paragraphs into HeadingBlocks in place.

        Runs after the block list is built so isolation and following-content can be
        judged from context. Signals are transient — nothing is persisted to the IR.
        """
        last = len(blocks) - 1
        index = {id(b): k for k, b in enumerate(blocks)}
        for pb, obj, style, blank_before, blank_after in candidates:
            sig = _heading_signals(obj, pb.runs, pb.text,
                                   blank_before, blank_after, baseline)
            has_following = index[id(pb)] < last
            is_h, level, _source = _classify_heading(sig, style, has_following, cfg)
            if not is_h:
                continue
            hruns = [r for r in pb.runs if not _MARKER.fullmatch(r.text)]
            blocks[index[id(pb)]] = HeadingBlock(
                id=pb.id, span=pb.span,
                text=_MARKER.sub("", pb.text).strip(),
                level=level or cfg.default_level,
                runs=finalize_runs(hruns) if runs_have_marks(hruns) else [])

    @staticmethod
    def _list_meta(paragraph, ordered_map, style_map) -> dict:
        pPr = paragraph._p.find(qn("w:pPr"))
        numId, ilvl = None, None
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:  # direct numbering on the paragraph
                numId_e = numPr.find(qn("w:numId"))
                ilvl_e = numPr.find(qn("w:ilvl"))
                if numId_e is not None:
                    numId = numId_e.get(qn("w:val"))
                ilvl = int(ilvl_e.get(qn("w:val"))) if ilvl_e is not None else 0
            if numId is None:  # fall back to the paragraph's style
                pStyle = pPr.find(qn("w:pStyle"))
                if pStyle is not None:
                    numId, ilvl = style_map.get(pStyle.get(qn("w:val")), (None, None))
        if numId is None or numId == "0":
            return {}
        ilvl = ilvl or 0
        return {"list_id": f"num{numId}", "list_level": ilvl,
                "list_ordered": ordered_map.get((numId, ilvl))}
