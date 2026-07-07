# parser

A modular parser that converts different file formats (docx, pptx, xlsx, html, pdf, markdown)
into a common intermediate representation (IR — `ParsedDocument`), runs images through OCR to
verify their meaningfulness, and structures and describes tables.

The IR is serialized as JSON (`.json` files under `storage/output/`). All enrichment results —
image OCR, table description, etc. — are written back directly into this IR JSON rather than
into a separate database. Raw image bytes are never embedded in the JSON; only an `image_id`
reference is carried.

Chunking, RAPTOR, and the chunk schema are out of scope for this repo; the parser only produces
the IR.

For what each format can currently do, see [`SCOPE.txt`](SCOPE.txt).

## Pipeline

1. `storage/raw/` — the input file is kept as-is.
2. `parsers/` — the parser appropriate to the file type converts it to the common
   `ParsedDocument` IR → `storage/output/`.
3. `images/` — runs the `<imageN>` markers placed during parsing through OCR, verifies their
   meaningfulness with an LLM, and writes the result back into the IR in place of the marker.
   The raw image is stored immutably and deduplicated by sha256 (`image_id`) in the
   `storage/images/` blob store; the record itself lives on the `ImageBlock` in the IR.
4. `tables/` — adds a short description (`table_description`) to structured table blocks and
   runs the result through an LLM check; the result is written to the `TableBlock` in the IR.
5. `webapp/` — a developer interface that shows the parser stages step by step, in a
   start-stop mode.

## Folders

- `parsers/` — format-specific parsers + the `BaseParser`/`ParsedDocument` contract
- `images/` — `image_handler` and `ocr_output_control`
- `tables/` — `table_describe`; `tables/structure/` is optional table-grid detection
  (`TABLE_STRUCT_PROVIDER=vlm|tableformer|http`, see `tables/structure/__init__.py`)
- `storage/` — raw data (`raw/`), resulting IR output (`output/`), image blob store (`images/`);
  these three paths are not hardcoded — they can be overridden via the `STORAGE_RAW_DIR` /
  `STORAGE_OUTPUT_DIR` / `STORAGE_IMAGES_DIR` env variables through `storage_paths.py`
  (see `.env.example`) — otherwise they fall back to the dev-time defaults here.
- `webapp/` — developer interface
