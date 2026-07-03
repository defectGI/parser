"""Provider-agnostic LLM access.

Public surface:
    LLMClient   - the protocol every adapter satisfies
    LLMError    - raised on any model/transport failure
    get_client  - build a client from environment configuration

Environment (read from the real environment, then from a `.env` file at the repo
root if present — real environment variables always win):
    LLM_PROVIDER   "openai" (default, any OpenAI-compatible server) | "anthropic"
    LLM_MODEL      model id (required for openai; defaults to opus for anthropic)
    LLM_BASE_URL   API root, required for the openai provider (e.g. .../v1)
    LLM_API_KEY    optional; local servers need none, Anthropic can use its own
                   credential chain when omitted
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .base import LLMClient, LLMError

__all__ = ["LLMClient", "LLMError", "get_client"]


def get_client() -> LLMClient:
    """Construct an `LLMClient` from `LLM_*` environment variables."""
    load_dotenv()  # no-op if there's no .env file; never overrides a set env var
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    model = os.getenv("LLM_MODEL") or None
    api_key = os.getenv("LLM_API_KEY") or None

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model=model, api_key=api_key)

    # Known OpenAI-compatible hosts get a default base URL so LLM_BASE_URL is optional.
    _COMPAT_DEFAULT_URL = {"openrouter": "https://openrouter.ai/api/v1"}
    if provider in ("openai", "openai-compat", "openai_compatible", "local",
                    "openrouter"):
        base_url = os.getenv("LLM_BASE_URL") or _COMPAT_DEFAULT_URL.get(provider)
        if not base_url:
            raise LLMError("LLM_BASE_URL is required for the openai provider")
        if not model:
            raise LLMError("LLM_MODEL is required for the openai provider")
        from .openai_compat import OpenAICompatClient

        # OpenRouter reasoning models hide their answer behind a "reasoning" field
        # and can burn the whole token budget thinking, leaving content empty on
        # short-answer tasks. Disable it by default; set LLM_REASONING=1 to keep it.
        extra_body: dict = {}
        reasoning_on = os.getenv("LLM_REASONING", "").strip().lower() in (
            "1", "true", "yes", "on")
        if provider == "openrouter" and not reasoning_on:
            extra_body["reasoning"] = {"enabled": False}

        return OpenAICompatClient(base_url=base_url, model=model, api_key=api_key,
                                  extra_body=extra_body)

    raise LLMError(f"unknown LLM_PROVIDER: {provider!r}")
