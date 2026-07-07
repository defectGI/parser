# parsers/

A separate parser module per file format, each conforming to the common `BaseParser`
contract, converting the input file into the `ParsedDocument` IR.

- `base.py` — `BaseParser` (abstract interface) and `ParsedDocument` (IR) definitions. Every
  format parser implements this.
- `registry.py` — selects the right parser based on file extension/mimetype.
- `docx_parser.py`, `pptx_parser.py`, `xlsx_parser.py`, `html_parser.py`, `pdf_parser.py`,
  `markdown_parser.py` — format-specific implementations.

Notes:
- For what each format can currently do, see `SCOPE.txt` in the root directory.
- markitdown is not used: it doesn't preserve byte offset information from the original
  document, and the IR requires it.
- `pdf_parser.py` implements the "PDF pipeline" flow below. LLM access is model- and
  provider-agnostic: `llm.get_vlm_client()` (env: `VLM_*`, falls back to `LLM_*`; the second
  verifier model is `VLM2_*`). If the VLM isn't configured, the parser degrades gracefully:
  the hybrid page falls back to the code path, a scanned page stays a full-page `ImageBlock`
  (OCR is then done by the `images/` stage).
- Table blocks, including merges, are written to the IR here as structured JSON; generating
  the description is the `tables/` module's job.
- Wherever an image occurs, a marker like `<imageN>` is placed; filling in the marker is the
  `images/` module's job.
- Lists are not a separate container block: the IR is a flat block stream, and list membership
  is written onto ordinary blocks as metadata (`list_id` / `list_level` / `list_ordered`). This
  way, a table/image inside a list item is preserved as a real `TableBlock`/`ImageBlock` (not
  flattened into plain text); this also aligns with how docx (`w:numId`/`w:ilvl`) and pdf store
  lists. A "list" is reconstructed by grouping blocks that share the same `list_id`.

## PPTX pipeline

`pptx_parser.py` walks slides shape by shape (`_walk_shapes`), descending recursively into
group shapes (PowerPoint allows multiple shapes to be grouped; without recursion, text/table/
image inside a group would be lost entirely).

- **Heading**: a real TITLE/CENTER_TITLE/VERTICAL_TITLE placeholder is definitive and always
  wins (exact enum match — the old "TITLE" substring check was accidentally also catching
  SUBTITLE; this was fixed). If there is no placeholder (a slide designed to "look like a
  heading" using a free text-box), the same pattern as docx's pseudo-header logic runs: the
  slide's non-placeholder candidate paragraphs are scored with `PptxHeadingConfig` (bold/caps/
  title-case/centered/short/isolation/underlined/font-ratio weights + threshold), and the
  single highest-scoring candidate that clears the threshold is promoted to a HeadingBlock (one
  heading per slide). Plain-text clues are shared with docx via `heading_heuristics.py`; font/
  bold/underline/italic signals are read both from the run's own rPr and from the paragraph's
  default (a:pPr/a:defRPr).
- **Inline formatting**: `_walk_pptx_para` walks each `a:p` in order, converting a:r/a:fld into
  InlineRun (Mark: bold/italic/underline/strike/superscript/subscript), following OOXML's own
  resolution rule (if a run doesn't set a property in its own rPr, it falls back to the
  paragraph's a:pPr/a:defRPr — the same as docx's `_walk_para`). a:br (soft line break) is
  converted to an unmarked space; python-pptx returns it as a raw `\x0b` (vertical tab), which
  would otherwise leak a control character into the text, including into headings, if not
  cleaned up.
- **Lists**: not a separate container like in docx — written onto blocks as `list_id` (slide+
  shape based) / `list_level` (paragraph indent) / `list_ordered` (a:buAutoNum vs a:buChar)
  metadata.
- **Tables**: `TableBlock`, including merges (gridSpan/rowSpan, read via python-pptx's
  merge-cell API). Cells are still plain text (`text_cell`) — the docx parallel of `Cell.blocks`
  (in-cell runs/nested table/image) doesn't exist yet.
- **Images**: PICTURE shape -> `ImageBlock` (locator = media part path).
- **Embedded OLE objects** (e.g. an Excel table pasted into a slide): the raster preview that
  OOXML mandates (`mc:Fallback/p:oleObj/p:pic/blipFill/a:blip`, or `p:oleObj/p:pic` directly
  without AlternateContent) is extracted as an `ImageBlock`; `ole_format.prog_id` is written to
  `alt_text` (e.g. "Embedded object (Excel.Sheet.12)"). The preview usually comes in EMF
  (vector metafile, `image/x-emf`) format — not PNG/JPEG; the `images/` stage needs to
  rasterize it first, otherwise the OCR/vision model can't read it. If there's no preview
  (rare), no block is produced at all.
- **Speaker notes**: if `slide.notes_slide` exists, it's kept as a separate `ParagraphBlock`,
  distinguished from the body by `Span(part="ppt/notesSlides/notesSlideN.xml")`; consumers can
  distinguish body/notes by looking at span.part.
- **Locator**: shapes don't have a meaningful byte offset; `Span(part="ppt/slides/
  slideN.xml", page=N)` is used (Decision B best-effort, same as in the PDF pipeline).

### Known v1 limitations

- Table cells are plain text (`text_cell`); in-cell formatting/nested-table/image is not
  modeled.
- OLE previews can come as EMF; without rasterization they can't go through the OCR/vision
  stage.

## PDF pipeline

Unlike other formats, PDF does not follow a single lossless path; it's split into three paths
depending on the source, and content produced by the VLM is verified against an independent
source. `pdf_parser.py` implements this; triage is done per page (a single PDF can mix scanned
and digital pages), and the per-page decision is written to `metadata["pdf_pages"]`.
Provenance labels are carried in the IR on the `Block.provenance` / `Block.source_crop` fields
(see `base.py`).

