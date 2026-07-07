from __future__ import annotations

from dataclasses import replace

import pytest

from pdf_ocrer.config import AppConfig, DebugConfig, LlmConfig, NamingConfig, OcrConfig, OutputConfig
from pdf_ocrer.llm_namer import (
    build_prompt,
    resolve_collision,
    sanitize_filename,
    suggest_filename,
)
from pdf_ocrer.llm_providers import LLMError


def make_cfg(naming: NamingConfig | None = None) -> AppConfig:
    return AppConfig(
        ocr=OcrConfig(),
        output=OutputConfig(),
        naming=NamingConfig() if naming is None else naming,
        llm=LlmConfig(),
        debug=DebugConfig(),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<think>嗯...想想</think>\n20260615_轉診單_王小明", "20260615_轉診單_王小明"),
        ("「20260615_收據」", "20260615_收據"),
        ("**`20260615_收據`**", "20260615_收據"),
        ("# 20260615_報告", "20260615_報告"),
        ("檔名：20260615_報告.pdf", "檔名：20260615_報告"),
        ("檔名:20260615_報告.pdf", "檔名20260615_報告"),
        ('a/b\\c:d*e?f"g<h>i|j', "abcdefghij"),
        ("a\x00b\x1fc\x7f", "abc"),
        ("a   \t b", "a b"),
        ("name...   ", "name"),
        ("CON", "_CON"),
        ("lpt9.txt", "_lpt9.txt"),
        ("", None),
        ("<think>only</think>", None),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize_filename(raw, 80) == expected


def test_sanitize_uses_first_non_empty_line():
    assert sanitize_filename("\n \nfirst\nsecond", 80) == "first"


def test_sanitize_truncates_to_max():
    assert len(sanitize_filename("字" * 300, 80)) == 80


def test_sanitize_strips_trailing_dot_after_truncation():
    assert sanitize_filename("abc.def", 4) == "abc"


def test_collision_disk_and_batch(tmp_path):
    (tmp_path / "報告.pdf").touch()
    used: set[str] = set()

    assert resolve_collision(tmp_path, "報告", used) == "報告_2"
    assert "報告_2".casefold() in used
    assert resolve_collision(tmp_path, "報告", used) == "報告_3"
    assert resolve_collision(tmp_path, "RePort", {"report"}) == "RePort_2"


def test_collision_treats_existing_txt_as_unavailable(tmp_path):
    (tmp_path / "X.txt").touch()

    assert resolve_collision(tmp_path, "X", set()) == "X_2"


def test_suggest_retries_once_then_fallback():
    calls = []

    class Boom:
        def complete(self, prompt: str) -> str:
            calls.append(prompt)
            raise LLMError("down")

    stem, source = suggest_filename("文字", "scan001", make_cfg(), Boom(), "$text")

    assert (stem, source) == ("scan001_OCR", "fallback")
    assert len(calls) == 2


def test_suggest_retries_once_then_uses_second_success():
    calls = []

    class Flaky:
        def complete(self, prompt: str) -> str:
            calls.append(prompt)
            if len(calls) == 1:
                raise LLMError("temporary")
            return "「20260615_收據.pdf」"

    stem, source = suggest_filename("文字", "scan001", make_cfg(), Flaky(), "$text")

    assert (stem, source) == ("20260615_收據", "llm")
    assert len(calls) == 2


def test_suggest_fallback_when_client_none():
    assert suggest_filename("文字", "scan001", make_cfg(), None, "$text") == (
        "scan001_OCR",
        "fallback",
    )


def test_suggest_fallback_when_text_blank():
    class ShouldNotCall:
        def complete(self, prompt: str) -> str:
            raise AssertionError("client should not be called")

    assert suggest_filename("  \n", "scan001", make_cfg(), ShouldNotCall(), "$text") == (
        "scan001_OCR",
        "fallback",
    )


def test_suggest_fallback_when_sanitized_name_empty():
    calls = []

    class EmptyName:
        def complete(self, prompt: str) -> str:
            calls.append(prompt)
            return "<think>only</think>"

    assert suggest_filename("文字", "scan001", make_cfg(), EmptyName(), "$text") == (
        "scan001_OCR",
        "fallback",
    )
    assert len(calls) == 1


def test_suggest_uses_configured_max_filename_length_and_suffix():
    cfg = make_cfg(replace(NamingConfig(), max_filename_length=5, fallback_suffix="_FB"))

    class LongName:
        def complete(self, prompt: str) -> str:
            return "abcdef"

    assert suggest_filename("文字", "scan001", cfg, LongName(), "$text") == ("abcde", "llm")
    assert suggest_filename("", "scan001", cfg, LongName(), "$text") == ("scan001_FB", "fallback")


def test_build_prompt_user_braces_safe():
    out = build_prompt("模板{x} $text $original_name $today $unknown", "T", "o", "20260703")

    assert "T" in out
    assert "o" in out
    assert "20260703" in out
    assert "{x}" in out
    assert "$unknown" in out
