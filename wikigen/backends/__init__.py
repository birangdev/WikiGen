"""wikigen LLM backends — unified interface over Claude, OpenAI, and Ollama."""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import Any

from ..config import BackendConfig

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    """Abstract LLM backend.  All backends expose a single `complete` method."""

    def __init__(self, cfg: BackendConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Send a completion request; return the assistant's response text."""
        ...

    @property
    def model_name(self) -> str:
        return self.cfg.model or self._default_model()

    def _default_model(self) -> str:
        return ""


# ---------------------------------------------------------------------------
# Claude (Anthropic)
# ---------------------------------------------------------------------------

class ClaudeBackend(LLMBackend):
    """Anthropic Claude backend using the official SDK."""

    _DEFAULT_MODEL = "claude-sonnet-4-20250514"

    def _default_model(self) -> str:
        return self._DEFAULT_MODEL

    def complete(self, system: str, user: str) -> str:
        try:
            import anthropic
        except ImportError:
            sys.exit("✗ anthropic package not installed.  Run: pip install anthropic")

        api_key = self.cfg.resolve_api_key()
        if not api_key:
            env = self.cfg.api_key_env or "ANTHROPIC_API_KEY"
            sys.exit(f"✗ API key not found.  Set the {env} environment variable.")

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=self.model_name,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

class OpenAIBackend(LLMBackend):
    """OpenAI-compatible backend (works with OpenAI, Azure, Together, etc.)."""

    _DEFAULT_MODEL = "gpt-4o"

    def _default_model(self) -> str:
        return self._DEFAULT_MODEL

    def complete(self, system: str, user: str) -> str:
        try:
            import openai
        except ImportError:
            sys.exit("✗ openai package not installed.  Run: pip install openai")

        api_key = self.cfg.resolve_api_key()
        if not api_key:
            env = self.cfg.api_key_env or "OPENAI_API_KEY"
            sys.exit(f"✗ API key not found.  Set the {env} environment variable.")

        kwargs: dict[str, Any] = {"api_key": api_key}
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url

        client = openai.OpenAI(**kwargs)
        response = client.chat.completions.create(
            model=self.model_name,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaBackend(LLMBackend):
    """Local Ollama backend via its OpenAI-compatible REST API."""

    _DEFAULT_MODEL = "llama3"

    def _default_model(self) -> str:
        return self._DEFAULT_MODEL

    def complete(self, system: str, user: str) -> str:
        try:
            import httpx
        except ImportError:
            sys.exit("✗ httpx package not installed.  Run: pip install httpx")

        base_url = self.cfg.base_url or "http://localhost:11434"
        url = f"{base_url.rstrip('/')}/api/chat"

        payload = {
            "model": self.model_name,
            "stream": False,
            "options": {"temperature": self.cfg.temperature, "num_predict": self.cfg.max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        with httpx.Client(timeout=300) as client:
            resp = client.post(url, json=payload)

        if resp.status_code != 200:
            sys.exit(f"✗ Ollama error {resp.status_code}: {resp.text}")

        data = resp.json()
        return data.get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[LLMBackend]] = {
    "claude": ClaudeBackend,
    "openai": OpenAIBackend,
    "ollama": OllamaBackend,
}


def get_backend(cfg: BackendConfig) -> LLMBackend:
    name = cfg.name.lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(_REGISTRY)
        sys.exit(f"✗ Unknown backend {name!r}.  Supported: {known}")
    return cls(cfg)
