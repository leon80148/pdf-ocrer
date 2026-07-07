from __future__ import annotations

from dataclasses import replace
import warnings

import pytest

from pdf_ocrer.config import (
    CommonSettings,
    ConfigError,
    LlmConfig,
    apply_common_settings,
    ensure_config_file,
    load_config,
    read_common_settings,
    resolve_api_key,
)


def _common_settings(**overrides: object) -> CommonSettings:
    values = {
        "dpi": 300,
        "min_confidence": 0.75,
        "det_model_name": "PP-OCRv6_mobile_det",
        "rec_model_name": "PP-OCRv6_mobile_rec",
        "naming_enabled": False,
        "llm_provider": "none",
        "llm_model": "local-model",
        "llm_base_url": "https://example.test/v1",
        "llm_api_key": "sk-test",
    }
    values.update(overrides)
    return CommonSettings(**values)


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")

    assert cfg.ocr.dpi == 200
    assert cfg.ocr.engine == "paddle"
    assert cfg.ocr.cpu_threads == 0
    assert cfg.ocr.textline_orientation is True
    assert cfg.llm.provider == "openai_compatible"
    assert cfg.gui.appearance == "system"


def test_toml_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[ocr]\ndpi = 300\n", encoding="utf-8")

    assert load_config(p).ocr.dpi == 300


def test_old_ocr_config_loads_new_engine_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[ocr]\n"
        "dpi = 300\n"
        "min_confidence = 0.8\n",
        encoding="utf-8",
    )

    cfg = load_config(p)

    assert cfg.ocr.engine == "paddle"
    assert cfg.ocr.cpu_threads == 0
    assert cfg.ocr.textline_orientation is True


def test_ocr_engine_fields_load_from_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[ocr]\n"
        "engine = \"rapidocr\"\n"
        "cpu_threads = 4\n"
        "textline_orientation = false\n",
        encoding="utf-8",
    )

    cfg = load_config(p)

    assert cfg.ocr.engine == "rapidocr"
    assert cfg.ocr.cpu_threads == 4
    assert cfg.ocr.textline_orientation is False


def test_invalid_ocr_engine_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[ocr]\nengine = \"unknown\"\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="engine.*paddle.*rapidocr"):
        load_config(p)


@pytest.mark.parametrize("cpu_threads", [-1, 65])
def test_invalid_cpu_threads_range_raises(tmp_path, cpu_threads):
    p = tmp_path / "c.toml"
    p.write_text(f"[ocr]\ncpu_threads = {cpu_threads}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="cpu_threads"):
        load_config(p)


def test_gui_appearance_loads(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[gui]\nappearance = \"dark\"\n", encoding="utf-8")

    assert load_config(p).gui.appearance == "dark"


def test_invalid_gui_appearance_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[gui]\nappearance = \"blue\"\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="appearance"):
        load_config(p)


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


def test_ensure_config_file_creates_empty_file_without_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "nested" / "config.toml"

    ensure_config_file(config_path)

    assert config_path.read_text(encoding="utf-8") == ""


def test_ensure_config_file_copies_example_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    example = tmp_path / "config.example.toml"
    example.write_text("# template\n[ocr]\ndpi = 222\n", encoding="utf-8")
    config_path = tmp_path / "config" / "config.toml"

    ensure_config_file(config_path)

    assert config_path.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")


def test_ensure_config_file_leaves_existing_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    original = "# existing\n[ocr]\ndpi = 240\n"
    config_path.write_text(original, encoding="utf-8")

    ensure_config_file(config_path)

    assert config_path.read_text(encoding="utf-8") == original


def test_apply_common_settings_preserves_comments_and_unrelated_keys(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# root comment\n"
        "\n"
        "[ocr]\n"
        "# dpi comment\n"
        "dpi = 200\n"
        "lang = \"chinese_cht\"\n"
        "min_confidence = 0.5\n"
        "det_model_name = \"old_det\"\n"
        "rec_model_name = \"old_rec\"\n"
        "\n"
        "[naming]\n"
        "enabled = true\n"
        "fallback_suffix = \"_KEEP\"\n"
        "\n"
        "[llm]\n"
        "provider = \"openai_compatible\"\n"
        "base_url = \"http://localhost:11434/v1\"\n"
        "model = \"old-model\"\n"
        "api_key = \"\"\n"
        "timeout_seconds = 45.0\n"
        "\n"
        "[debug]\n"
        "visible_text = true\n",
        encoding="utf-8",
    )

    settings = _common_settings()
    apply_common_settings(config_path, settings)

    text = config_path.read_text(encoding="utf-8")
    cfg = load_config(config_path)
    assert "# root comment" in text
    assert "# dpi comment" in text
    assert cfg.ocr.dpi == 300
    assert cfg.ocr.min_confidence == 0.75
    assert cfg.ocr.det_model_name == "PP-OCRv6_mobile_det"
    assert cfg.ocr.rec_model_name == "PP-OCRv6_mobile_rec"
    assert cfg.ocr.lang == "chinese_cht"
    assert cfg.naming.enabled is False
    assert cfg.naming.fallback_suffix == "_KEEP"
    assert cfg.llm.provider == "none"
    assert cfg.llm.model == "local-model"
    assert cfg.llm.base_url == "https://example.test/v1"
    assert cfg.llm.api_key == "sk-test"
    assert cfg.llm.timeout_seconds == 45.0
    assert cfg.debug.visible_text is True
    assert read_common_settings(config_path) == settings


def test_apply_common_settings_removes_model_keys_for_medium_defaults(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[ocr]\n"
        "det_model_name = \"old_det\"\n"
        "rec_model_name = \"old_rec\"\n"
        "\n"
        "[naming]\n"
        "enabled = true\n"
        "\n"
        "[llm]\n"
        "provider = \"openai_compatible\"\n",
        encoding="utf-8",
    )

    apply_common_settings(config_path, _common_settings(det_model_name=None, rec_model_name=None))

    text = config_path.read_text(encoding="utf-8")
    cfg = load_config(config_path)
    assert "det_model_name" not in text
    assert "rec_model_name" not in text
    assert "null" not in text.lower()
    assert cfg.ocr.det_model_name is None
    assert cfg.ocr.rec_model_name is None


def test_apply_common_settings_does_not_write_when_validation_fails(tmp_path):
    config_path = tmp_path / "config.toml"
    original = "[ocr]\ndpi = 200\n"
    config_path.write_text(original, encoding="utf-8")
    original_bytes = config_path.read_bytes()

    with pytest.raises(ConfigError, match="dpi"):
        apply_common_settings(config_path, replace(_common_settings(), dpi=9999))

    assert config_path.read_bytes() == original_bytes


def test_apply_common_settings_bootstraps_missing_file_without_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "nested" / "config.toml"

    apply_common_settings(config_path, _common_settings())

    cfg = load_config(config_path)
    assert cfg.ocr.dpi == 300
    assert cfg.naming.enabled is False
    assert "[ocr]" in config_path.read_text(encoding="utf-8")


def test_apply_common_settings_bootstraps_missing_file_from_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    example = tmp_path / "config.example.toml"
    example.write_text(
        "# template comment\n"
        "[ocr]\n"
        "dpi = 200\n"
        "\n"
        "[output]\n"
        "subdir_name = \"KEEP\"\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "nested" / "config.toml"

    apply_common_settings(config_path, _common_settings())

    text = config_path.read_text(encoding="utf-8")
    cfg = load_config(config_path)
    assert "# template comment" in text
    assert cfg.ocr.dpi == 300
    assert cfg.output.subdir_name == "KEEP"
