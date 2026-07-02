"""Provider-agnostic LLM access.

Public surface:
    LLMClient   - the protocol every adapter satisfies
    LLMError    - raised on any model/transport failure
    get_client  - build a client from environment configuration

Environment:
    LLM_PROVIDER   "openai" (default, any OpenAI-compatible server) | "anthropic"
    LLM_MODEL      model id (required for openai; defaults to opus for anthropic)
    LLM_BASE_URL   API root, required for the openai provider (e.g. .../v1)
    LLM_API_KEY    optional; local servers need none, Anthropic can use its own
                   credential chain when omitted
"""

from __future__ import annotations

import os

from .base import LLMClient, LLMError

__all__ = ["LLMClient", "LLMError", "get_client"]


def get_client() -> LLMClient:
    """Construct an `LLMClient` from `LLM_*` environment variables."""
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    model = os.getenv("LLM_MODEL") or None
    api_key = os.getenv("LLM_API_KEY") or None

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model=model, api_key=api_key)

    if provider in ("openai", "openai-compat", "openai_compatible", "local"):
        base_url = os.getenv("LLM_BASE_URL")
        if not base_url:
            raise LLMError("LLM_BASE_URL is required for the openai provider")
        if not model:
            raise LLMError("LLM_MODEL is required for the openai provider")
        from .openai_compat import OpenAICompatClient

        return OpenAICompatClient(base_url=base_url, model=model, api_key=api_key)

    raise LLMError(f"unknown LLM_PROVIDER: {provider!r}")
