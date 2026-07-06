"""Provider-agnostic LLM access.

Public surface:
    LLMClient      - the text protocol every adapter satisfies
    VLMClient      - the vision protocol (text + attached images)
    LLMError       - raised on any model/transport failure
    get_client     - build a text client from environment configuration
    get_vlm_client - build a vision client ("primary" or "secondary" role)

Environment (read from the real environment, then from a `.env` file at the repo
root if present — real environment variables always win). Every variable exists
in three prefixes; `VLM_*` falls back to `LLM_*` per variable (a single
configured multimodal model serves both roles), `VLM2_*` never falls back (the
secondary verifier must be an independently chosen model):
    {LLM,VLM,VLM2}_PROVIDER  "openai" (default, any OpenAI-compatible server)
                             | "openrouter" | "anthropic"
    {LLM,VLM,VLM2}_MODEL     model id (required for openai; defaults to opus
                             for anthropic)
    {LLM,VLM,VLM2}_BASE_URL  API root, required for the openai provider (.../v1)
    {LLM,VLM,VLM2}_API_KEY   optional; local servers need none, Anthropic can
                             use its own credential chain when omitted
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .base import LLMClient, LLMError, VLMClient

__all__ = ["LLMClient", "VLMClient", "LLMError", "get_client", "get_vlm_client"]


def get_client() -> LLMClient:
    """Construct a text `LLMClient` from `LLM_*` environment variables."""
    return _build_client("LLM")


def get_vlm_client(role: str = "primary") -> VLMClient:
    """Construct a vision `VLMClient` from environment variables.

    role="primary"    reads `VLM_*`, falling back to `LLM_*` per variable.
    role="secondary"  reads `VLM2_*` only; raises `LLMError` when unset. The
                      secondary model independently re-reads suspicious regions
                      (consensus check), so silently reusing the primary would
                      defeat its purpose.
    """
    if role == "primary":
        return _build_client("VLM", fallback="LLM")
    if role == "secondary":
        load_dotenv()
        if not (os.getenv("VLM2_PROVIDER") or os.getenv("VLM2_MODEL")):
            raise LLMError("secondary VLM not configured (set VLM2_* variables)")
        return _build_client("VLM2")
    raise ValueError(f"unknown VLM role: {role!r}")


def _env(prefix: str, name: str, fallback: str | None) -> str | None:
    """`{prefix}_{name}`, else `{fallback}_{name}`, else None."""
    val = os.getenv(f"{prefix}_{name}")
    if val is None and fallback:
        val = os.getenv(f"{fallback}_{name}")
    return val or None


def _build_client(prefix: str, fallback: str | None = None):
    """Build an adapter from `{prefix}_*` environment variables."""
    load_dotenv()  # no-op if there's no .env file; never overrides a set env var
    provider = (_env(prefix, "PROVIDER", fallback) or "openai").strip().lower()
    model = _env(prefix, "MODEL", fallback)
    api_key = _env(prefix, "API_KEY", fallback)

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model=model, api_key=api_key)

    # Known OpenAI-compatible hosts get a default base URL so *_BASE_URL is optional.
    _COMPAT_DEFAULT_URL = {"openrouter": "https://openrouter.ai/api/v1"}
    if provider in ("openai", "openai-compat", "openai_compatible", "local",
                    "openrouter"):
        base_url = _env(prefix, "BASE_URL", fallback) or _COMPAT_DEFAULT_URL.get(provider)
        if not base_url:
            raise LLMError(f"{prefix}_BASE_URL is required for the openai provider")
        if not model:
            raise LLMError(f"{prefix}_MODEL is required for the openai provider")
        from .openai_compat import OpenAICompatClient

        # OpenRouter reasoning models hide their answer behind a "reasoning" field
        # and can burn the whole token budget thinking, leaving content empty on
        # short-answer tasks. Disable it by default; set LLM_REASONING=1 to keep it.
        extra_body: dict = {}
        reasoning_on = (_env(prefix, "REASONING", fallback) or "").strip().lower() in (
            "1", "true", "yes", "on")
        if provider == "openrouter" and not reasoning_on:
            extra_body["reasoning"] = {"enabled": False}

        return OpenAICompatClient(base_url=base_url, model=model, api_key=api_key,
                                  extra_body=extra_body)

    raise LLMError(f"unknown {prefix}_PROVIDER: {provider!r}")
