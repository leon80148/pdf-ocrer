from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pdf_ocrer.config import ConfigError, LlmConfig
from pdf_ocrer.llm_providers import LLMError, create_client, register_provider


def test_create_client_none_provider():
    assert create_client(replace(LlmConfig(), provider="none")) is None


def test_create_client_unknown_lists_available():
    with pytest.raises(ConfigError, match="openai_compatible"):
        create_client(replace(LlmConfig(), provider="wat"))


def test_register_provider_is_casefolded():
    calls = []

    @register_provider("TEST_CASE_PROVIDER")
    class FakeClient:
        def __init__(self, cfg: LlmConfig) -> None:
            calls.append(cfg.provider)

        def complete(self, prompt: str) -> str:
            return prompt

    client = create_client(replace(LlmConfig(), provider="test_case_provider"))

    assert client is not None
    assert client.complete("ok") == "ok"
    assert calls == ["test_case_provider"]


def test_openai_compat_calls_sdk(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            msg = SimpleNamespace(content="20260615_診斷證明書_王小明")
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class FakeOpenAI:
        def __init__(self, **kw):
            captured["init"] = kw
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("pdf_ocrer.llm_providers.OpenAI", FakeOpenAI)

    out = create_client(LlmConfig()).complete("hi")

    assert out == "20260615_診斷證明書_王小明"
    assert captured["init"] == {
        "base_url": "http://localhost:11434/v1",
        "api_key": "not-needed",
        "timeout": 60.0,
    }
    assert captured["model"] == "qwen3:8b"
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["temperature"] == 0.1
    assert captured["max_tokens"] == 1024


@pytest.mark.parametrize("content", [None, ""])
def test_openai_compat_empty_response_raises(monkeypatch, content):
    class FakeCompletions:
        def create(self, **kw):
            msg = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("pdf_ocrer.llm_providers.OpenAI", FakeOpenAI)

    with pytest.raises(LLMError) as excinfo:
        create_client(LlmConfig()).complete("hi")

    assert excinfo.value.__cause__ is not None


def test_openai_compat_sdk_exception_raises_llm_error(monkeypatch):
    class FakeCompletions:
        def create(self, **kw):
            raise RuntimeError("down")

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("pdf_ocrer.llm_providers.OpenAI", FakeOpenAI)

    with pytest.raises(LLMError, match="down") as excinfo:
        create_client(LlmConfig()).complete("hi")

    assert isinstance(excinfo.value.__cause__, RuntimeError)
