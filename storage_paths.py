"""Central storage-path configuration: where raw input files, parsed IR
output, and the image/crop blob store live. All three default to the
dev-time `storage/` layout (see storage/README.md) but are overridable via
environment variables (and `.env`, loaded the same way llm/ loads model
config) so a deployment isn't stuck with paths baked into scripts.

Environment (all optional):
    STORAGE_RAW_DIR      raw input files (default storage/raw)
    STORAGE_OUTPUT_DIR   parsed IR JSON, one file per doc_id (default storage/output)
    STORAGE_IMAGES_DIR   image/crop blob store, sha256-keyed (default storage/images)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; never overrides a set env var


def raw_dir() -> Path:
    return Path(os.getenv("STORAGE_RAW_DIR", "storage/raw"))


def output_dir() -> Path:
    return Path(os.getenv("STORAGE_OUTPUT_DIR", "storage/output"))


def images_dir() -> Path:
    return Path(os.getenv("STORAGE_IMAGES_DIR", "storage/images"))
