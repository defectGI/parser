"""Copies storage/output/*.json entries that match corpus_docx/*.docx files into
review/<doc_id>/. Does not call the LLM again; it only pairs up existing files.

Kullanim:
    python scripts/pair_review.py

Environment (all optional):
    PAIR_REVIEW_CORPUS_DIR   raw docx corpus to match against (default corpus_docx)
    PAIR_REVIEW_DIR          where matched pairs are copied (default review)
    parsed IR output dir is the shared STORAGE_OUTPUT_DIR (see storage_paths.py)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_paths import output_dir

CORPUS_DIR = Path(os.getenv("PAIR_REVIEW_CORPUS_DIR", "corpus_docx"))
OUTPUT_DIR = output_dir()
REVIEW_DIR = Path(os.getenv("PAIR_REVIEW_DIR", "review"))


def main() -> None:
    paired = 0
    for json_path in OUTPUT_DIR.glob("*.json"):
        doc_id = json_path.stem
        raw_path = CORPUS_DIR / f"{doc_id}.docx"
        if not raw_path.exists():
            continue
        pair_dir = REVIEW_DIR / doc_id
        pair_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw_path, pair_dir / raw_path.name)
        shutil.copy2(json_path, pair_dir / "result.json")
        paired += 1
    print(f"paired {paired} doc(s) -> {REVIEW_DIR}/<doc_id>/")


if __name__ == "__main__":
    main()
