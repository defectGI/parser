"""llm/__init__.py: provider resolution and the model-override parameter
(added so a caller with a different task than a role's usual job -- e.g.
tables/structure/vlm_adapter.py's grid/bbox extraction vs. the VLM role's
normal OCR transcription -- can point at a different model on the same
server without needing its own separate provider/base_url/api_key config).
"""

from __future__ import annotations

import pytest

from llm import LLMError, get_client, get_vlm_client


def _clear_llm_env(monkeypatch):
    for prefix in ("LLM", "VLM", "VLM2"):
        for name in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)


def test_ollama_provider_defaults_base_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")

    client = get_client()
    assert client.base_url == "http://localhost:11434/v1"
    assert client.model == "llama3.1"


def test_vlm_falls_back_to_llm_per_variable(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    monkeypatch.setenv("VLM_MODEL", "qwen2.5vl")  # only VLM_MODEL overridden

    vlm = get_vlm_client()
    assert vlm.base_url == "http://localhost:11434/v1"  # fell back to LLM_*
    assert vlm.model == "qwen2.5vl"  # VLM_MODEL wins over the fallback


def test_vlm_model_override_keeps_provider_and_base_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("VLM_PROVIDER", "ollama")
    monkeypatch.setenv("VLM_MODEL", "qwen2.5vl")

    vlm = get_vlm_client(model="llava")
    assert vlm.model == "llava"  # override wins
    assert vlm.base_url == "http://localhost:11434/v1"  # unchanged


def test_vlm_secondary_requires_vlm2_config(monkeypatch):
    _clear_llm_env(monkeypatch)
    # get_vlm_client calls load_dotenv() internally, which would otherwise
    # repopulate VLM2_* from this repo's own real .env (it has Ollama VLM2
    # settings for actual use) -- stub it out so this test genuinely
    # exercises the "nothing configured" path.
    import llm

    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(LLMError):
        get_vlm_client("secondary")


def test_vlm_secondary_model_override(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("VLM2_PROVIDER", "ollama")
    monkeypatch.setenv("VLM2_MODEL", "llava")

    vlm2 = get_vlm_client("secondary", model="bakllava")
    assert vlm2.model == "bakllava"
