"""storage/output/*.json ile corpus_docx/*.docx eslesenleri review/<doc_id>/
altina kopyalar. LLM'i tekrar cagirmaz; sadece var olan dosyalari eslestirir.

Kullanim:
    python scripts/pair_review.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

CORPUS_DIR = Path("corpus_docx")
OUTPUT_DIR = Path("storage/output")
REVIEW_DIR = Path("review")


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
