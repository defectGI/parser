"""Provider-agnostic access to a table STRUCTURE-recognition model.

Public surface:
    TableStructureClient    the protocol every adapter satisfies (base.py)
    TableStructureError     raised on any model/transport failure
    DetectedTable/DetectedCell/TextCellHint   the neutral data shapes
    get_table_structure_client   build a client from environment configuration

Mirrors llm/__init__.py's shape. Environment (real environment wins over a
`.env` file, same as llm/):
    TABLE_STRUCT_PROVIDER   "tableformer" | "http" | "vlm". Unset/empty ->
                            the feature is off; callers should treat
                            get_table_structure_client() raising
                            TableStructureError as "not configured" and fall
                            back to a line-geometry table detector.
    TABLE_STRUCT_MODEL      provider-specific model/variant selector; for
                            "vlm" this OVERRIDES just the model id (same
                            server/provider as VLM_*) -- leave unset to
                            reuse VLM_MODEL as-is
    TABLE_STRUCT_BASE_URL   required for provider="http"
    TABLE_STRUCT_API_KEY    optional, provider="http" only

provider="vlm" is the no-extra-dependency option: it reuses whichever VLM is
already configured via VLM_*/LLM_* (llm.get_vlm_client()) -- any Ollama
model, hosted API, etc. you already run for OCR/hybrid-page reading -- to
read table structure via a vision prompt instead of a dedicated
structure-recognition model. Less precise about cell boundaries than
TableFormer, but needs nothing beyond what the parser already installs.
Grid/bbox extraction is a different task than that VLM's usual OCR job
though, so set TABLE_STRUCT_MODEL to point it at a different model already
pulled on the same server if one reads layout/structure better than your
OCR model does.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .base import (
    DetectedCell,
    DetectedTable,
    TableStructureClient,
    TableStructureError,
    TextCellHint,
)

__all__ = [
    "DetectedCell",
    "DetectedTable",
    "TableStructureClient",
    "TableStructureError",
    "TextCellHint",
    "get_table_structure_client",
]


def get_table_structure_client() -> TableStructureClient:
    """Construct a table-structure client from `TABLE_STRUCT_*` environment
    variables. Raises `TableStructureError` when unconfigured or on an
    unknown provider, so callers can treat that as "feature not enabled"."""
    load_dotenv()  # no-op if there's no .env file; never overrides a set env var

    provider = (os.getenv("TABLE_STRUCT_PROVIDER") or "").strip().lower()
    model = os.getenv("TABLE_STRUCT_MODEL") or None

    if not provider:
        raise TableStructureError("TABLE_STRUCT_PROVIDER is not set")

    if provider == "tableformer":
        from .tableformer_adapter import TableFormerAdapter

        return TableFormerAdapter(model=model)

    if provider == "http":
        base_url = os.getenv("TABLE_STRUCT_BASE_URL")
        if not base_url:
            raise TableStructureError(
                "TABLE_STRUCT_BASE_URL is required for provider='http'")
        api_key = os.getenv("TABLE_STRUCT_API_KEY")
        from .http_client import HttpTableStructureClient

        return HttpTableStructureClient(base_url=base_url, api_key=api_key)

    if provider == "vlm":
        from .vlm_adapter import VLMStructureAdapter

        return VLMStructureAdapter(model=model)

    raise TableStructureError(f"unknown TABLE_STRUCT_PROVIDER: {provider!r}")
