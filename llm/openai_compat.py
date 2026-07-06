"""OpenAI-compatible Chat Completions adapter over raw HTTP.

This is the lingua franca that lets one client reach almost anything: Ollama,
vLLM, llama.cpp, LM Studio, TGI and most hosted APIs all speak the
`POST {base_url}/chat/completions` shape. Implemented with stdlib `urllib` so
the core has no third-party HTTP dependency.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from .base import LLMError


class OpenAICompatClient:
    """Talks to any OpenAI-compatible `/chat/completions` endpoint.

    `base_url` is the API root without the trailing path, e.g.
    `http://localhost:11434/v1` (Ollama) or `https://api.openai.com/v1`.
    `api_key` is optional — local servers usually need none.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        extra_body: dict | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        # Merged into every request body — e.g. {"reasoning": {"enabled": False}}
        # to stop a reasoning model from spending the token budget on hidden
        # thinking (which leaves message.content null for short-answer tasks).
        self.extra_body = extra_body or {}

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str:
        return self._chat(system=system, user_content=user, max_tokens=max_tokens)

    def complete_vision(self, *, system: str, user: str,
                        images: Sequence[tuple[str, bytes]],
                        max_tokens: int = 2048) -> str:
        # Multimodal user turn: text part + one data-URI image part per image.
        # The data-URI `image_url` shape is the de-facto standard understood by
        # OpenAI, vLLM, Ollama, LM Studio and OpenRouter alike.
        content: list[dict] = [{"type": "text", "text": user}]
        for mime, data in images:
            b64 = base64.b64encode(data).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}})
        return self._chat(system=system, user_content=content, max_tokens=max_tokens)

    def _chat(self, *, system: str, user_content, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
            **self.extra_body,
        }
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {exc.code} from {self.base_url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"cannot reach {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"non-JSON response from {self.base_url}: {exc}") from exc

        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {payload!r:.500}") from exc
