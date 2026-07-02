"""Provider-agnostic LLM interface.

The rest of the codebase talks to models only through `LLMClient`. Adapters
(OpenAI-compatible HTTP, Anthropic SDK, ...) implement this one method, so a
local model served over HTTP and a hosted API are interchangeable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """Any failure while talking to a model (transport, HTTP, bad payload)."""


@runtime_checkable
class LLMClient(Protocol):
    """Minimal text-in / text-out chat contract.

    A single system + user turn is all the enrichment stages need. Streaming,
    tools and multi-turn history are intentionally out of scope here; adapters
    may expose extra capabilities (e.g. batch) on their own concrete classes.
    """

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str:
        """Return the assistant's text reply. Raises `LLMError` on failure."""
        ...
