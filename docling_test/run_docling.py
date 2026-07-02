"""Docling ile bir dosyayi parse edip markdown + json cikti alir.

Amac: ayni dosyayi hem bizim parsers/ ile hem docling ile parse edip
sonuclari karsilastirmak. Bu klasor kendi venv'ini kullanir (bkz. .venv),
ana projenin requirements.txt'i ile karismaz.

Kullanim:
    .venv/Scripts/python.exe run_docling.py <dosya-yolu>

Cikti storage: docling_test/output/<dosya-adi>.md ve .json
"""

from __future__ import annotations

import sys
from pathlib import Path

from docling.document_converter import DocumentConverter


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    src = Path(sys.argv[1])
    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(exist_ok=True)

    converter = DocumentConverter()
    result = converter.convert(str(src))
    doc = result.document

    md_path = out_dir / f"{src.stem}.md"
    json_path = out_dir / f"{src.stem}.json"

    md_path.write_text(doc.export_to_markdown(), encoding="utf-8")
    json_path.write_text(doc.export_to_json(), encoding="utf-8")

    print(f"parsed: {src.name}")
    print(f"markdown -> {md_path}")
    print(f"json     -> {json_path}")


if __name__ == "__main__":
    main()
