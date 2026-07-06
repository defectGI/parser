"""Manual smoke test: parse one raw document and enrich its tables via the LLM.

Not a unit test (no assertions) — a runner to eyeball real output end to end
while there is no test suite yet.

Usage:
    python scripts/test_pipeline.py <path-to-document> [doc_id]

Writes the resulting IR to <output-dir>/<doc_id>.json and prints a summary
(block/table/image counts, each table's description and check status).
Output dir defaults to storage/output, overridable via STORAGE_OUTPUT_DIR
(see storage_paths.py).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from images.image_handler import handle_images
from parsers.registry import parser_for
from storage_paths import output_dir
from tables.table_describe import describe_tables


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    raw_path = Path(sys.argv[1])
    doc_id = sys.argv[2] if len(sys.argv) > 2 else raw_path.stem

    parser = parser_for(raw_path)
    doc = parser.parse(raw_path, doc_id)
    doc.raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()

    print(f"parsed: fmt={doc.fmt} blocks={len(doc.blocks)} "
          f"tables={len(doc.tables())} images={len(doc.images())}")

    print("resolving images...")
    handle_images(doc)

    if doc.tables():
        print("describing tables via LLM...")
        describe_tables(doc)

    out_path = output_dir() / f"{doc_id}.json"
    doc.save(out_path)
    print(f"saved IR -> {out_path}")

    for block in doc.tables():
        print("-" * 60)
        print(f"table {block.id}: {block.table.n_rows}x{block.table.n_cols}")
        print(f"description: {block.table_description}")
        if block.describe_status:
            print(f"check: {block.describe_status} ({block.describe_attempts} attempt(s))")


if __name__ == "__main__":
    main()
