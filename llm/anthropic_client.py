"""Anthropic Messages API adapter (optional).

Used when `LLM_PROVIDER=anthropic`. Requires the `anthropic` SDK to be
installed; the import is deferred so the core package works without it.
Credentials resolve through the SDK's own chain (ANTHROPIC_API_KEY, then an
`ant auth login` profile), so `LLM_API_KEY` is optional here.
"""

from __future__ import annotations

from .base import LLMError

# Opus 4.8 and the rest of the 4.6+ family reject a `temperature` argument, so
# this adapter never sends one.
DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicClient:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
            raise LLMError(
                "LLM_PROVIDER=anthropic requires the 'anthropic' package"
            ) from exc

        self.model = model or DEFAULT_MODEL
        # api_key=None lets the SDK fall back to its own credential resolution.
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str:
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # SDK raises many concrete types; normalize them.
            raise LLMError(f"anthropic request failed: {exc}") from exc

        return "".join(
            block.text for block in msg.content if getattr(block, "type", None) == "text"
        )
