# storage/output/

The final IR (`ParsedDocument`) is kept here as a JSON file (`{doc_id}.json`). The parser's
output is written here first; enrichments from the `images/` and `tables/` stages (OCR text,
table description, LLM check status, etc.) are then processed back into the same IR. There is
no separate database — all results and state live in these files.

Raw image bytes are not embedded here; an image is only referenced by its `image_id` (sha256),
with the bytes themselves kept in the `storage/images/` blob store.