### 0. Triage

Using pdfplumber, is there a text layer, and what's the coverage ratio?

- No text layer → **Scanned path**
- Text layer present + simple layout → **Code path**
- Text layer present + complex region (table/multi-column detected) → **Hybrid path**

### 1. Code path (born-digital, simple)

pdfplumber → IR blocks. Lossless, deterministic. Tagged `text-layer-verified`. Done.

### 2. Hybrid path (born-digital, complex)

1. Render the page
2. Feed to the VLM, adding the text layer to the prompt as grounding (document anchoring)
3. Fuzzy-match the output against the text layer
4. Match → `text-layer-verified`, no match → goes to Verification

### 3. Scanned path

1. Render
2. Text detector (bboxes)
3. VLM reading
4. Goes to Verification

### 4. Verification (for content without a text layer)

- Every VLM line must map to a detector bbox; one that doesn't map is suspected fabrication
- Suspicious/critical regions: crop-and-reread with a second model, fuzzy-match
- Domain regex (part number, unit, date)
- Agreement → `consensus-verified`, disagreement → **warning, no automatic resolution**

### 5. Output

IR blocks + a provenance tag on every block + a source crop reference (blob store hash) for
`unverified` ones. The citation pipeline reads its trust level from here.

**Summary:** Read with code if it's cheap, read with the VLM if you must, check everything the
VLM says against an independent source, and tag and keep a crop of whatever it couldn't verify.

### Configuration

Models (all optional; if none are set the parser runs with deterministic fallbacks):

- `VLM_*` — the primary vision model (`VLM_PROVIDER/MODEL/BASE_URL/API_KEY`; any variable
  that's missing falls back to its `LLM_*` counterpart).
- `VLM2_*` — the independent second model that verifies suspicious regions via
  crop-and-reread. Deliberately has no fallback: the verifier must be chosen independently of
  the primary model.
- Scanned-path text detector: used automatically if `pytesseract` is installed (language via
  `PDF_TESSERACT_LANG`); otherwise verification relies on VLM2.

Settings: `PDF_VLM=0` (disable all VLM use), `PDF_RENDER_DPI` (150),
`PDF_CROP_DIR` (storage/images), `PDF_VLM_MAX_TOKENS` (8192), `PDF_CONTAINMENT` (0.9),
`PDF_LINE_MATCH` (0.8).

### Table merges

pdfplumber doesn't give rowspan/colspan, but the cell bboxes it detects (`table.cells`) carry
merge information geometrically: a merged cell appears as a single bbox spanning multiple grid
boundaries; the other grid positions it covers have no bbox of their own. From this distinction
(covered-and-empty vs. genuinely borderless/empty cell), `Merge` records are derived
deterministically (`_table_to_data`). The hybrid (VLM) path uses the same geometric table too:
the VLM's "table" block is matched in reading order against the page's pdfplumber table, and
instead of the VLM's flat JSON grid, the real merge-aware table from the text layer is used —
if the counts don't match, the unmatched table falls back to the VLM's own grid.

### Heading level

In the code path, whether a line is a heading is still decided locally based on the median body
text size of its own column/page (`_is_heading`) — that hasn't changed. But which level (1-6)
it corresponds to is now determined by a **document-wide** font-size ranking: before actually
processing the pages, `parse()` scans all "code" and "hybrid" pages (including hybrid pages
that may fall back to the code path if left without a VLM) and collects all heading-candidate
sizes into a single set (`_page_heading_candidates`), sorts them, and passes this shared list to
every page (as the `heading_sizes` parameter to `_specs_from_lines`). This way, a section
heading on page 50 isn't incorrectly promoted to level 1 just because there's nothing bigger
than it on that page — if page 1 has a bigger heading, it stays level 2 (or lower), producing a
hierarchy that's consistent across the whole document. On hybrid pages where the VLM read
successfully, the heading level is still the VLM's own estimate (that's a separate mechanism);
this ranking only kicks in for hybrid pages that end up falling back to the code path due to a
missing or failed VLM.

### Inline formatting (runs)

Bold/italic is always read from the word's own font name (e.g. "Arial-BoldMT") — the PDF
equivalent of reading a run's rPr in docx: deterministic, not a VLM guess. In the code path
this is direct (`_word_marks`/`_line_runs`). In the hybrid path, the page's own word+font
stream is extracted (`_word_marks_stream`, the same reading order as the code path's column/
gutter logic); if a block's text has already been verified against the text layer and is
`text-layer-verified`, that text's own words are aligned to this stream with a **forward-only,
never-rewinding** pointer (`_align_runs`), and matched words get their own font marks. Blocks
that couldn't be verified (`consensus-verified`/`unverified`) never go through alignment at all
— their text doesn't already match the text layer, so alignment wouldn't be reliable either.
On a scanned page (no text layer at all), `runs` is not filled in; there's no independent font
source to verify against, so this is out of scope.

### Known v1 limitations

- On both hybrid and scanned pages, figures the VLM calls out but that have no counterpart in
  pdfplumber's object model (e.g. a vector drawing, or a sub-figure embedded in a scanned
  background raster) remain an `unverified` `ImageBlock`; if the VLM supplied its own bbox
  guess, the audit crop is tightened to that guess, otherwise no crop is produced at all (a
  full-page dump would be misleading, so it's avoided).
- On a hybrid page, embedded images the VLM didn't count as a figure (i.e. the VLM missed them)
  are appended to the end of the page flow, without a position; ones the VLM did call a figure
  and that match a pdfplumber raster get both the reading-order position from the VLM and the
  real bbox from pdfplumber.
- On a scanned page, `runs` (inline formatting) is not filled in (see above).
- `Span` has no byte offset, only a page number.
