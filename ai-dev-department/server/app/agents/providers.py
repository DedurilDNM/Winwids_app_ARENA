"""Catalogue of model providers («aggregators») the API department can use.

Two protocols cover them all:
- "anthropic": Claude models through the official `anthropic` SDK;
- "openai": the OpenAI-compatible chat-completions protocol, spoken by most
  aggregators (OpenRouter, Groq, DeepSeek, Together, Mistral, ...). A «custom»
  provider takes its base URL from the key, for any other compatible service.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str  # "anthropic" | "openai"
    base_url: str = ""
    # GET path that needs a valid key (a key check without spending tokens); None = /models is public
    auth_check_path: str | None = "/models"
    key_hint: str = ""
    default_model: str = ""

    @property
    def needs_base_url(self) -> bool:
        return self.id == "custom"


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "/key", "sk-or-…"),
    Provider("anthropic", "Anthropic", "anthropic", "", None, "sk-ant-…", "claude-opus-5-5"),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", "/models", "sk-…"),
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "/models", "gsk_…"),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "/models", "sk-…", "deepseek-chat"),
    Provider("together", "Together", "openai", "https://api.together.xyz/v1", "/models", ""),
    Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "/models", ""),
    Provider("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
             "/models", "AIza…"),
    Provider("xai", "xAI", "openai", "https://api.x.ai/v1", "/models", "xai-…"),
    Provider("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1", "/models", "csk-…"),
    Provider("fireworks", "Fireworks", "openai", "https://api.fireworks.ai/inference/v1", "/models", "fw_…"),
    Provider("custom", "Свой (OpenAI-совместимый)", "openai", "", "/models", ""),
)}


def get_provider(provider_id: str) -> Provider:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ValueError(f"Unknown provider: {provider_id}") from None


def base_url_for(provider: Provider, key_base_url: str | None) -> str:
    url = (key_base_url or provider.base_url or "").strip().rstrip("/")
    return url


def public_list() -> list[dict]:
    return [{"id": p.id, "name": p.name, "kind": p.kind, "needs_base_url": p.needs_base_url,
             "key_hint": p.key_hint, "default_model": p.default_model} for p in PROVIDERS.values()]
