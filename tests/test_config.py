from __future__ import annotations

import warnings

import pytest

from pdf_ocrer.config import ConfigError, LlmConfig, load_config, resolve_api_key


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")

    assert cfg.ocr.dpi == 200
    assert cfg.llm.provider == "openai_compatible"


def test_toml_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[ocr]\ndpi = 300\n", encoding="utf-8")

    assert load_config(p).ocr.dpi == 300


def test_unknown_key_warns(tmp_path, recwarn):
    p = tmp_path / "c.toml"
    p.write_text("[ocr]\nbogus = 1\n", encoding="utf-8")

    load_config(p)

    assert any("bogus" in str(w.message) for w in recwarn.list)


def test_unknown_section_warns(tmp_path, recwarn):
    p = tmp_path / "c.toml"
    p.write_text("[extra]\nvalue = 1\n", encoding="utf-8")

    load_config(p)

    assert any("extra" in str(w.message) for w in recwarn.list)


def test_invalid_dpi_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[ocr]\ndpi = 10\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="dpi"):
        load_config(p)


def test_invalid_filename_length_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[naming]\nmax_filename_length = 5\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="max_filename_length"):
        load_config(p)


def test_api_key_env_fallback(monkeypatch):
    monkeypatch.setenv("PDF_OCRER_API_KEY", "sk-x")

    assert resolve_api_key(LlmConfig()) == "sk-x"


def test_api_key_config_takes_precedence(monkeypatch):
    monkeypatch.setenv("PDF_OCRER_API_KEY", "sk-env")

    assert resolve_api_key(LlmConfig(api_key="sk-config")) == "sk-config"


def test_unknown_key_uses_warning_class(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[ocr]\nbogus = 1\n", encoding="utf-8")

    with pytest.warns(UserWarning, match="bogus"):
        load_config(p)


def test_no_warning_for_missing_file(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        cfg = load_config(tmp_path / "missing.toml")

    assert cfg.output.subdir_name == "OCR輸出"
    assert caught == []
