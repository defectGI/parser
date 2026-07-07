# storage/

The parser's runtime data. Data folders, not code.

- `raw/` — raw input files. Not deleted once processing finishes; they're kept.
- `output/` — the resulting IR output (`ParsedDocument`), as JSON. Image/table enrichment
  results are also written back into this IR, so the final result lives here.
- `images/` — the image blob store. Addressed by sha256 (`image_id`), immutable and
  deduplicated (if the same image occurs again, only one copy is kept).

Note: there is no separate database; all records/state are kept in the IR JSON. The chunk
schema also does not belong to this store; chunking is a separate component's responsibility.
