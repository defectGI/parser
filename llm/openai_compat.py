"""OpenAI-compatible Chat Completions adapter over raw HTTP.

This is the lingua franca that lets one client reach almost anything: Ollama,
vLLM, llama.cpp, LM Studio, TGI and most hosted APIs all speak the
`POST {base_url}/chat/completions` shape. Implemented with stdlib `urllib` so
the core has no third-party HTTP dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0,
            }
        ).encode("utf-8")

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
