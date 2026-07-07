# images/

Processes the image markers (`<imageN>`, each an `ImageBlock`) placed into the IR during
parsing.

- `image_handler.py` — locates the raw image based on each `ImageBlock`'s `locator` (docx/pptx/
  xlsx: zip media part; html/markdown: data-uri or local file; pdf: the page is rendered and
  cropped from the bbox), runs it through OCR with a VLM, and verifies the result with
  `ocr_output_control.py`; if meaningful, writes the corrected text into `ImageBlock.ocr_text`
  — i.e. into the marker's *own slot* in the IR, not into the surrounding paragraph/cell text
  (that text's `Span` must stay byte-exact to the source). Regardless of whether it's meaningful
  or not, stores the image immutably and deduplicated by sha256 (`image_id`) in the
  `storage/images/` blob store. The image's record (image_id, locator, ocr_text,
  ocr_meaningful, mime, width/height) is kept on the `ImageBlock` in the IR; `doc_id` and
  `access_level` come from the document. There is no separate database. Images inside table
  cells (including nested tables at any depth) are processed too. A remote (http/https) `src`
  is left unresolved — the pipeline does not make network requests.
- `ocr_output_control.py` — asks an LLM whether the OCR output is meaningful, and fixes
  spelling mistakes and format corruption.

Rule: the raw image bytes are never embedded in the IR — only the `image_id` reference is
carried. If the OCR is not meaningful, `ocr_text` stays empty (only `ocr_meaningful=False` is
set) but the blob + the record in the IR are kept — resolving the marker at search/display time
(substituting it with text, or dropping it) is the read-time consumer's (webapp/chunker) job.
