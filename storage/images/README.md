# storage/images/

The immutable blob store for images. Addressed by sha256 (`image_id`); if the same image
occurs again, only one copy is kept (dedup). Raw image bytes are never embedded in the parse
IR — they're only carried from here via the `image_id` reference.
