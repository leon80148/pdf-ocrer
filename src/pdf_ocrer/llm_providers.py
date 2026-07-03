from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from openai import OpenAI

from pdf_ocrer.config import ConfigError, LlmConfig, resolve_api_key


class LLMError(Exception):
    """Raised when an LLM provider cannot return usable completion text."""


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


_ProviderFactory = Callable[[LlmConfig], LLMClient]
_TProviderFactory = TypeVar("_TProviderFactory", bound=_ProviderFactory)
_PROVIDERS: dict[str, _ProviderFactory] = {}


def register_provider(name: str):
    key = name.casefold()
    if not key:
        raise ConfigError("LLM provider name must not be empty")

    def decorator(factory: _TProviderFactory) -> _TProviderFactory:
        if key in _PROVIDERS:
            raise ConfigError(f"LLM provider already registered: {name}")
        _PROVIDERS[key] = factory
        return factory

    return decorator


def create_client(cfg: LlmConfig) -> LLMClient | None:
    provider = cfg.provider.casefold()
    if provider == "none":
        return None

    factory = _PROVIDERS.get(provider)
    if factory is None:
        available = ", ".join(sorted(_PROVIDERS))
        raise ConfigError(f"Unknown LLM provider {cfg.provider!r}. Available providers: {available}")

    return factory(cfg)


@register_provider("openai_compatible")
class OpenAICompatClient:
    def __init__(self, cfg: LlmConfig) -> None:
        self._cfg = cfg
        try:
            self._client = OpenAI(
                base_url=cfg.base_url,
                api_key=resolve_api_key(cfg) or "not-needed",
                timeout=cfg.timeout_seconds,
            )
        except Exception as exc:
            raise LLMError(f"Failed to initialize LLM client: {exc}") from exc

    def complete(self, prompt: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._cfg.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self._cfg.temperature,
                max_tokens=self._cfg.max_tokens,
            )
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError("LLM response did not contain message content") from exc

        if content is None or content == "":
            exc = ValueError("empty LLM response content")
            raise LLMError("LLM returned empty content") from exc
        if not isinstance(content, str):
            exc = TypeError(f"expected str content, got {type(content).__name__}")
            raise LLMError("LLM response content was not text") from exc

        return content
