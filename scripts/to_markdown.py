"""Parse a document to IR, enrich it (image OCR + table descriptions), and render
the result to Markdown -- the CLI face of render/markdown.py.

Usage:
    python scripts/to_markdown.py <path-to-document> [doc_id]

Writes <output-dir>/<doc_id>.json (the IR) and <output-dir>/<doc_id>.md (the
rendered Markdown). Output dir defaults to storage/output (STORAGE_OUTPUT_DIR).

Enrichment (image OCR, table descriptions) uses the configured LLM/VLM (.env,
see llm/). Each enrichment stage is best-effort: if a model call fails the parse
+ render still complete, just without that enrichment -- so you always get a .md.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from images.image_handler import handle_images
from parsers.registry import parser_for
from render.markdown import to_markdown
from storage_paths import output_dir
from tables.table_describe import describe_tables


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    raw_path = Path(sys.argv[1])
    doc_id = sys.argv[2] if len(sys.argv) > 2 else raw_path.stem

    t0 = time.time()
    parser = parser_for(raw_path)
    print(f"parsing with {type(parser).__name__} ...", flush=True)
    doc = parser.parse(raw_path, doc_id)
    doc.raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    print(f"parsed in {time.time() - t0:.1f}s: fmt={doc.fmt} "
          f"blocks={len(doc.blocks)} tables={len(doc.tables())} "
          f"images={len(doc.images())}", flush=True)

    try:
        print("resolving images (OCR) ...", flush=True)
        handle_images(doc)
        ocr = sum(1 for im in doc.images() if im.ocr_meaningful)
        print(f"images resolved ({ocr} with meaningful OCR)", flush=True)
    except Exception as e:  # best-effort: keep the parse + render
        print(f"!! image stage error, continuing: {e!r}", flush=True)

    if doc.tables():
        try:
            print("describing tables (LLM) ...", flush=True)
            describe_tables(doc)
            print("tables described", flush=True)
        except Exception as e:
            print(f"!! table stage error, continuing: {e!r}", flush=True)

    out_json = output_dir() / f"{doc_id}.json"
    doc.save(out_json)
    md = to_markdown(doc)
    out_md = output_dir() / f"{doc_id}.md"
    out_md.write_text(md, encoding="utf-8")

    print(f"saved IR -> {out_json}", flush=True)
    print(f"saved MD -> {out_md}  ({len(md)} chars, {md.count(chr(10)) + 1} lines)",
          flush=True)
    print(f"total {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
